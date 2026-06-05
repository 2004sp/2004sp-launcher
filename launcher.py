#!/usr/bin/env python3
"""
2004sp Launcher — unified installer + server manager
Replaces AIO-2004sp-install.bat and engine/launcher.ts
"""

import sys
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import queue
import platform
import webbrowser
from pathlib import Path
from uuid import uuid4

import dearpygui.dearpygui as dpg
import bcrypt
import requests

# ─────────────────────────────────────────────────────────────────────────────
#  Bootstrap helpers
# ─────────────────────────────────────────────────────────────────────────────

def _launcher_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent

def resource_path(rel: str) -> str:
    base = Path(getattr(sys, "_MEIPASS", _launcher_dir()))
    return str(base / rel)

# ─────────────────────────────────────────────────────────────────────────────
#  Constants
# ─────────────────────────────────────────────────────────────────────────────

APP_TITLE   = "2004sp Launcher"
APP_VERSION = "1.0.0"
WIN_W, WIN_H = 1220, 800

REPO_ENGINE           = "https://github.com/LostCityRS/Engine-TS.git"
REPO_CONTENT          = "https://github.com/LostCityRS/Content.git"
REPO_SERVER           = "https://github.com/LostCityRS/Server.git"
REPO_PROGRESSIVE      = "https://github.com/2004sp/2004sp-progressive.git"
REPO_EXTRAS           = "https://github.com/2004sp/2004sp-extras.git"
CLIENT_API            = "https://api.github.com/repos/2004sp/2004sp-client/releases/latest"
PROGRESSIVE_BRANCHES_API = "https://api.github.com/repos/2004sp/2004sp-progressive/branches"

INSTALL_DIR_DEFAULT = _launcher_dir() / "lostcity254"

# ─────────────────────────────────────────────────────────────────────────────
#  Color palette
# ─────────────────────────────────────────────────────────────────────────────

C_BG       = (13,  13,  18,  255)
C_PANEL    = (20,  20,  32,  255)
C_CYAN     = (0,   229, 255, 255)
C_MAGENTA  = (198, 120, 221, 255)
C_GREEN    = (152, 195, 121, 255)
C_YELLOW   = (229, 192, 123, 255)
C_RED      = (224, 108, 117, 255)
C_TEXT     = (171, 178, 191, 255)
C_DIM      = (90,  95,  110, 255)
C_BORDER   = (50,  50,  80,  255)
C_BTN      = (30,  30,  50,  255)
C_BTN_HOV  = (40,  40,  68,  255)
C_BTN_ACT  = (50,  50,  85,  255)

LOG_COLORS = {
    "ok":   C_GREEN,
    "warn": C_YELLOW,
    "err":  C_RED,
    "step": C_CYAN,
    "info": C_TEXT,
    "dim":  C_DIM,
}
LOG_LABELS = {
    "ok":   " OK  ",
    "warn": "WARN ",
    "err":  "FAIL ",
    "step": " >>  ",
    "info": "INFO ",
    "dim":  "     ",
}

# ─────────────────────────────────────────────────────────────────────────────
#  Global state
# ─────────────────────────────────────────────────────────────────────────────

_state_lock = threading.Lock()
_state: dict = {
    "install_dir":       INSTALL_DIR_DEFAULT,
    "engine_dir":        None,
    "running_procs":     {},      # name -> subprocess.Popen
    "dl_progress":       0.0,
    "selected_branch":   "dev",
    "available_branches": ["dev"],
}

_log_queue: queue.Queue = queue.Queue()
_log_items: list = []
MAX_LOG_LINES = 400

# UI-update queue: background threads push (callable, args) tuples;
# the render loop drains them on the main thread.
_ui_updates: queue.Queue = queue.Queue()

# Confirm-dialog system: background threads push requests; render loop creates modals.
_confirm_queue: queue.Queue   = queue.Queue()
_confirm_results: dict        = {}   # req_id -> {"event": Event, "result": bool}

# ─────────────────────────────────────────────────────────────────────────────
#  Logging
# ─────────────────────────────────────────────────────────────────────────────

def log(msg: str, level: str = "info") -> None:
    _log_queue.put((msg, level))

def flush_log() -> None:
    """Drain log queue and append text items to the log panel (main thread only)."""
    changed = False
    while not _log_queue.empty():
        try:
            msg, level = _log_queue.get_nowait()
        except queue.Empty:
            break
        tag = f"logln_{uuid4().hex[:8]}"
        prefix = LOG_LABELS.get(level, "INFO ")
        color  = LOG_COLORS.get(level, C_TEXT)
        dpg.add_text(f"[{prefix}] {msg}", color=color, parent="log_panel", tag=tag, wrap=0)
        _log_items.append(tag)
        changed = True

    while len(_log_items) > MAX_LOG_LINES:
        old = _log_items.pop(0)
        if dpg.does_item_exist(old):
            dpg.delete_item(old)

    if changed:
        dpg.set_y_scroll("log_panel", dpg.get_y_scroll_max("log_panel"))

# ─────────────────────────────────────────────────────────────────────────────
#  UI-update queue (main-thread-safe deferred updates from background threads)
# ─────────────────────────────────────────────────────────────────────────────

def _drain_ui_updates() -> None:
    while not _ui_updates.empty():
        try:
            fn, args = _ui_updates.get_nowait()
            fn(*args)
        except queue.Empty:
            break

# ─────────────────────────────────────────────────────────────────────────────
#  Confirm dialog  (background-thread-safe Yes/No)
# ─────────────────────────────────────────────────────────────────────────────

def _confirm(message: str, title: str = "Confirm") -> bool:
    """Block the calling (background) thread until the user clicks Yes or No.
    The render loop picks up the request and creates the modal on the main thread."""
    req_id = uuid4().hex[:8]
    event  = threading.Event()
    _confirm_results[req_id] = {"event": event, "result": False}
    _confirm_queue.put((req_id, title, message))
    event.wait(timeout=300)   # auto-decline after 5 min if ignored
    return _confirm_results.pop(req_id, {}).get("result", False)

def _on_confirm_btn(sender, app_data, user_data) -> None:
    """Shared callback for Yes/No modal buttons. user_data = (req_id, modal_tag, result)."""
    req_id, modal_tag, result = user_data
    entry = _confirm_results.get(req_id)
    if entry:
        entry["result"] = result
        entry["event"].set()
    if dpg.does_item_exist(modal_tag):
        dpg.delete_item(modal_tag)

def _show_confirm_modal(req_id: str, title: str, message: str) -> None:
    """Create a DearPyGui modal. Must be called from the main thread (render loop)."""
    modal_tag = f"modal_{req_id}"
    cx = WIN_W // 2 - 215
    cy = WIN_H // 2 - 80

    with dpg.window(label=title, modal=True, tag=modal_tag,
                    no_resize=True, no_move=False,
                    width=430, height=160,
                    pos=[cx, cy]):
        dpg.add_spacer(height=6)
        dpg.add_text(message, color=C_TEXT, wrap=410)
        dpg.add_spacer(height=14)
        with dpg.group(horizontal=True):
            dpg.add_button(
                label="  Yes  ",
                callback=_on_confirm_btn,
                user_data=(req_id, modal_tag, True),
                width=90, height=32,
            )
            dpg.add_spacer(width=10)
            dpg.add_button(
                label="  No   ",
                callback=_on_confirm_btn,
                user_data=(req_id, modal_tag, False),
                width=90, height=32,
            )

def _drain_confirm_queue() -> None:
    """Called every frame from the render loop to pop pending confirm requests."""
    while not _confirm_queue.empty():
        try:
            req_id, title, message = _confirm_queue.get_nowait()
        except queue.Empty:
            break
        _show_confirm_modal(req_id, title, message)

# ─────────────────────────────────────────────────────────────────────────────
#  Core helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_win() -> bool:
    return platform.system() == "Windows"

def stream_cmd(cmd: list, cwd: Path) -> int:
    """Run command, stream stdout+stderr to log panel. Returns exit code."""
    log(f"$ {' '.join(str(c) for c in cmd)}", "dim")
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(cwd),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, shell=_is_win(),
            encoding="utf-8", errors="replace",
        )
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                log(line, "dim")
        proc.wait()
        return proc.returncode
    except FileNotFoundError as e:
        log(f"Command not found: {e}", "err")
        return -1
    except Exception as e:
        log(f"Command error: {e}", "err")
        return -1

def git_clone_or_pull(url: str, dest: Path, branch: str | None = None) -> bool:
    if (dest / ".git").exists():
        log(f"Pulling {dest.name}...", "step")
        rc = stream_cmd(["git", "pull"], dest)
    else:
        log(f"Cloning {dest.name}...", "step")
        cmd = ["git", "clone", url]
        if branch:
            cmd += ["-b", branch, "--single-branch"]
        cmd.append(str(dest))
        dest.parent.mkdir(parents=True, exist_ok=True)
        rc = stream_cmd(cmd, dest.parent)
    if rc != 0:
        log(f"git op failed (exit {rc})", "err")
        return False
    log(f"{dest.name} ready.", "ok")
    return True

def _git_head(path: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(path),
            text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        return ""

def overlay_files(src: Path, dst: Path) -> None:
    """Copy src tree onto dst, skipping .git dirs (cross-platform robocopy equiv)."""
    log(f"Overlaying {src.name} → {dst.name}...", "step")
    def _ignore(_dir: str, names: list) -> list:
        return [n for n in names if n == ".git"]
    shutil.copytree(str(src), str(dst), ignore=_ignore, dirs_exist_ok=True)
    log("Overlay complete.", "ok")

def npm_install(cwd: Path, label: str) -> None:
    log(f"npm install @ {label}...", "step")
    rc = stream_cmd(["npm", "install"], cwd)
    if rc != 0:
        log(f"npm install @ {label} finished with warnings.", "warn")
    else:
        log(f"npm install @ {label} done.", "ok")

def patch_env(env_path: Path, patches: dict) -> None:
    content = env_path.read_text("utf-8") if env_path.exists() else ""
    for key, value in patches.items():
        pattern = re.compile(rf"^#?\s*{re.escape(key)}\s*=.*$", re.MULTILINE)
        replacement = f"{key}={value}"
        if pattern.search(content):
            content = pattern.sub(replacement, content)
        else:
            content += f"\n{replacement}"
    env_path.write_text(content, "utf-8")
    for k, v in patches.items():
        log(f"  {k}={v}", "ok")

def check_prereqs() -> dict:
    result: dict = {}
    for tool in ["git", "node", "npm"]:
        try:
            r = subprocess.run(
                [tool, "--version"], capture_output=True, text=True,
                timeout=8, shell=_is_win()
            )
            ver = (r.stdout or r.stderr).strip().splitlines()[0]
            result[tool] = ver
            log(f"{tool}: {ver}", "ok")
        except Exception:
            result[tool] = None
            log(f"{tool}: NOT FOUND", "err")
    return result

def get_engine_dir() -> Path | None:
    with _state_lock:
        ed = _state.get("engine_dir")
        if ed and Path(ed).exists():
            return Path(ed)
        guess = Path(_state["install_dir"]) / "engine"
        if guess.exists():
            _state["engine_dir"] = guess
            return guess
    return None

# ─────────────────────────────────────────────────────────────────────────────
#  Branch detection
# ─────────────────────────────────────────────────────────────────────────────

def _lostcity_branch(progressive_branch: str) -> str:
    """Return the LostCity repo branch that matches the progressive branch name."""
    if "274" in progressive_branch:
        return "274"
    return "254"

def _fetch_progressive_branches() -> None:
    """Fetch available branches from 2004sp-progressive and populate the combo."""
    log("Fetching branches from 2004sp-progressive...", "step")
    try:
        r = requests.get(PROGRESSIVE_BRANCHES_API, timeout=15)
        r.raise_for_status()
        names = [b["name"] for b in r.json()]
        if not names:
            log("No branches returned — using fallback.", "warn")
            names = ["dev", "main"]

        def _sort_key(n: str):
            nl = n.lower()
            if "dev" in nl:
                return (0, n)
            if "main" in nl or "stable" in nl:
                return (2, n)
            return (1, n)

        names.sort(key=_sort_key)
        default = next((n for n in names if "dev" in n.lower()), names[0])

        with _state_lock:
            _state["available_branches"] = names
            _state["selected_branch"]    = default

        def _apply(branch_names, branch_default):
            if dpg.does_item_exist("branch_combo"):
                dpg.configure_item("branch_combo", items=branch_names,
                                   default_value=branch_default)
            log(f"Branches loaded — {len(branch_names)} found. Default: {branch_default}", "ok")

        _ui_updates.put((_apply, (names, default)))

    except Exception as e:
        log(f"Could not fetch branches: {e}", "warn")
        log("Falling back to dev / main.", "info")
        with _state_lock:
            _state["available_branches"] = ["dev", "main"]
            _state["selected_branch"]    = "dev"

        def _apply_fallback(branch_names, branch_default):
            if dpg.does_item_exist("branch_combo"):
                dpg.configure_item("branch_combo", items=branch_names,
                                   default_value=branch_default)

        _ui_updates.put((_apply_fallback, (["dev", "main"], "dev")))

# ─────────────────────────────────────────────────────────────────────────────
#  Client launcher
# ─────────────────────────────────────────────────────────────────────────────

def _find_client_binary() -> Path | None:
    """Return the first 2004sp client binary found in the install directory."""
    install_dir = Path(_state["install_dir"])
    system = platform.system().lower()
    if "windows" in system:
        patterns = ["*.exe", "*.msi"]
        exclude  = {"uninstall", "setup"}
    elif "darwin" in system:
        patterns = ["*.dmg", "*.app"]
        exclude  = set()
    else:
        patterns = ["*.AppImage", "*.deb"]
        exclude  = set()

    for pattern in patterns:
        for p in install_dir.glob(pattern):
            if not any(ex in p.name.lower() for ex in exclude):
                return p
    return None

def op_launch_client() -> None:
    binary = _find_client_binary()
    if not binary:
        log("No 2004sp client found in the install directory.", "warn")
        log("Use Install → Install Client to download it first.", "info")
        return
    log(f"Launching: {binary.name}", "step")
    try:
        system = platform.system()
        if system == "Windows":
            os.startfile(str(binary))
        elif system == "Darwin":
            subprocess.Popen(["open", str(binary)])
        else:
            subprocess.Popen([str(binary)])
        log("Client launched.", "ok")
    except Exception as e:
        log(f"Could not launch client: {e}", "err")

# ─────────────────────────────────────────────────────────────────────────────
#  Installer operations
# ─────────────────────────────────────────────────────────────────────────────

def op_check_prereqs() -> None:
    log("── Checking Prerequisites ──", "step")
    prereqs = check_prereqs()
    missing = [t for t, v in prereqs.items() if v is None]
    if missing:
        log(f"Missing: {', '.join(missing)}", "err")
    else:
        log("All prerequisites satisfied.", "ok")

def _do_server_install(branch: str, install_dir: Path) -> bool:
    prereqs = check_prereqs()
    if None in prereqs.values():
        log("Prerequisites missing — aborting.", "err")
        return False

    lc_branch = _lostcity_branch(branch)
    log(f"Using LostCity branch: {lc_branch}", "info")

    install_dir.mkdir(parents=True, exist_ok=True)

    # Server root files
    server_tmp = install_dir / "_server_tmp"
    if not (install_dir / "package.json").exists():
        if git_clone_or_pull(REPO_SERVER, server_tmp):
            if server_tmp.exists():
                overlay_files(server_tmp, install_dir)
                shutil.rmtree(str(server_tmp), ignore_errors=True)

    # Engine-TS
    git_clone_or_pull(REPO_ENGINE, install_dir / "engine", branch=lc_branch)

    # Content
    git_clone_or_pull(REPO_CONTENT, install_dir / "content", branch=lc_branch)

    # Progressive bots overlay
    prog_tmp = install_dir / "_progressive_tmp"
    if git_clone_or_pull(REPO_PROGRESSIVE, prog_tmp, branch=branch):
        overlay_files(prog_tmp, install_dir)

    # npm install
    log("── Installing Dependencies ──", "step")
    npm_install(install_dir, "root")
    npm_install(install_dir / "engine", "engine/")
    content_pkg = install_dir / "content" / "package.json"
    if content_pkg.exists():
        npm_install(install_dir / "content", "content/")

    # .env bootstrap
    env_example = install_dir / "engine" / ".env.example"
    env_dest    = install_dir / "engine" / ".env"
    if env_example.exists() and not env_dest.exists():
        shutil.copy(str(env_example), str(env_dest))
        log("Copied .env.example → .env", "ok")

    return True

def _do_client_install(install_dir: Path) -> None:
    log("── Downloading 2004sp Client ──", "step")
    try:
        r = requests.get(CLIENT_API, timeout=15)
        r.raise_for_status()
        assets = r.json().get("assets", [])

        system = platform.system().lower()
        if "windows" in system:
            inc = [r"\.exe$", r"\.msi$", r"win", r"x64", r"setup"]
            exc = [r"blockmap", r"sha256", r"\.yml$", r"\.yaml$", r"dmg", r"appimage", r"\.deb$"]
        elif "darwin" in system:
            inc = [r"\.dmg$", r"mac", r"darwin", r"osx"]
            exc = [r"blockmap", r"sha256", r"\.yml$", r"\.exe$", r"appimage"]
        else:
            inc = [r"appimage", r"\.deb$", r"linux"]
            exc = [r"blockmap", r"sha256", r"\.yml$", r"\.exe$", r"\.dmg$"]

        def _match(name: str) -> bool:
            n = name.lower()
            if any(re.search(p, n, re.I) for p in exc):
                return False
            return any(re.search(p, n, re.I) for p in inc)

        asset = next((a for a in assets if _match(a["name"])), None)
        if not asset:
            log(f"No client binary found for {platform.system()}.", "warn")
            log("https://github.com/2004sp/2004sp-client/releases", "info")
            return

        name = asset["name"]
        url  = asset["browser_download_url"]
        install_dir.mkdir(parents=True, exist_ok=True)
        dest = install_dir / name

        log(f"Downloading {name}...", "step")
        dl    = requests.get(url, stream=True, timeout=120)
        dl.raise_for_status()
        total = int(dl.headers.get("content-length", 0))
        done  = 0

        with open(str(dest), "wb") as fh:
            for chunk in dl.iter_content(65536):
                fh.write(chunk)
                done += len(chunk)
                if total:
                    with _state_lock:
                        _state["dl_progress"] = done / total

        with _state_lock:
            _state["dl_progress"] = 1.0
        log(f"Client saved: {name}", "ok")

    except Exception as e:
        log(f"Client download failed: {e}", "err")

def op_install_server(branch: str) -> None:
    install_dir = Path(_state["install_dir"])
    log(f"── Install Server  branch={branch}  →  {install_dir} ──", "step")
    if _do_server_install(branch, install_dir):
        log("── Server Installation Complete ──", "ok")
    else:
        log("── Server Installation Failed ──", "err")

def op_install_client() -> None:
    _do_client_install(Path(_state["install_dir"]))

def _do_extra_content(install_dir: Path) -> None:
    log("── Extra Content ──", "step")
    if not install_dir.exists():
        log("No install found. Run Install first.", "err")
        return
    extras_tmp = install_dir / "_extras_tmp"
    if git_clone_or_pull(REPO_EXTRAS, extras_tmp):
        extras_content = extras_tmp / "content"
        if extras_content.exists():
            overlay_files(extras_content, install_dir / "content")
        env_path = install_dir / "engine" / ".env"
        if env_path.exists():
            patch_env(env_path, {"NODE_CLIENT_ROUTEFINDER": "false", "BUILD_VERIFY": "false"})
    log("── Extra Content Complete ──", "ok")

def op_extra_content() -> None:
    _do_extra_content(Path(_state["install_dir"]))

def op_install_all(branch: str) -> None:
    install_dir = Path(_state["install_dir"])
    log(f"── Install All  branch={branch} ──", "step")
    if not _do_server_install(branch, install_dir):
        log("── Install All aborted (server install failed) ──", "err")
        return

    if _confirm("Download the 2004sp client for your platform?",
                "Install Client"):
        _do_client_install(install_dir)
    else:
        log("Skipping client install.", "info")

    if _confirm("Install Extra Content? (additional items, capes, etc.)",
                "Extra Content"):
        _do_extra_content(install_dir)
    else:
        log("Skipping extra content.", "info")

    log("── Install All Complete ──", "ok")

def op_update_server(branch: str) -> None:
    install_dir = Path(_state["install_dir"])
    lc_branch   = _lostcity_branch(branch)
    log(f"── Update Server  branch={branch}  lc={lc_branch} ──", "step")

    if not (install_dir / "engine" / ".git").exists():
        log("No existing install found. Run Install first.", "err")
        return

    prog_tmp = install_dir / "_progressive_tmp"
    if (prog_tmp / ".git").exists():
        log("Checking progressive for updates...", "step")
        try:
            cur = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(prog_tmp), text=True, stderr=subprocess.DEVNULL
            ).strip()
            if cur != branch:
                log(f"Switching branch {cur} → {branch}", "info")
                stream_cmd(["git", "fetch", "origin", branch], prog_tmp)
                stream_cmd(["git", "checkout", branch], prog_tmp)
        except Exception:
            pass

        before = _git_head(prog_tmp)
        stream_cmd(["git", "pull", "origin", branch], prog_tmp)
        after = _git_head(prog_tmp)

        if before != after:
            log("Changes detected — re-overlaying...", "step")
            overlay_files(prog_tmp, install_dir)
        else:
            log("Progressive already up to date.", "ok")
    else:
        log("Progressive repo not found — skipping bot overlay.", "warn")

    git_clone_or_pull(REPO_ENGINE,  install_dir / "engine")
    git_clone_or_pull(REPO_CONTENT, install_dir / "content")

    log("── Reinstalling Dependencies ──", "step")
    npm_install(install_dir, "root")
    npm_install(install_dir / "engine", "engine/")

    if _confirm("Update the 2004sp client too?", "Update Client"):
        _do_client_install(install_dir)
    else:
        log("Skipping client update.", "info")

    if _confirm("Update Extra Content too?", "Update Extra Content"):
        _do_extra_content(install_dir)
    else:
        log("Skipping extra content update.", "info")

    log("── Update Complete ──", "ok")

# ─────────────────────────────────────────────────────────────────────────────
#  Launcher operations
# ─────────────────────────────────────────────────────────────────────────────

NPM_SCRIPTS: dict = {
    "start":      ("Start Server",         None),   # new terminal
    "quickstart": ("Quickstart",           None),   # new terminal
    "dev":        ("Dev Mode",             None),   # new terminal
    "friend":     ("Friend Server",        None),   # new terminal
    "logger":     ("Logger",               None),   # new terminal
    "login":      ("Login Server",         None),   # new terminal
    "hiscores":   ("Hiscores",             True),   # background, tracked
    "build":      ("Build",                False),  # tracked (exits when done)
    "clean":      ("Clean",                False),  # tracked (exits when done)
    "setup":      ("Setup (interactive)",  None),   # new terminal
}

def launch_npm(name: str) -> None:
    engine_dir = get_engine_dir()
    if not engine_dir:
        log("Engine dir not found — check install dir in sidebar.", "err")
        return

    label, detached = NPM_SCRIPTS.get(name, (name, False))

    if detached is None:
        _open_new_terminal(["npm", "run", name], engine_dir)
        return

    if name in _state["running_procs"]:
        log(f"{label} is already running.", "warn")
        return

    log(f"Starting {label}...", "step")
    try:
        proc = subprocess.Popen(
            ["npm", "run", name],
            cwd=str(engine_dir),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, shell=_is_win(),
            encoding="utf-8", errors="replace",
        )
        _state["running_procs"][name] = proc
        log(f"{label} started (PID {proc.pid})", "ok")

        def _monitor() -> None:
            for line in proc.stdout:
                stripped = line.rstrip()
                if stripped:
                    log(f"[{name}] {stripped}", "dim")
            proc.wait()
            log(f"{label} stopped (exit {proc.returncode})", "info")
            _state["running_procs"].pop(name, None)

        threading.Thread(target=_monitor, daemon=True).start()
    except Exception as e:
        log(f"Failed to start {label}: {e}", "err")

def stop_proc(name: str) -> None:
    proc = _state["running_procs"].get(name)
    if not proc:
        label = NPM_SCRIPTS.get(name, (name,))[0]
        log(f"{label} is not running.", "warn")
        return
    try:
        proc.terminate()
        _state["running_procs"].pop(name, None)
        log(f"{NPM_SCRIPTS.get(name, (name,))[0]} stopped.", "info")
    except Exception as e:
        log(f"Could not stop {name}: {e}", "err")

def _open_new_terminal(cmd: list, cwd: Path) -> None:
    cmd_str = " ".join(str(c) for c in cmd)
    system = platform.system()
    try:
        if system == "Windows":
            subprocess.Popen(
                ["cmd", "/k", cmd_str], cwd=str(cwd),
                creationflags=subprocess.CREATE_NEW_CONSOLE
            )
        elif system == "Darwin":
            script = f'tell application "Terminal" to do script "cd {cwd!s} && {cmd_str}"'
            subprocess.Popen(["osascript", "-e", script])
        else:
            for term in ["xterm", "gnome-terminal", "konsole", "xfce4-terminal", "lxterminal"]:
                try:
                    subprocess.Popen([term, "-e", f"bash -c 'cd {cwd!s} && {cmd_str}; exec bash'"])
                    return
                except FileNotFoundError:
                    continue
            log("No terminal emulator found (tried xterm, gnome-terminal, konsole).", "err")
    except Exception as e:
        log(f"Could not open terminal: {e}", "err")

# ─────────────────────────────────────────────────────────────────────────────
#  Account tools
# ─────────────────────────────────────────────────────────────────────────────

def tool_import_character(username: str, password: str) -> None:
    engine_dir = get_engine_dir()
    if not engine_dir:
        log("Engine dir not found.", "err")
        return

    username = username.strip().lower()
    password = password.strip()
    if not username or not password:
        log("Username and password are required.", "err")
        return

    sav_path = engine_dir / "data" / "players" / "main" / f"{username}.sav"
    if not sav_path.exists():
        log(f".sav not found: {sav_path}", "err")
        log("Place your .sav in engine/data/players/main/ first.", "info")
        return

    log(f"Importing character '{username}'...", "step")
    try:
        hashed = bcrypt.hashpw(password.lower().encode(), bcrypt.gensalt(10)).decode()
        con = sqlite3.connect(str(engine_dir / "db.sqlite"))
        try:
            con.execute(
                "INSERT INTO account (username, password, registration_ip, registration_date)"
                " VALUES (?, ?, ?, datetime('now'))",
                (username, hashed, "127.0.0.1"),
            )
            con.commit()
            log(f"Account created — {username} can now log in.", "ok")
        except sqlite3.IntegrityError:
            con.execute(
                "UPDATE account SET password = ? WHERE username = ?",
                (hashed, username),
            )
            con.commit()
            log(f"Password updated — {username} can now log in.", "ok")
        finally:
            con.close()
    except Exception as e:
        log(f"Error: {e}", "err")

def tool_change_password(username: str, new_password: str) -> None:
    engine_dir = get_engine_dir()
    if not engine_dir:
        log("Engine dir not found.", "err")
        return

    username     = username.strip().lower()
    new_password = new_password.strip()
    if not username or not new_password:
        log("Username and new password are required.", "err")
        return

    sav_path = engine_dir / "data" / "players" / "main" / f"{username}.sav"
    if not sav_path.exists():
        log(f"No .sav for '{username}' — cannot verify ownership.", "err")
        return

    log(f"Changing password for '{username}'...", "step")
    try:
        hashed = bcrypt.hashpw(new_password.lower().encode(), bcrypt.gensalt(10)).decode()
        con = sqlite3.connect(str(engine_dir / "db.sqlite"))
        try:
            cur = con.execute(
                "UPDATE account SET password = ? WHERE username = ?",
                (hashed, username),
            )
            con.commit()
            if cur.rowcount == 0:
                log(f"No account for '{username}'. Use Import Character first.", "warn")
            else:
                log(f"Password updated — {username} can now log in.", "ok")
        finally:
            con.close()
    except Exception as e:
        log(f"Error: {e}", "err")

# ─────────────────────────────────────────────────────────────────────────────
#  Theme
# ─────────────────────────────────────────────────────────────────────────────

def _build_global_theme() -> int:
    with dpg.theme() as theme:
        with dpg.theme_component(dpg.mvAll):
            dpg.add_theme_color(dpg.mvThemeCol_WindowBg,           C_BG)
            dpg.add_theme_color(dpg.mvThemeCol_ChildBg,            C_PANEL)
            dpg.add_theme_color(dpg.mvThemeCol_PopupBg,            C_PANEL)
            dpg.add_theme_color(dpg.mvThemeCol_Border,             C_BORDER)
            dpg.add_theme_color(dpg.mvThemeCol_BorderShadow,       (0, 0, 0, 0))
            dpg.add_theme_color(dpg.mvThemeCol_FrameBg,            (25, 25, 40, 255))
            dpg.add_theme_color(dpg.mvThemeCol_FrameBgHovered,     (35, 35, 55, 255))
            dpg.add_theme_color(dpg.mvThemeCol_FrameBgActive,      (45, 45, 70, 255))
            dpg.add_theme_color(dpg.mvThemeCol_TitleBg,            C_BG)
            dpg.add_theme_color(dpg.mvThemeCol_TitleBgActive,      C_BG)
            dpg.add_theme_color(dpg.mvThemeCol_ScrollbarBg,        C_BG)
            dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrab,      (50, 50, 80, 255))
            dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrabHovered,(70, 70, 110, 255))
            dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrabActive, (90, 90, 140, 255))
            dpg.add_theme_color(dpg.mvThemeCol_CheckMark,          C_CYAN)
            dpg.add_theme_color(dpg.mvThemeCol_SliderGrab,         C_CYAN)
            dpg.add_theme_color(dpg.mvThemeCol_SliderGrabActive,   C_MAGENTA)
            dpg.add_theme_color(dpg.mvThemeCol_Button,             C_BTN)
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered,      C_BTN_HOV)
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive,       C_BTN_ACT)
            dpg.add_theme_color(dpg.mvThemeCol_Header,             (40, 40, 65, 255))
            dpg.add_theme_color(dpg.mvThemeCol_HeaderHovered,      (55, 55, 85, 255))
            dpg.add_theme_color(dpg.mvThemeCol_HeaderActive,       (70, 70, 105, 255))
            dpg.add_theme_color(dpg.mvThemeCol_Separator,          C_BORDER)
            dpg.add_theme_color(dpg.mvThemeCol_SeparatorHovered,   C_MAGENTA)
            dpg.add_theme_color(dpg.mvThemeCol_Tab,                (22, 22, 36, 255))
            dpg.add_theme_color(dpg.mvThemeCol_TabHovered,         (40, 40, 65, 255))
            dpg.add_theme_color(dpg.mvThemeCol_TabActive,          (30, 30, 55, 255))
            dpg.add_theme_color(dpg.mvThemeCol_TabUnfocused,       (18, 18, 30, 255))
            dpg.add_theme_color(dpg.mvThemeCol_TabUnfocusedActive, (25, 25, 45, 255))
            dpg.add_theme_color(dpg.mvThemeCol_TextSelectedBg,     (0, 229, 255, 80))
            dpg.add_theme_color(dpg.mvThemeCol_Text,               C_TEXT)
            dpg.add_theme_style(dpg.mvStyleVar_WindowRounding,     0)
            dpg.add_theme_style(dpg.mvStyleVar_ChildRounding,      4)
            dpg.add_theme_style(dpg.mvStyleVar_FrameRounding,      4)
            dpg.add_theme_style(dpg.mvStyleVar_PopupRounding,      4)
            dpg.add_theme_style(dpg.mvStyleVar_ScrollbarRounding,  4)
            dpg.add_theme_style(dpg.mvStyleVar_GrabRounding,       4)
            dpg.add_theme_style(dpg.mvStyleVar_TabRounding,        4)
            dpg.add_theme_style(dpg.mvStyleVar_WindowPadding,      12, 12)
            dpg.add_theme_style(dpg.mvStyleVar_FramePadding,       8, 5)
            dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing,        8, 7)
            dpg.add_theme_style(dpg.mvStyleVar_ItemInnerSpacing,   6, 6)
            dpg.add_theme_style(dpg.mvStyleVar_IndentSpacing,      20)
            dpg.add_theme_style(dpg.mvStyleVar_ScrollbarSize,      12)
            dpg.add_theme_style(dpg.mvStyleVar_GrabMinSize,        10)
            dpg.add_theme_style(dpg.mvStyleVar_WindowBorderSize,   1)
            dpg.add_theme_style(dpg.mvStyleVar_ChildBorderSize,    1)
            dpg.add_theme_style(dpg.mvStyleVar_FrameBorderSize,    1)
    return theme

def _btn_theme(bg: tuple, bg_hov: tuple, bg_act: tuple, text: tuple) -> int:
    with dpg.theme() as t:
        with dpg.theme_component(dpg.mvButton):
            dpg.add_theme_color(dpg.mvThemeCol_Button,        bg)
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, bg_hov)
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive,  bg_act)
            dpg.add_theme_color(dpg.mvThemeCol_Text,          text)
            dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 4)
    return t

# ─────────────────────────────────────────────────────────────────────────────
#  UI helpers
# ─────────────────────────────────────────────────────────────────────────────

def _section(label: str) -> None:
    dpg.add_spacer(height=8)
    dpg.add_text(label, color=C_MAGENTA)
    dpg.add_separator()
    dpg.add_spacer(height=5)

def _btn(label: str, cb, w: int, theme: int) -> int:
    b = dpg.add_button(label=label, callback=cb, width=w, height=36)
    dpg.bind_item_theme(b, theme)
    return b

def _link_btn(label: str, url: str, w: int, theme: int) -> int:
    b = dpg.add_button(label=label, callback=lambda: webbrowser.open(url), width=w, height=36)
    dpg.bind_item_theme(b, theme)
    return b

def _bg(fn, *args):
    """Fire and forget a function on a daemon thread."""
    threading.Thread(target=fn, args=args, daemon=True).start()

# ─────────────────────────────────────────────────────────────────────────────
#  Callbacks
# ─────────────────────────────────────────────────────────────────────────────

def _branch() -> str:
    if dpg.does_item_exist("branch_combo"):
        val = dpg.get_value("branch_combo")
        if val:
            return val
    with _state_lock:
        return _state.get("selected_branch", "dev")

def _on_branch_change(sender, value: str) -> None:
    with _state_lock:
        _state["selected_branch"] = value

def _on_dir_change(_, value: str) -> None:
    with _state_lock:
        _state["install_dir"]  = Path(value)
        _state["engine_dir"]   = None

def _cb_clear_log() -> None:
    for tag in list(_log_items):
        if dpg.does_item_exist(tag):
            dpg.delete_item(tag)
    _log_items.clear()

# ─────────────────────────────────────────────────────────────────────────────
#  Render callback (main thread, every frame)
# ─────────────────────────────────────────────────────────────────────────────

def _render() -> None:
    flush_log()
    _drain_confirm_queue()
    _drain_ui_updates()

    procs = _state["running_procs"]

    if dpg.does_item_exist("procs_status"):
        if procs:
            names = "\n".join(
                f"• {NPM_SCRIPTS.get(n, (n,))[0]}" for n in procs
            )
            dpg.set_value("procs_status", names)
            dpg.configure_item("procs_status", color=list(C_GREEN))
        else:
            dpg.set_value("procs_status", "(none)")
            dpg.configure_item("procs_status", color=list(C_DIM))

    # Download progress bar
    if dpg.does_item_exist("dl_progress"):
        prog = _state.get("dl_progress", 0.0)
        dpg.set_value("dl_progress", prog)
        if 0.0 < prog < 1.0:
            dpg.set_value("dl_label", f"Downloading... {prog*100:.0f}%")
        elif prog >= 1.0:
            dpg.set_value("dl_label", "Download complete.")
        else:
            dpg.set_value("dl_label", "")

# ─────────────────────────────────────────────────────────────────────────────
#  UI construction
# ─────────────────────────────────────────────────────────────────────────────

SIDEBAR_W    = 230
TAB_CONTENT_H= 410
LOG_H        = 195
BTN_W        = 210

def build_ui() -> None:
    # Button themes
    t_cyan    = _btn_theme((0,75,105,255),    (0,110,155,255),  (0,165,215,255),  C_CYAN)
    t_green   = _btn_theme((28,65,28,255),    (45,95,45,255),   (65,130,65,255),  C_GREEN)
    t_red     = _btn_theme((75,22,22,255),    (105,38,38,255),  (140,55,55,255),  C_RED)
    t_magenta = _btn_theme((58,22,72,255),    (82,38,102,255),  (108,58,138,255), C_MAGENTA)
    t_yellow  = _btn_theme((70,55,10,255),    (100,80,15,255),  (140,115,25,255), C_YELLOW)

    # Logo texture
    logo_loaded = False
    logo_path   = resource_path("logo.png")
    if Path(logo_path).exists():
        try:
            lw, lh, _, ldata = dpg.load_image(logo_path)
            with dpg.texture_registry():
                dpg.add_static_texture(lw, lh, ldata, tag="logo_tex")
            logo_loaded = True
        except Exception:
            pass

    with dpg.window(tag="main_win", no_title_bar=True, no_move=True, no_resize=True,
                    no_scrollbar=True, no_scroll_with_mouse=True):

        # ── Header ───────────────────────────────────────────────────────────
        with dpg.group(horizontal=True):
            if logo_loaded:
                dpg.add_image("logo_tex", width=58, height=58)
                dpg.add_spacer(width=14)
            with dpg.group():
                dpg.add_spacer(height=5)
                dpg.add_text(APP_TITLE, color=C_CYAN)
                dpg.add_text(
                    f"LostCity RS 254 + 2004sp  ·  v{APP_VERSION}",
                    color=C_DIM,
                )
        dpg.add_separator()
        dpg.add_spacer(height=5)

        # ── Body ─────────────────────────────────────────────────────────────
        with dpg.group(horizontal=True):

            # ─── Sidebar ─────────────────────────────────────────────────────
            with dpg.child_window(tag="sidebar", width=SIDEBAR_W, border=True):

                dpg.add_text("Install Directory", color=C_MAGENTA)
                dpg.add_separator()
                dpg.add_spacer(height=4)
                dpg.add_input_text(
                    tag="install_dir_input",
                    default_value=str(INSTALL_DIR_DEFAULT),
                    width=SIDEBAR_W - 24,
                    hint="path/to/install",
                    callback=_on_dir_change,
                )

                dpg.add_spacer(height=12)
                dpg.add_text("Running Processes", color=C_MAGENTA)
                dpg.add_separator()
                dpg.add_spacer(height=4)
                dpg.add_text("(none)", tag="procs_status", color=C_DIM,
                             wrap=SIDEBAR_W - 24)

                dpg.add_spacer(height=12)
                dpg.add_text("Download Progress", color=C_MAGENTA)
                dpg.add_separator()
                dpg.add_spacer(height=4)
                dpg.add_progress_bar(tag="dl_progress", default_value=0.0,
                                     width=SIDEBAR_W - 24)
                dpg.add_text("", tag="dl_label", color=C_DIM, wrap=SIDEBAR_W - 24)

                dpg.add_spacer(height=12)
                dpg.add_text("Prerequisites", color=C_MAGENTA)
                dpg.add_separator()
                dpg.add_spacer(height=4)
                b = dpg.add_button(
                    label="Check Prereqs",
                    width=SIDEBAR_W - 24, height=34,
                    callback=lambda: _bg(op_check_prereqs),
                )
                dpg.bind_item_theme(b, t_cyan)

            # ─── Tabs + log ──────────────────────────────────────────────────
            with dpg.group():
                content_w = WIN_W - SIDEBAR_W - 48

                with dpg.tab_bar(tag="main_tabs"):

                    # ── INSTALL TAB ───────────────────────────────────────────
                    with dpg.tab(label="   Install   "):
                        with dpg.child_window(width=content_w, height=TAB_CONTENT_H,
                                              border=False):
                            _section("Branch")
                            with dpg.group(horizontal=True):
                                dpg.add_combo(
                                    tag="branch_combo",
                                    items=["dev"],
                                    default_value="dev",
                                    width=240,
                                    callback=_on_branch_change,
                                )
                                dpg.add_spacer(width=8)
                                b_refresh = dpg.add_button(
                                    label="↻  Refresh",
                                    callback=lambda: _bg(_fetch_progressive_branches),
                                    height=28,
                                )
                                dpg.bind_item_theme(b_refresh, t_cyan)
                            dpg.add_spacer(height=3)
                            dpg.add_text(
                                "Branches are fetched live from 2004sp-progressive. "
                                "If a branch contains '274', the LostCity 274 base is used; "
                                "otherwise 254.",
                                color=C_DIM, wrap=content_w - 20,
                            )

                            _section("Install")
                            with dpg.group(horizontal=True):
                                _btn("▶  Install Server",
                                     lambda: _bg(op_install_server, _branch()),
                                     BTN_W, t_green)
                                dpg.add_spacer(width=8)
                                _btn("★  Install All",
                                     lambda: _bg(op_install_all, _branch()),
                                     BTN_W, t_green)
                            dpg.add_spacer(height=4)
                            dpg.add_text(
                                "Install All prompts you for client and extra content.",
                                color=C_DIM,
                            )

                            _section("Clients")
                            _btn("▶  Install Client",
                                 lambda: _bg(op_install_client),
                                 BTN_W, t_cyan)
                            dpg.add_spacer(height=3)
                            dpg.add_text(
                                "Downloads the 2004sp native client for your platform.",
                                color=C_DIM,
                            )

                            _section("Content, Update & Setup")
                            with dpg.group(horizontal=True):
                                _btn("+  Extra Content",
                                     lambda: _bg(op_extra_content),
                                     BTN_W, t_magenta)
                                dpg.add_spacer(width=8)
                                _btn("↺  Update",
                                     lambda: _bg(op_update_server, _branch()),
                                     BTN_W, t_cyan)
                            dpg.add_spacer(height=4)
                            dpg.add_text(
                                "Update pulls the latest commits, reinstalls deps, then prompts\n"
                                "for client and extra content updates.",
                                color=C_DIM,
                            )
                            dpg.add_spacer(height=8)
                            _btn("⚙  Setup (interactive)",
                                 lambda: _bg(launch_npm, "setup"),
                                 BTN_W, t_magenta)
                            dpg.add_spacer(height=3)
                            dpg.add_text("Setup opens in a new terminal window.", color=C_DIM)

                    # ── LAUNCH TAB ────────────────────────────────────────────
                    with dpg.tab(label="   Launch   "):
                        with dpg.child_window(width=content_w, height=TAB_CONTENT_H,
                                              border=False):
                            _section("Client")
                            _btn("▶  Launch 2004sp Client",
                                 lambda: _bg(op_launch_client),
                                 BTN_W, t_green)
                            dpg.add_spacer(height=3)
                            dpg.add_text(
                                "Launches the installed 2004sp client from the install directory.",
                                color=C_DIM,
                            )

                            _section("Server")
                            with dpg.group(horizontal=True):
                                _btn("▶  Start Server",
                                     lambda: _bg(launch_npm, "start"),
                                     BTN_W, t_green)
                                dpg.add_spacer(width=8)
                                _btn("⚡  Quickstart",
                                     lambda: _bg(launch_npm, "quickstart"),
                                     BTN_W, t_cyan)
                            dpg.add_spacer(height=6)
                            with dpg.group(horizontal=True):
                                _btn("★  Server + Hiscores",
                                     lambda: (_bg(launch_npm, "start"),
                                              _bg(launch_npm, "hiscores")),
                                     BTN_W, t_green)
                                dpg.add_spacer(width=8)
                                _btn("⟳  Dev Mode",
                                     lambda: _bg(launch_npm, "dev"),
                                     BTN_W, t_cyan)

                            _section("Services")
                            with dpg.group(horizontal=True):
                                _btn("▶  Run Hiscores",
                                     lambda: _bg(launch_npm, "hiscores"),
                                     BTN_W, t_cyan)
                                dpg.add_spacer(width=8)
                                _btn("■  Stop Hiscores",
                                     lambda: stop_proc("hiscores"),
                                     BTN_W, t_red)
                            dpg.add_spacer(height=6)
                            with dpg.group(horizontal=True):
                                _btn("▶  Friend Server",
                                     lambda: _bg(launch_npm, "friend"),
                                     BTN_W, t_cyan)
                                dpg.add_spacer(width=8)
                                _btn("▶  Logger",
                                     lambda: _bg(launch_npm, "logger"),
                                     BTN_W, t_cyan)
                            dpg.add_spacer(height=6)
                            _btn("▶  Login Server",
                                 lambda: _bg(launch_npm, "login"),
                                 BTN_W, t_cyan)

                            _section("Build & Maintenance")
                            with dpg.group(horizontal=True):
                                _btn("⚙  Build",
                                     lambda: _bg(launch_npm, "build"),
                                     BTN_W, t_magenta)
                                dpg.add_spacer(width=8)
                                _btn("🗑  Clean",
                                     lambda: _bg(launch_npm, "clean"),
                                     BTN_W, t_red)

                    # ── TOOLS TAB ─────────────────────────────────────────────
                    with dpg.tab(label="   Tools   "):
                        with dpg.child_window(width=content_w, height=TAB_CONTENT_H,
                                              border=False):
                            _section("Environment Config")
                            _btn("Patch .env", _cb_patch_env, BTN_W, t_cyan)
                            dpg.add_spacer(height=3)
                            dpg.add_text(
                                "Sets NODE_CLIENT_ROUTEFINDER=false and BUILD_VERIFY=false\n"
                                "in engine/.env — required for offline single-player mode.",
                                color=C_DIM,
                            )

                            _section("Import Character  (.sav → account)")
                            dpg.add_text(
                                "Place your .sav file in  engine/data/players/main/",
                                color=C_DIM,
                            )
                            dpg.add_spacer(height=6)
                            dpg.add_input_text(
                                label="Username##imp", tag="import_user",
                                width=280, hint="character name (no .sav extension)",
                            )
                            dpg.add_input_text(
                                label="Password##imp", tag="import_pass",
                                width=280, password=True,
                            )
                            dpg.add_spacer(height=6)
                            _btn("Import Character", _cb_import_char, BTN_W, t_green)

                            _section("Change Password")
                            dpg.add_text(
                                ".sav must be present in engine/data/players/main/ to verify ownership.",
                                color=C_DIM,
                            )
                            dpg.add_spacer(height=6)
                            dpg.add_input_text(
                                label="Username##chpw", tag="chpw_user",
                                width=280, hint="character name",
                            )
                            dpg.add_input_text(
                                label="New Password##chpw", tag="chpw_pass",
                                width=280, password=True,
                            )
                            dpg.add_spacer(height=6)
                            _btn("Change Password", _cb_change_pw, BTN_W, t_magenta)

                    # ── RESOURCES TAB ─────────────────────────────────────────
                    with dpg.tab(label="   Resources   "):
                        with dpg.child_window(width=content_w, height=TAB_CONTENT_H,
                                              border=False):
                            _section("2004sp")
                            dpg.add_text("Official Website", color=C_TEXT)
                            _link_btn("🌐  www.2004sp.cc",
                                      "https://www.2004sp.cc/",
                                      content_w - 24, t_cyan)
                            dpg.add_spacer(height=8)
                            dpg.add_text("Community Forum", color=C_TEXT)
                            _link_btn("💬  forum.2004sp.cc",
                                      "https://forum.2004sp.cc/",
                                      content_w - 24, t_cyan)

                            _section("GitHub")
                            _link_btn("🐙  github.com/2004sp",
                                      "https://github.com/2004sp",
                                      content_w - 24, t_cyan)

                            _section("Java Client")
                            dpg.add_text(
                                "The Progressive Java Client runs in any browser-independent environment.\n"
                                "Requires Java 17 or newer.",
                                color=C_DIM, wrap=content_w - 24,
                            )
                            dpg.add_spacer(height=6)
                            _link_btn("☕  Progressive Java Client — Releases",
                                      "https://github.com/2004sp/Progressive-Java-Client/releases",
                                      content_w - 24, t_yellow)
                            dpg.add_spacer(height=8)
                            dpg.add_text("Need Java 17+?", color=C_TEXT)
                            _link_btn("☕  adoptium.net  (Eclipse Temurin)",
                                      "https://adoptium.net",
                                      content_w - 24, t_yellow)

                # ── Log panel ─────────────────────────────────────────────────
                dpg.add_spacer(height=6)
                with dpg.group(horizontal=True):
                    dpg.add_text("Output Log", color=C_MAGENTA)
                    dpg.add_spacer(width=10)
                    clr = dpg.add_button(label="Clear", callback=_cb_clear_log,
                                         width=58, height=22)
                    dpg.bind_item_theme(clr, t_red)

                with dpg.child_window(tag="log_panel", width=content_w,
                                      height=LOG_H, border=True,
                                      horizontal_scrollbar=False):
                    pass  # items added by flush_log()

# ─────────────────────────────────────────────────────────────────────────────
#  Tool callbacks (defined after build_ui to avoid forward-ref issues)
# ─────────────────────────────────────────────────────────────────────────────

def _cb_patch_env() -> None:
    engine_dir = get_engine_dir()
    if not engine_dir:
        log("Engine dir not found — check install dir in sidebar.", "err")
        return
    env_path = engine_dir / ".env"
    if not env_path.exists():
        log(".env not found — install server first.", "err")
        return
    _bg(patch_env, env_path, {"NODE_CLIENT_ROUTEFINDER": "false", "BUILD_VERIFY": "false"})

def _cb_import_char() -> None:
    user = dpg.get_value("import_user")
    pw   = dpg.get_value("import_pass")
    _bg(tool_import_character, user, pw)

def _cb_change_pw() -> None:
    user = dpg.get_value("chpw_user")
    pw   = dpg.get_value("chpw_pass")
    _bg(tool_change_password, user, pw)

# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    dpg.create_context()

    global_theme = _build_global_theme()
    dpg.bind_theme(global_theme)

    build_ui()

    dpg.create_viewport(
        title=APP_TITLE,
        width=WIN_W, height=WIN_H,
        min_width=960, min_height=640,
    )
    dpg.setup_dearpygui()
    dpg.show_viewport()
    dpg.set_primary_window("main_win", True)

    log(f"{APP_TITLE} v{APP_VERSION} ready.", "ok")
    log("Set your install directory in the sidebar, then choose an action.", "info")

    # Fetch progressive branches in the background — combo updates via _ui_updates queue
    _bg(_fetch_progressive_branches)

    while dpg.is_dearpygui_running():
        _render()
        dpg.render_dearpygui_frame()

    dpg.destroy_context()


if __name__ == "__main__":
    main()
