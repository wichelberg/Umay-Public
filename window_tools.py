"""Windows of open apps: to the front, minimized, maximized, restored, or moved to another monitor (plan 26).

Why: Umay had no such tool, so a plan pressed Win+Shift+arrow four times and said done while Opera GX stayed
where it was (2026-11-06). Those keys toggle between two monitors, and nothing looked at the result.

- window_control is the tool. Every action reads the window back and says it failed if the window didn't
  change, so a "done" here is a fact. A window running as administrator can refuse: Windows blocks a normal
  program from moving it, and then the tool says so.
- start_and_wait starts an app (pc_tools.launch_application) and waits for its window. It reports the Windows
  administrator question (UAC) when that is what stopped it. Nothing here can press Yes: that screen is
  closed to programs on purpose, and only the owner answers it.
- asked / shortcut: "move Opera GX to the other monitor", "Spotify'ı küçült", "run Documents\\x\\setup.exe",
  said on their own, skip the model (the 4b moved the mouse instead, three times, 2026-11-06).
"""
import ctypes
import re
import threading
import time
from ctypes import wintypes
from pathlib import Path

import psutil

import hand_gesture as G
import input_tools
import pc_tools
import screen_tools
import system_tools

ACTIONS = ("front", "minimize", "maximize", "restore", "move")
MONITORS = ("other", "left", "right", "main")
SETTLE_S = 2.0        # how long a window gets to show the change
LAUNCH_WAIT_S = 20    # how long a launched app gets to open a window
EXISTING_AFTER_S = 3  # an app that was already open: its own window counts after this long
UAC_PROCESS = "consent.exe"
BROWSERS = {"opera.exe", "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe"}
MIN_NAME = 5          # shorter names ("code") are everyday words

SW_MAXIMIZE, SW_MINIMIZE, SW_RESTORE = 3, 6, 9
SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_local = threading.local()


def new_request():
    """A new request from the owner (ai_assistant._send_message): "other" may move a window again."""
    _local.moved = set()


def _moved():
    """Windows sent to the other monitor in this request. "Other" is a toggle: a step's new attempt that moved the
    window again brought it back (2026-11-06)."""
    if not hasattr(_local, "moved"):
        new_request()
    return _local.moved


# --- which window -----------------------------------------------------------------------

def _pick(name, apps):
    """The exe of the open app `name` means, or None; several apps that fit equally raise a ToolError."""
    matches = system_tools._match_apps(name, apps)
    if len(matches) > 1:  # a window title can mention another app: the exe's own name counts first
        query = pc_tools._normalize(name)
        by_exe = [m for m in matches
                  if pc_tools._normalize(m[:-4]) in query or query in pc_tools._normalize(m[:-4])]
        matches = by_exe or matches
    if len(matches) > 1:
        raise system_tools.ToolError("Several open apps match; ask the user which one: "
                                     + ", ".join(m[:-4] for m in matches))
    return matches[0] if matches else None


def _rect(hwnd):
    r = wintypes.RECT()
    _user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def main_window(hwnds):
    """An app's main window: the biggest one on the screen (a minimized one has no real size)."""
    def size(hwnd):
        left, top, right, bottom = _rect(hwnd)
        return not _user32.IsIconic(hwnd), (right - left) * (bottom - top)
    return max(hwnds, key=size)


# --- doing it ---------------------------------------------------------------------------

def _to_front(hwnd):
    """Windows lets only the program in front give the front away, so this one joins its input queue for a moment."""
    if _user32.IsIconic(hwnd):
        _user32.ShowWindow(hwnd, SW_RESTORE)
    front = _user32.GetForegroundWindow()
    mine = _kernel32.GetCurrentThreadId()
    theirs = _user32.GetWindowThreadProcessId(front, None) if front else 0
    joined = bool(theirs) and theirs != mine and _user32.AttachThreadInput(mine, theirs, True)
    try:
        _user32.BringWindowToTop(hwnd)
        _user32.SetForegroundWindow(hwnd)
    finally:
        if joined:
            _user32.AttachThreadInput(mine, theirs, False)


def target_monitor(screens, here, where):
    """The monitor `where` means. Left, right and main are fixed places, so asking twice changes nothing; only
    "other" depends on where the window is. None when there is no other monitor."""
    if where == "left":
        return screens[0]
    if where == "right":
        return screens[-1]
    if where == "main":
        return G.monitor_of(screens, 0, 0)
    return next((s for s in screens if s != here), None)


def _centre(rect):
    return (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2


def _monitor_of(hwnd, screens):
    return G.monitor_of(screens, *_centre(_rect(hwnd)))


def _move(hwnd, there, screens):
    if _user32.IsIconic(hwnd):
        _user32.ShowWindow(hwnd, SW_RESTORE)
    zoomed = _user32.IsZoomed(hwnd)
    if zoomed:  # a maximized window moves only after it's restored; it's maximized again over there
        _user32.ShowWindow(hwnd, SW_RESTORE)
    rect = _rect(hwnd)
    x, y, w, h = G.place_on(rect, G.monitor_of(screens, *_centre(rect)), there)
    _user32.SetWindowPos(hwnd, None, x, y, w, h, SWP_NOZORDER | SWP_NOACTIVATE)
    if zoomed:
        _user32.ShowWindow(hwnd, SW_MAXIMIZE)


def _until(check):
    """True as soon as check() is, False if it isn't within SETTLE_S."""
    deadline = time.monotonic() + SETTLE_S
    while True:
        if check():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def _is_front(hwnd):
    return _user32.GetAncestor(_user32.GetForegroundWindow(), 2) == hwnd  # GA_ROOT


def _refused(label, what):
    return (f"Couldn't {what}: {label} didn't change. If it runs as administrator, Windows doesn't let other "
            "programs touch it. Tell the user it didn't work.")


def _act(hwnd, label, action, monitor):
    """Does it and reads the window back. Returns what really happened."""
    if action == "minimize":
        _user32.ShowWindow(hwnd, SW_MINIMIZE)
        return f"{label} is minimized." if _until(lambda: _user32.IsIconic(hwnd)) else _refused(label, "minimize it")
    if action == "maximize":
        _user32.ShowWindow(hwnd, SW_MAXIMIZE)
        return f"{label} is maximized." if _until(lambda: _user32.IsZoomed(hwnd)) else _refused(label, "maximize it")
    if action == "restore":
        _user32.ShowWindow(hwnd, SW_RESTORE)
        restored = _until(lambda: not _user32.IsIconic(hwnd) and not _user32.IsZoomed(hwnd))
        return f"{label} is back to its normal size." if restored else _refused(label, "restore it")
    if action == "front":
        _to_front(hwnd)
        return f"{label} is in front." if _until(lambda: _is_front(hwnd)) else _refused(label, "bring it to the front")
    screens = screen_tools.monitors()
    here = _monitor_of(hwnd, screens)
    there = target_monitor(screens, here, monitor)
    if there is None:
        return "There's only one monitor, so the window can't go to another one."
    if there == here and not _user32.IsIconic(hwnd):
        return f"{label} is already on that monitor."
    if monitor == "other" and hwnd in _moved():
        return f"{label} already went to the other monitor in this request, so it stays there."
    _move(hwnd, there, screens)
    if _until(lambda: _monitor_of(hwnd, screens) == there):
        _moved().add(hwnd)
        return f"{label} is on the {monitor} monitor now."
    return _refused(label, "move it")


def window_control(app: str, action: str = "front", monitor: str = "other") -> str:
    def run():
        if action not in ACTIONS:
            raise system_tools.ToolError(f"action must be one of: {', '.join(ACTIONS)}.")
        if monitor not in MONITORS:
            raise system_tools.ToolError(f"monitor must be one of: {', '.join(MONITORS)}.")
        apps = system_tools._open_apps()
        exe = _pick(app, apps)
        if exe is None:
            raise system_tools.ToolError(f"No open app matches '{app}'. Open apps:\n{system_tools.list_open_apps()}")
        stop = input_tools._allowed(step=f"{action} the window of {exe[:-4]}")  # asks only in strict mode (plan 20b)
        if stop:
            return stop
        with screen_tools.physical_px():
            hwnd = main_window(apps[exe]["hwnds"])
            # The title, not just the exe: a check has to see that this was the window asked for (a test window
            # reported only as "python" failed its check, 2026-11-06).
            return _act(hwnd, f"{exe[:-4]} (window '{_title(hwnd)[:60]}')", action, monitor)
    return system_tools._run_tool(run)


# --- starting an app --------------------------------------------------------------------

def uac_showing():
    """Windows' administrator question is on the screen: its process runs for as long as it is."""
    for process in psutil.process_iter(["name"]):
        if (process.info.get("name") or "").lower() == UAC_PROCESS:
            return True
    return False


def _all_windows(apps):
    return {hwnd for app in apps.values() for hwnd in app["hwnds"]}


def _in_folder(pid, folder):
    path = system_tools._exe_path(pid)
    try:
        return bool(path) and Path(path).is_relative_to(folder)
    except (OSError, ValueError):
        return False


def belongs(exe, app, name, folder=None):
    """This open app is the one that was started: it runs from `folder`, or its name or a title has `name`."""
    if folder is not None and any(_in_folder(pid, folder) for pid in app["pids"]):
        return True
    query, stem = pc_tools._normalize(name), pc_tools._normalize(exe[:-4])
    return bool(query) and (query in stem or stem in query or any(query in pc_tools._normalize(t) for t in app["titles"]))


def _opened(apps, name, folder, before, existing_too):
    """A window of the started app: a new one, or (existing_too) one it already had. None if there is none."""
    for exe, app in apps.items():
        if not belongs(exe, app, name, folder):
            continue
        new = [hwnd for hwnd in app["hwnds"] if hwnd not in before]
        if new or existing_too:
            return main_window(new or app["hwnds"])
    return None


def _title(hwnd):
    title = ctypes.create_unicode_buffer(256)
    _user32.GetWindowTextW(hwnd, title, 256)
    return title.value


def start_and_wait(name, start, folder=None):
    """Runs start() and waits for the app's window, then brings it to the front. Returns what really happened.
    start runs on its own thread: opening a program that wants administrator rights blocks until the owner
    answers Windows' question, and this has to see that question."""
    before = _all_windows(system_tools._open_apps())
    errors = []

    def run():
        try:
            start()
        except OSError as e:
            errors.append(e)
    threading.Thread(target=run, daemon=True, name="app-start").start()
    started = time.monotonic()
    while time.monotonic() - started < LAUNCH_WAIT_S:
        time.sleep(0.5)
        if errors:
            return f"Failed to launch {name}: {errors[0]}"
        waited = time.monotonic() - started
        hwnd = _opened(system_tools._open_apps(), name, folder, before, waited >= EXISTING_AFTER_S)
        if hwnd:
            with screen_tools.physical_px():
                _to_front(hwnd)
            return f"{name} is open: its window '{_title(hwnd)[:60]}' is on the screen."
        if uac_showing():
            return (f"{name} isn't running yet: it wants administrator rights, and Windows' Yes / No question is on "
                    "the screen. Only the owner can answer it, you can't. Tell them to press Yes.")
    return (f"Windows was told to start {name}, but no window showed up in {LAUNCH_WAIT_S} seconds, so it isn't "
            "confirmed. It may still be loading. Tell the user exactly that, not that it's open.")


# --- said on its own --------------------------------------------------------------------

_APP = r"(?:the\s+|my\s+)?(?P<app>[\w.'’ -]+?)"
_SIDE_EN = r"(?P<side>other|second|left|right|main|primary|first)"
_SIDE_TR = r"(?P<side>di[gğ]er|[oö]b[uü]r|ikinci|sa[gğ]|sol|ana|birinci)"
_EN = [
    ("move", re.compile(rf"\b(?:move|send|put|take|throw|drag)\s+{_APP}\s+(?:over\s+|back\s+)?(?:to|onto|on)\s+"
                        rf"(?:the\s+|my\s+)?{_SIDE_EN}\s+(?:[\w']+\s+){{0,2}}?(?:monitor|screen|display)s?\b", re.I)),
    ("minimize", re.compile(rf"\b(?:minimi[sz]e|hide)\s+{_APP}\s*(?:please|for me|window)?[.!?]*$", re.I)),
    ("minimize", re.compile(rf"\b(?:send|put)\s+{_APP}\s+(?:to|in)\s+the\s+background\b", re.I)),
    ("maximize", re.compile(rf"\bmaximi[sz]e\s+{_APP}\s*(?:please|for me|window)?[.!?]*$", re.I)),
    ("restore", re.compile(rf"\brestore\s+{_APP}\s*(?:please|for me|window)?[.!?]*$", re.I)),
    ("front", re.compile(rf"\bbring\s+{_APP}\s+(?:to\s+the\s+front|to\s+front|forward|up)\b", re.I)),
]
_TR_APP = r"(?P<app>[\w.-]+(?:\s+[\w.-]+){0,2}?)(?:['’]?(?:y?[ıiuü]|n[ıiuü]))?"
_TR = [
    ("move", re.compile(rf"\b{_TR_APP}\s+{_SIDE_TR}\s+(?:ekran|monit[oö]r)\w*\s+"
                        r"(?:ta[sş][ıi]|g[oö]t[uü]r|ge[cç]ir|at|al|koy)\w*", re.I)),
    ("minimize", re.compile(rf"\b{_TR_APP}\s+(?:simge\s+durumuna\s+)?(?:k[uü][cç][uü]lt|arka\s+plana\s+at)\w*", re.I)),
    ("maximize", re.compile(rf"\b{_TR_APP}\s+(?:b[uü]y[uü]t|tam\s+ekran\s+yap)\w*", re.I)),
    ("front", re.compile(rf"\b{_TR_APP}\s+[oö]ne\s+(?:getir|al)\w*", re.I)),
]
_NOT_AN_APP = re.compile(r"^(?:mouse|cursor|pointer|fare\w*|imle[cç]\w*|it|this|that|bunu|onu|\w*\s*video\w*|"
                         r"(?:this\s+|the\s+)?window|file|folder|dosya\w*|klas[oö]r\w*|sesi?\w*|volume)$", re.I)
_EXE = re.compile(r"((?:[a-z]:[\\/]|(?:documents|desktop|downloads)[\\/])[^\n\"<>|*?]*?\.exe)(?![\w.])", re.I)
_SHOT = re.compile(r"\b(?:send|share|give)\b.{0,30}\bscreenshot\b|ekran\s+g[oö]r[uü]nt[uü]\w*\s+"
                   r"(?:at|g[oö]nder|yolla|payla[sş])", re.I)
# "Run x.exe, then maximize it": there's more to do than the shortcut does (it dropped the rest, 2026-11-06).
_THEN = re.compile(r"\b(?:then|and|after|sonra|ard[ıi]ndan|ve)\b", re.I)
_RUN = re.compile(r"\b(?:run|start|launch|open|execute|install|[cç]al[ıi][sş]t[ıi]r\w*|ba[sş]lat\w*|a[cç]\w*|kur\w*)\b",
                  re.I)


def _side(word):
    word = (word or "other").lower()
    if word in ("left", "sol"):
        return "left"
    if word.startswith(("right", "sa")):
        return "right"
    if word in ("main", "primary", "first", "ana", "birinci"):
        return "main"
    return "other"


def asked(text):
    """(app, action, monitor) if the user only asks for that of one app's window, else None."""
    if input_tools._ABOUT_IT.search(text):
        return None
    for action, pattern in _EN + _TR:
        m = pattern.search(text.strip())
        if not m:
            continue
        rest = text[:m.start()] + " " + text[m.end():]
        app = re.sub(r"\s+(?:window|app|application|browser|penceresi\w*|uygulamas[ıi]\w*)$", "",
                     m.group("app").strip(), flags=re.I)
        if not app or len(app.split()) > 3 or _NOT_AN_APP.match(app) or input_tools._OTHER_TASK.search(rest):
            return None
        side = m.groupdict().get("side")
        return app, action, _side(side)
    return None


def open_app_note(text):
    """A line for the model when the request names an open app, else "". Browsers have their own tools."""
    said, words = pc_tools._normalize(text), set(re.findall(r"\w+", text.lower()))
    for exe, app in system_tools._open_apps().items():
        stem = exe[:-4].lower()
        if exe in BROWSERS:
            continue
        titled = next((t for t in app["titles"] if len(t) >= MIN_NAME and pc_tools._normalize(t) in said), None)
        if titled or (len(stem) >= MIN_NAME and stem in words):
            return (f"\n(Note: {titled or stem} is open on this PC. This is about that app's window, not a website: "
                    "start with window_control to bring it to the front.)")
    return ""


def exe_asked(text):
    """The .exe path in "run this: Documents\\x\\setup.exe", else None."""
    m = _EXE.search(text)
    if not m or not _RUN.search(text[:m.start()] + " " + text[m.end():]) or _THEN.search(text[m.end():]):
        return None
    return m.group(1)


def open_app(name):
    """What's open under this name, for find_applications, or None."""
    apps = system_tools._open_apps()
    try:
        exe = _pick(name, apps)
    except system_tools.ToolError:
        return None
    return f"{exe[:-4]} (window '{apps[exe]['titles'][0][:60]}')" if exe else None


def shortcut(text):
    """The answer to a request `asked` or `exe_asked` understood, or None: then the model takes it (also when no
    open app has that name: "hide the sidebar" isn't about an app)."""
    if pc_tools.current_photo_sender() and _SHOT.search(text) and not input_tools._ABOUT_IT.search(text):
        return screen_tools.send_screenshot()  # the 4b looked at the screen and said it had sent it (2026-11-06)
    path = exe_asked(text)
    if path:
        return pc_tools.launch_application(path)
    wanted = asked(text)
    if wanted is None:
        return None
    try:
        if _pick(wanted[0], system_tools._open_apps()) is None:
            return None
    except system_tools.ToolError:
        pass  # several fit: window_control asks which one
    return window_control(*wanted)


# --- tool schema ------------------------------------------------------------------------

TOOLS = [{
    "type": "function",
    "function": {
        "name": "window_control",
        "description": (
            "Controls the window of an open app: brings it to the front, minimizes it (also 'send it to the "
            "background'), maximizes it, restores it, or moves it to another monitor. It checks the window "
            "afterwards and its answer is what really happened: if it says it couldn't, tell the user that. Use "
            "this for 'move this window to the other monitor' and the like, never key shortcuts or move_mouse."),
        "parameters": {
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "The open app's name, e.g. 'Opera GX', 'Spotify'."},
                "action": {"type": "string", "enum": list(ACTIONS), "description": "What to do with the window."},
                "monitor": {"type": "string", "enum": list(MONITORS),
                            "description": "Only for move: other (default), left, right or main."},
            },
            "required": ["app", "action"],
        },
    },
}]

TOOL_FUNCTIONS = {"window_control": window_control}
