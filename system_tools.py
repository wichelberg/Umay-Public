"""Tools for closing apps, running Python scripts, opening folders in VS Code,
and shutting down / restarting the PC.

Closing apps, running or stopping scripts and planning a shutdown are confirmed
first (pc_tools.confirm). Script runs and planned shutdowns happen in the
background and report back through the notifier of the channel (PC window or
Telegram) they were started from.

Shutdowns are forced (the owner chose this: when away, the PC must really turn
off even if an app has unsaved work), always preceded by a 60-second warning
that can be cancelled. Plans live in Umay's memory, so restarting Umay
drops them.
"""
import ctypes
import ctypes.wintypes as wintypes
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import psutil

import paths
import pc_tools

RUN_LOG_DIR = paths.app_dir() / "run_logs"
OUTPUT_TAIL_LINES = 15
CLOSE_WAIT_SECONDS = 10
SHUTDOWN_WARNING_SECONDS = 60
FORCE_SHUTDOWN_BY_DEFAULT = True

PROCESS_LIST_LIMIT = 20
END_WAIT_SECONDS = 5

# Never offered for closing: Windows itself, security software, and Umay.
PROTECTED_PROCESSES = {
    "explorer.exe", "umay.exe", "dwm.exe", "csrss.exe", "winlogon.exe", "lsass.exe", "wininit.exe",
    "services.exe", "svchost.exe", "smss.exe", "searchhost.exe", "startmenuexperiencehost.exe",
    "shellexperiencehost.exe", "textinputhost.exe", "applicationframehost.exe", "systemsettings.exe",
    "sihost.exe", "fontdrvhost.exe", "ctfmon.exe", "conhost.exe", "taskhostw.exe", "runtimebroker.exe",
    "msmpeng.exe", "nissrv.exe", "securityhealthservice.exe", "securityhealthsystray.exe",
}
# Python processes whose command line contains one of these are Umay's own plumbing.
PROTECTED_SCRIPTS = ("claude_comm",)

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_dwmapi = ctypes.windll.dwmapi

_runs = {}  # run id -> dict(proc, name, path, log, started, ended, code, done Event)
_run_lock = threading.Lock()
_power = {"plan": None}  # the single active shutdown plan, if any
cancel_hooks = []  # () -> bool: other shutdowns cancel_shutdown stops too (User Care's); True if one ran


class ToolError(Exception):
    pass


def _run_tool(fn):
    try:
        return fn()
    except (ToolError, pc_tools.ToolError) as e:
        return str(e)
    except OSError as e:
        return f"Failed: {e}"


def _duration(seconds):
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m {secs}s"


# --- open apps -------------------------------------------------------------------

def _exe_path(pid):
    """The process's program, its full path (None if it can't be read)."""
    handle = _kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    finally:
        _kernel32.CloseHandle(handle)


def _exe_name(pid):
    path = _exe_path(pid)
    return Path(path).name.lower() if path else None


def _is_cloaked(hwnd):
    """Suspended Store apps keep 'visible' but cloaked windows; they aren't really open."""
    cloaked = wintypes.DWORD()
    _dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))  # DWMWA_CLOAKED
    return bool(cloaked.value)


def _open_apps():
    """Apps with a visible main window: {exe: {"pids": set, "hwnds": [], "titles": []}}."""
    apps = {}
    own_pid = os.getpid()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        if not _user32.IsWindowVisible(hwnd) or _user32.GetWindow(hwnd, 4) or _is_cloaked(hwnd):
            return True
        length = _user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = _exe_name(pid.value)
        if not exe or exe in PROTECTED_PROCESSES or pid.value == own_pid:
            return True
        title = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, title, length + 1)
        app = apps.setdefault(exe, {"pids": set(), "hwnds": [], "titles": []})
        app["pids"].add(pid.value)
        app["hwnds"].append(hwnd)
        app["titles"].append(title.value)
        return True

    _user32.EnumWindows(visit, 0)
    return apps


def list_open_apps() -> str:
    apps = _open_apps()
    if not apps:
        return "No apps with open windows."
    return "\n".join(
        f"- {exe[:-4]}: " + " | ".join(t[:60] for t in app["titles"][:3])
        for exe, app in sorted(apps.items())
    )


def _match_apps(name, apps):
    query = pc_tools._normalize(name)
    if not query:
        return []
    matches = []
    for exe, app in apps.items():
        stem = pc_tools._normalize(exe[:-4])
        if query in stem or stem in query or any(query in pc_tools._normalize(t) for t in app["titles"]):
            matches.append(exe)
    return matches


BROWSERS = {"opera.exe", "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe"}  # "close YouTube" closed all of Opera


def close_app(name: str, force: bool = False) -> str:
    def run():
        apps = _open_apps()
        matches = _match_apps(name, apps)
        if not matches:
            raise ToolError(f"No open app matches '{name}'. Open apps:\n{list_open_apps()}")
        if len(matches) > 1:
            raise ToolError(
                "Several open apps match; ask the user which one and call again with its exact name: "
                + ", ".join(m[:-4] for m in matches)
            )
        exe = matches[0]
        app = apps[exe]
        query, stem = pc_tools._normalize(name), pc_tools._normalize(exe[:-4])
        if exe.lower() in BROWSERS and query not in stem and stem not in query:  # matched only a tab's title
            raise ToolError(f"'{name}' is a tab in {exe[:-4]}, not an app: closing the app would close every tab. "
                            "Use web_tabs to close that tab.")
        if force:
            if not pc_tools.confirm("confirm_force_close_app", app=exe[:-4]):
                return pc_tools._declined()
            subprocess.run(["taskkill", "/F", "/T", "/IM", exe], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
            return f"Force-closed {exe[:-4]}."

        titles = "\n".join(f"• {t[:80]}" for t in app["titles"][:5])
        if not pc_tools.confirm("confirm_close_app", app=exe[:-4], titles=titles):
            return pc_tools._declined()
        for hwnd in app["hwnds"]:
            _user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE: the app closes itself (and may ask to save)
        deadline = time.monotonic() + CLOSE_WAIT_SECONDS
        while time.monotonic() < deadline:
            time.sleep(0.5)
            if exe not in _open_apps():
                return f"Closed {exe[:-4]}."
        return (
            f"Asked {exe[:-4]} to close, but it still has a window open (maybe a 'save changes?' "
            "prompt). The user can answer it, or ask you to force-close it (unsaved work is lost)."
        )
    return _run_tool(run)


# --- processes (Task Manager) ---------------------------------------------------------

def _own_pids():
    """Umay's process, plus the PyInstaller bootloader that started it (same exe)."""
    me = psutil.Process()
    pids = {me.pid}
    try:
        parent = me.parent()
        if parent and parent.exe() == me.exe():
            pids.add(parent.pid)
    except psutil.Error:
        pass
    return pids


def _protected_reason(proc, own_pids, user):
    """Why `proc` may not be ended, or None. Only the user's own, non-system processes may."""
    if proc.pid in own_pids:
        return "it's Umay herself"
    try:
        name = proc.name().lower()
        if name in PROTECTED_PROCESSES:
            return "it's part of Windows or its security"
        if proc.username() != user:
            return "it belongs to another user or to Windows"
        if name.startswith("python") and any(s in " ".join(proc.cmdline()).lower() for s in PROTECTED_SCRIPTS):
            return "it's Umay's Telegram bridge"
    except psutil.AccessDenied:
        return "it belongs to another user or to Windows"
    return None


def _describe(proc):
    try:
        memory = proc.memory_info().rss / 2**20
        exe = proc.exe() or "?"
    except psutil.Error:
        memory, exe = 0, "?"
    return f"{proc.name()} (PID {proc.pid}) · {memory:.0f} MB · {exe}"


def list_processes(name: str = "") -> str:
    own, user = _own_pids(), psutil.Process().username()
    query = pc_tools._normalize(name)
    found = []
    for proc in psutil.process_iter():
        try:
            stem = proc.name()[:-4] if proc.name().lower().endswith(".exe") else proc.name()
            if query and query not in pc_tools._normalize(stem):
                continue
            if _protected_reason(proc, own, user):
                continue
            found.append((proc.memory_info().rss, proc))
        except psutil.Error:
            continue
    if not found:
        return f"No process of yours matches '{name}'." if name else "No processes found."
    found.sort(key=lambda item: item[0], reverse=True)
    lines = [f"- {_describe(proc)}" for _, proc in found[:PROCESS_LIST_LIMIT]]
    if len(found) > PROCESS_LIST_LIMIT:
        lines.append(f"(+{len(found) - PROCESS_LIST_LIMIT} more; use a more specific name)")
    return "\n".join(lines)


def end_process(pid: int) -> str:
    """Like Task Manager's End task: immediate, so for apps with a window close_app is gentler."""
    def run():
        try:
            proc = psutil.Process(pid)
        except psutil.NoSuchProcess:
            raise ToolError(f"There's no process with PID {pid} (it may have ended already).")
        reason = _protected_reason(proc, _own_pids(), psutil.Process().username())
        if reason:
            raise ToolError(f"Won't end PID {pid}: {reason}.")
        try:
            children = proc.children(recursive=True)
        except psutil.Error:
            children = []
        if not pc_tools.confirm("confirm_end_process", process=_describe(proc), children=len(children)):
            return pc_tools._declined()
        name = proc.name()
        alive = kill_tree(proc, children)
        if alive:
            return f"Sent the end signal, but {alive} of the processes are still running."
        extra = f" and {len(children)} child processes" if children else ""
        return f"Ended {name} (PID {pid}){extra}."
    return _run_tool(run)


def kill_tree(proc, children=None):
    """Ends `proc` and its child processes at once. Returns how many are still running after
    a short wait. psutil checks that each PID still belongs to the same process (start time)
    before killing it, so a reused PID is never hit."""
    if children is None:
        try:
            children = proc.children(recursive=True)
        except psutil.Error:
            children = []
    targets = children + [proc]
    for p in targets:
        try:
            p.kill()
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            raise ToolError(f"Windows denied ending {p.name()} (PID {p.pid}).")
    _, alive = psutil.wait_procs(targets, timeout=END_WAIT_SECONDS)
    return len(alive)


# --- Python scripts ----------------------------------------------------------------

def _python_command(script):
    """The project's virtualenv Python if there is one, else the py launcher."""
    for folder in [script.parent, *script.parent.parents]:
        for venv in (".venv", "venv", "env"):
            exe = folder / venv / "Scripts" / "python.exe"
            if exe.exists():
                return [str(exe)]
        if folder in pc_tools.ALLOWED_DIRS:
            break
    launcher = shutil.which("py")
    if launcher:
        return [launcher, "-3"]
    python = shutil.which("python")
    if python:
        return [python]
    raise ToolError("Python isn't installed (neither 'py' nor 'python' was found).")


def _tail(path, lines=OUTPUT_TAIL_LINES):
    try:
        text = path.read_text(encoding="utf-8", errors="replace").rstrip()
    except OSError:
        return "(no output)"
    return "\n".join(text.splitlines()[-lines:]) or "(no output)"


def _show_log_window(run_id, name, log_path):
    """Opens a console that follows the run's output live; the run itself stays tracked."""
    title = f"Umay run #{run_id}: {name}".replace("'", "")
    log = str(log_path).replace("'", "''")
    script = (
        f"$Host.UI.RawUI.WindowTitle = '{title}'; "
        f"Get-Content -LiteralPath '{log}' -Wait -Encoding UTF8"
    )
    subprocess.Popen(["powershell", "-NoProfile", "-NoExit", "-Command", script],
                     creationflags=subprocess.CREATE_NEW_CONSOLE)


def run_python(path: str, args: list = None, visible: bool = False) -> str:
    def run():
        script = pc_tools._resolve(path, allow_root=False)
        if not script.is_file() or script.suffix.lower() != ".py":
            raise ToolError(f"Not a Python file: {script}")
        arguments = [str(a) for a in (args or [])]
        command = _python_command(script) + [str(script)] + arguments
        if not pc_tools.confirm("confirm_run_python", path=script, args=" ".join(arguments)):
            return pc_tools._declined()

        RUN_LOG_DIR.mkdir(exist_ok=True)
        log_path = RUN_LOG_DIR / f"{script.stem}_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log"
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        with open(log_path, "wb") as log:
            proc = subprocess.Popen(
                command, cwd=script.parent, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                env=env, creationflags=subprocess.CREATE_NO_WINDOW,
            )
        with _run_lock:
            run_id = len(_runs) + 1
            _runs[run_id] = {
                "proc": proc, "name": script.name, "path": script, "log": log_path,
                "started": time.time(), "ended": None, "code": None, "done": threading.Event(),
            }
        notify = pc_tools.current_notifier()
        threading.Thread(target=_watch_run, args=(run_id, notify), daemon=True).start()
        if visible:
            _show_log_window(run_id, script.name, log_path)
        return (
            f"Started run #{run_id}: {script.name} with {Path(command[0]).name} (output log: {log_path}). "
            "It runs in the background; the user is notified automatically when it finishes."
        )
    return _run_tool(run)


def _watch_run(run_id, notify):
    info = _runs[run_id]
    info["code"] = info["proc"].wait()
    info["ended"] = time.time()
    key = "run_finished" if info["code"] == 0 else "run_failed"
    try:
        notify(key, run_id=run_id, name=info["name"], code=info["code"],
               duration=_duration(info["ended"] - info["started"]), tail=_tail(info["log"]))
    finally:
        info["done"].set()  # after notifying, so "run finished" arrives before any shutdown warning


def list_runs() -> str:
    if not _runs:
        return "No Python runs since Umay started."
    lines = []
    for run_id, info in _runs.items():
        if info["ended"] is None:
            state = f"running for {_duration(time.time() - info['started'])}"
        else:
            state = f"finished with exit code {info['code']} after {_duration(info['ended'] - info['started'])}"
        lines.append(f"#{run_id} {info['name']} ({info['path'].parent}): {state}")
    return "\n".join(lines)


def stop_run(run_id: int) -> str:
    def run():
        info = _runs.get(int(run_id))
        if info is None:
            raise ToolError(f"There's no run #{run_id}.\n{list_runs()}")
        if info["ended"] is not None:
            return f"Run #{run_id} already finished."
        if not pc_tools.confirm("confirm_stop_run", run_id=run_id, name=info["name"]):
            return pc_tools._declined()
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(info["proc"].pid)], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        return f"Stopped run #{run_id} ({info['name']})."
    return _run_tool(run)


# --- VS Code -------------------------------------------------------------------------

def _vscode_exe():
    candidates = []
    cli = shutil.which("code")  # ...\Microsoft VS Code\bin\code.cmd
    if cli:
        candidates.append(Path(cli).resolve().parent.parent / "Code.exe")
    candidates.append(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Microsoft VS Code" / "Code.exe")
    candidates.append(Path(os.environ.get("ProgramFiles", "")) / "Microsoft VS Code" / "Code.exe")
    for exe in candidates:
        if exe.is_file():
            return exe
    raise ToolError("Visual Studio Code isn't installed.")


def open_in_vscode(path: str) -> str:
    def run():
        target = pc_tools._resolve(path)
        if not target.exists():
            raise ToolError(f"Doesn't exist: {target}")
        subprocess.Popen([str(_vscode_exe()), str(target)])
        return f"Opened {target} in VS Code."
    return _run_tool(run)


def open_terminal(path: str) -> str:
    def run():
        folder = pc_tools._resolve(path)
        if folder.is_file():
            folder = folder.parent
        if not folder.is_dir():
            raise ToolError(f"Not a folder: {folder}")
        terminal = shutil.which("wt")
        if terminal:
            subprocess.Popen([terminal, "-d", str(folder)])
        else:
            subprocess.Popen(["cmd", "/K"], cwd=folder, creationflags=subprocess.CREATE_NEW_CONSOLE)
        return f"Opened a terminal in {folder}."
    return _run_tool(run)


# --- PC state -------------------------------------------------------------------------

class _LastInput(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def idle_seconds():
    """Seconds since the last keyboard/mouse input (used to guess whether the user is away)."""
    info = _LastInput(ctypes.sizeof(_LastInput), 0)
    if not _user32.GetLastInputInfo(ctypes.byref(info)):
        return 0
    return (_kernel32.GetTickCount() - info.dwTime) / 1000


def lock_pc() -> str:
    _user32.LockWorkStation()
    return "Locked the PC (Windows lock screen)."


def pc_status() -> str:
    memory = psutil.virtual_memory()
    lines = [
        f"CPU: {psutil.cpu_percent(interval=0.5):.0f}%",
        f"RAM: {memory.percent:.0f}% used ({memory.used / 1024 ** 3:.1f} of {memory.total / 1024 ** 3:.1f} GB)",
    ]
    for part in psutil.disk_partitions():
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        lines.append(f"Disk {part.mountpoint}: {usage.free / 1024 ** 3:.0f} GB free of {usage.total / 1024 ** 3:.0f} GB")
    uptime = time.time() - psutil.boot_time()
    lines.append(f"Up for {_duration(uptime)}; last keyboard/mouse input {_duration(idle_seconds())} ago")
    lines.append("Python runs: " + (list_runs() if _runs else "none"))
    lines.append("Shutdown: " + shutdown_status())
    return "\n".join(lines)


# --- shutdown / restart --------------------------------------------------------------

def schedule_shutdown(action: str = "shutdown", minutes: float = 0, after_run_id: int = 0, force: bool = None) -> str:
    def run():
        if action not in ("shutdown", "restart"):
            raise ToolError("action must be 'shutdown' or 'restart'.")
        forced = FORCE_SHUTDOWN_BY_DEFAULT if force is None else bool(force)
        keys = [f"power_q_{action}"]
        kwargs = {}
        when, run_info = None, None
        if after_run_id:
            run_info = _runs.get(int(after_run_id))
            if run_info is None:
                raise ToolError(f"There's no run #{after_run_id}.\n{list_runs()}")
            if run_info["ended"] is not None:
                raise ToolError(f"Run #{after_run_id} already finished; schedule with minutes instead.")
            keys.append("power_when_run")
            kwargs.update(run_id=after_run_id, name=run_info["name"])
        elif minutes and float(minutes) > 0:
            when = datetime.now() + timedelta(minutes=float(minutes))
            keys.append("power_when_at")
            kwargs.update(time=when.strftime("%H:%M"), minutes=round(float(minutes)))
        else:
            keys.append("power_when_now")
        if forced:
            keys.append("power_force_note")
        if not pc_tools.confirm(keys, **kwargs):
            return pc_tools._declined()

        _cancel_plan()
        plan = {"action": action, "forced": forced, "when": when, "run_id": after_run_id or None,
                "cancel": threading.Event()}
        _power["plan"] = plan
        notify = pc_tools.current_notifier()
        threading.Thread(target=_carry_out, args=(plan, run_info, notify), daemon=True).start()
        if run_info:
            return f"Planned: {action} after run #{after_run_id} ({run_info['name']}) finishes, with a 60 s warning."
        if when:
            return f"Planned: {action} at {when:%H:%M}, with a 60 s warning before."
        return f"{action.capitalize()} starts in 60 seconds (the user was warned and can cancel)."
    return _run_tool(run)


def _carry_out(plan, run_info, notify):
    cancel = plan["cancel"]
    if run_info is not None:
        while not run_info["done"].wait(1):
            if cancel.is_set():
                return
    elif plan["when"] is not None:
        if cancel.wait(max(0.0, (plan["when"] - datetime.now()).total_seconds())):
            return
    if cancel.is_set():
        return
    notify(f"power_warning_{plan['action']}")
    if cancel.wait(SHUTDOWN_WARNING_SECONDS):
        return
    flag = "/s" if plan["action"] == "shutdown" else "/r"
    command = ["shutdown", flag, "/t", "0"] + (["/f"] if plan["forced"] else [])
    subprocess.run(command, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)


def _cancel_plan():
    plan, _power["plan"] = _power["plan"], None
    if plan is not None:
        plan["cancel"].set()
    return plan


def cancel_shutdown() -> str:
    """Cancels Umay's plan, User Care's shutdown (cancel_hooks) and any shutdown Windows is already
    counting down (e.g. one planned by a Claude Code session in sleep mode)."""
    plan = _cancel_plan()
    hooked = [hook() for hook in cancel_hooks]
    aborted = subprocess.run(["shutdown", "/a"], capture_output=True,
                             creationflags=subprocess.CREATE_NO_WINDOW).returncode == 0
    return ("Cancelled the planned shutdown; the PC stays on." if plan or aborted or any(hooked)
            else "No shutdown was planned.")


def shutdown_status() -> str:
    plan = _power["plan"]
    if plan is None or plan["cancel"].is_set():
        return "No shutdown is planned."
    if plan["run_id"]:
        return f"{plan['action']} planned after run #{plan['run_id']} finishes."
    if plan["when"]:
        return f"{plan['action']} planned at {plan['when']:%H:%M}."
    return f"{plan['action']} is about to happen (warning period)."


# --- tool schemas --------------------------------------------------------------------

def _tool(name, description, properties, required):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


_CONFIRM_NOTE = " Asks the user first; if they decline, don't retry."

TOOLS = [
    _tool(
        "list_open_apps",
        "Lists the apps that currently have an open window on the PC.",
        {}, [],
    ),
    _tool(
        "close_app",
        "Closes an open app (e.g. 'Opera GX', 'Spotify') the normal way, as if its window's X was clicked, "
        "so it can ask to save. Use directly for 'close X'. force=true only if the user asks to force-close it."
        + _CONFIRM_NOTE,
        {
            "name": {"type": "string", "description": "App name or a word from its window title."},
            "force": {"type": "boolean", "description": "Kill it immediately (unsaved work is lost)."},
        },
        ["name"],
    ),
    _tool(
        "list_processes",
        "Lists the user's running processes like Task Manager does, including ones without a window "
        "(background apps, stuck scripts), biggest RAM first, with their PID. Windows, security software and "
        "Umay herself are never listed.",
        {"name": {"type": "string", "description": "Optional part of the process name (e.g. 'python', 'node')."}},
        [],
    ),
    _tool(
        "end_process",
        "Ends a process and its child processes immediately by PID, like Task Manager's End task (unsaved work "
        "is lost). Get the PID from list_processes first; if several match, ask the user which one. For an app "
        "with a window prefer close_app, which closes it gently." + _CONFIRM_NOTE,
        {"pid": {"type": "integer", "description": "Process ID from list_processes."}},
        ["pid"],
    ),
    _tool(
        "run_python",
        "Runs a Python (.py) file in the background with the project's virtualenv if it has one, else the "
        "default Python. Returns a run number; the user is notified with the output when it finishes."
        + pc_tools._PATH_NOTE + _CONFIRM_NOTE,
        {
            "path": {"type": "string", "description": "Absolute path of the .py file."},
            "args": {"type": "array", "items": {"type": "string"}, "description": "Optional command-line arguments."},
            "visible": {"type": "boolean", "description": "Also open a window showing the output live (when the user wants to see it on screen)."},
        },
        ["path"],
    ),
    _tool(
        "list_runs",
        "Lists the Python runs started since Umay started, with their number and whether they're still running.",
        {}, [],
    ),
    _tool(
        "stop_run",
        "Stops a running Python run by its number." + _CONFIRM_NOTE,
        {"run_id": {"type": "integer", "description": "The run number."}},
        ["run_id"],
    ),
    _tool(
        "open_in_vscode",
        "Opens a folder (project) or file in Visual Studio Code." + pc_tools._PATH_NOTE,
        {"path": {"type": "string", "description": "Absolute folder or file path."}},
        ["path"],
    ),
    _tool(
        "open_terminal",
        "Opens a terminal window (Windows Terminal or cmd) in a folder on the PC." + pc_tools._PATH_NOTE,
        {"path": {"type": "string", "description": "Absolute folder path."}},
        ["path"],
    ),
    _tool(
        "lock_pc",
        "Locks the PC (Windows lock screen). Nothing is closed.",
        {}, [],
    ),
    _tool(
        "pc_status",
        "Shows the PC's state: CPU, RAM, free disk space, uptime, time since last input, Umay's runs and shutdown "
        "plan. Only when the user asks about these.",
        {}, [],
    ),
    _tool(
        "schedule_shutdown",
        "Shuts down or restarts the PC: now (minutes=0), after some minutes (e.g. 'in 2 hours' = 120), or when a "
        "Python run finishes (after_run_id). There's always a 60-second warning the user can cancel. Shutdown is "
        "forced by default (unsaved work is lost); set force=false only if the user asks not to force it. Planning "
        "a new one replaces the previous plan." + _CONFIRM_NOTE,
        {
            "action": {"type": "string", "enum": ["shutdown", "restart"]},
            "minutes": {"type": "number", "description": "Delay in minutes; 0 = now."},
            "after_run_id": {"type": "integer", "description": "Shut down when this run finishes instead."},
            "force": {"type": "boolean", "description": "Force-close apps with unsaved work (default true)."},
        },
        ["action"],
    ),
    _tool(
        "cancel_shutdown",
        "Cancels a planned or counting-down shutdown/restart. Use whenever the user says to cancel or stop it.",
        {}, [],
    ),
    _tool(
        "shutdown_status",
        "Tells whether a shutdown/restart is planned and when.",
        {}, [],
    ),
]

TOOL_FUNCTIONS = {
    "list_open_apps": list_open_apps,
    "close_app": close_app,
    "list_processes": list_processes,
    "end_process": end_process,
    "run_python": run_python,
    "list_runs": list_runs,
    "stop_run": stop_run,
    "open_in_vscode": open_in_vscode,
    "open_terminal": open_terminal,
    "lock_pc": lock_pc,
    "pc_status": pc_status,
    "schedule_shutdown": schedule_shutdown,
    "cancel_shutdown": cancel_shutdown,
    "shutdown_status": shutdown_status,
}
