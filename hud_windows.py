"""Where the orb goes (follow): to the bottom-right corner of an open VS Code window, else of the active
window, else by what is on its own monitor: nothing (it sits in the middle), a window (it docks in the
bottom-right corner) or something fullscreen (a game or a video: it hides). Windows only (user32 /
dwmapi), physical px.

A window counts the way Alt+Tab counts it: visible, not minimized, not cloaked (hidden UWP apps and
other virtual desktops), not a tool window, with a title, not owned by another window unless it says it
is an app window. That leaves out the desktop, the taskbar, overlays (NVIDIA's is a fullscreen layered
window) and Umay's own windows (own_pids). It must also cover at least MIN_SHARE of the monitor.
"""
import ctypes
from ctypes import wintypes
from dataclasses import dataclass

MIN_SHARE = 0.02  # of the monitor's area: a small window (a calculator) counts, a notification doesn't
GWL_STYLE, GWL_EXSTYLE, GW_OWNER = -16, -20, 4
WS_EX_TOOLWINDOW, WS_EX_APPWINDOW, WS_EX_NOACTIVATE = 0x80, 0x40000, 0x08000000
DWMWA_CLOAKED, DWMWA_EXTENDED_FRAME_BOUNDS = 14, 9
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MONITOR_DEFAULTTONEAREST = 2
SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}
ELECTRON_CLASS = "Chrome_WidgetWin_1"
CODE_EXES = {"code.exe", "code - insiders.exe"}

_user32 = ctypes.windll.user32
_dwm = ctypes.windll.dwmapi
_kernel32 = ctypes.windll.kernel32
_kernel32.OpenProcess.restype = wintypes.HANDLE
_user32.GetWindowLongW.restype = ctypes.c_long
_user32.GetWindow.restype = wintypes.HWND
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.MonitorFromWindow.restype = wintypes.HMONITOR
_user32.MonitorFromPoint.restype = wintypes.HMONITOR


@dataclass(frozen=True)
class Window:
    pid: int
    cls: str
    title: str
    rect: tuple       # left, top, right, bottom
    visible: bool
    iconic: bool
    cloaked: bool
    toolwindow: bool
    appwindow: bool
    owned: bool
    hwnd: int = 0
    exe: str = ""     # the program's file name, looked up only for Electron windows (VS Code is one)


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD)]


def _area(rect):
    l, t, r, b = rect
    return max(0, r - l) * max(0, b - t)


def _overlap(a, b):
    return _area((max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])))


def counts(w, own_pids):
    """An app window as Alt+Tab shows it, not Umay's."""
    return (w.visible and not w.iconic and not w.cloaked and not w.toolwindow and bool(w.title.strip())
            and (not w.owned or w.appwindow) and w.pid not in own_pids and w.cls not in SHELL_CLASSES)


def judge(windows, monitor, own_pids):
    """"free", "window" or "fullscreen" for the monitor (its full rect, physical px)."""
    m_area = _area(monitor) or 1
    covered = False
    for w in windows:
        if not counts(w, own_pids):
            continue
        share = _overlap(w.rect, monitor) / m_area
        if share >= 0.999 and tuple(w.rect) == tuple(monitor):
            return "fullscreen"
        covered = covered or share >= MIN_SHARE
    return "window" if covered else "free"


def is_code(w):
    """A VS Code window: by its program, or by its title if the program couldn't be read."""
    return w.cls == ELECTRON_CLASS and (w.exe.lower() in CODE_EXES or w.title.endswith("Visual Studio Code"))


def code_window(windows, own_pids, monitor_of_rect):
    """The topmost VS Code window that is open: ("code", window); ("hidden", None) when something else
    fullscreen is above it on its monitor (a game, a video); ("none", None) when no VS Code is open.
    `windows` in z-order (top first), monitor_of_rect(rect) gives a monitor's full rect."""
    above = []
    for w in windows:
        if not counts(w, own_pids):
            continue
        if is_code(w):
            monitor = tuple(monitor_of_rect(w.rect))
            return ("hidden", None) if any(tuple(o.rect) == monitor for o in above) else ("code", w)
        above.append(w)
    return "none", None


def follow(windows, foreground, own_pids, monitor_of_rect):
    """Where the orb goes, with the window it goes to:
    - ("code", w): an open VS Code window (code_window), whichever window is active;
    - ("active", w): else the active window (`foreground`, its handle) if it is an app window;
    - ("hidden", None): either of them is under or is something fullscreen;
    - ("keep", None): the active window is Umay's own, a dialog or a popup (the Start menu): no move;
    - ("none", None): nothing is active, or the desktop or the taskbar is (judge() decides)."""
    seen, w = code_window(windows, own_pids, monitor_of_rect)
    if seen != "none":
        return seen, w
    w = next((w for w in windows if foreground and w.hwnd == foreground), None)
    if w is None or w.cls in SHELL_CLASSES:
        return "none", None
    if not counts(w, own_pids):
        return "keep", None
    return ("hidden", None) if tuple(w.rect) == tuple(monitor_of_rect(w.rect)) else ("active", w)


def _exe(pid):
    name = ""
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if handle:
        try:
            buf, size = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
            if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                name = buf.value.rsplit("\\", 1)[-1]
        finally:
            _kernel32.CloseHandle(handle)
    return name


def _window(hwnd):
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    cls = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, cls, 256)
    title = ctypes.create_unicode_buffer(256)
    _user32.GetWindowTextW(hwnd, title, 256)
    rect = wintypes.RECT()
    _user32.GetWindowRect(hwnd, ctypes.byref(rect))
    cloaked = wintypes.DWORD()
    _dwm.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
    ex = _user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    return Window(pid.value, cls.value, title.value, (rect.left, rect.top, rect.right, rect.bottom),
                  bool(_user32.IsWindowVisible(hwnd)), bool(_user32.IsIconic(hwnd)), bool(cloaked.value),
                  bool(ex & WS_EX_TOOLWINDOW), bool(ex & WS_EX_APPWINDOW), bool(_user32.GetWindow(hwnd, GW_OWNER)),
                  hwnd or 0, _exe(pid.value) if cls.value == ELECTRON_CLASS else "")


def foreground():
    """The active window's handle (0 if none)."""
    return _user32.GetForegroundWindow() or 0


def frame_rect(hwnd):
    """What is seen of a window (physical px): its rect without the invisible resize borders."""
    rect = wintypes.RECT()
    if _dwm.DwmGetWindowAttribute(wintypes.HWND(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect),
                                  ctypes.sizeof(rect)):
        _user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


def top_windows():
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        if _user32.IsWindowVisible(hwnd):
            found.append(_window(hwnd))
        return True
    _user32.EnumWindows(visit, 0)
    return found


def monitor_of(hwnd):
    """The full rect (physical px) of the monitor `hwnd` is on."""
    info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    _user32.GetMonitorInfoW(_user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST), ctypes.byref(info))
    r = info.rcMonitor
    return r.left, r.top, r.right, r.bottom


def monitor_of_rect(rect):
    """The full rect (physical px) of the monitor a rect's middle is on."""
    l, t, r, b = rect
    return monitor_at((l + r) // 2, (t + b) // 2)


def monitor_at(x, y):
    """The full rect (physical px) of the monitor at a point."""
    info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    _user32.GetMonitorInfoW(_user32.MonitorFromPoint(wintypes.POINT(int(x), int(y)), MONITOR_DEFAULTTONEAREST),
                            ctypes.byref(info))
    r = info.rcMonitor
    return r.left, r.top, r.right, r.bottom
