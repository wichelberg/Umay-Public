"""The Memory Cortex wallpaper: cortex.html on the primary monitor, behind the desktop icons and the HUD's panels,
inside the HUD's process (hud_app.main starts it).

- Placement: like wallpaper apps, it becomes a child of the desktop's WorkerW (Progman gets message 0x052C to make
  one). On Windows 11 24H2+ that WorkerW sits inside Progman, so the window goes into Progman just under the icon
  layer (SHELLDLL_DefView). No desktop layer found: nothing is shown (a window over the desktop would hide the
  icons), unless UMAY_CORTEX_WALLPAPER=window.
- Data: every PUSH_MS the HUD reads umay_memory.db (cortex_graph.read, read only, no numpy) and, when it
  changed, hands it to the page (window.cortexFeed). No server: the page runs with ?src=push.
- Cost: frames stop (window.cortexPause) while an app window covers the primary monitor or something is
  fullscreen there (a game, a video); the page draws at most FPS frames a second.

UMAY_CORTEX_WALLPAPER: on (default) | off | window. Needs QtWebEngine (PySide6-Addons); without it, nothing shows
and hud.log says why. `python hud_cortex.py --demo` shows the page's demo for a quick look (Ctrl+C ends it).
"""
import ctypes
import json
import os
import sys
from ctypes import wintypes
from pathlib import Path

PUSH_MS = 1000
CHECK_MS = 1000  # also how soon her ring shows when the desktop clears (the orb waits FREE_AFTER_S)
FPS = 24
# orb=1: Umay's ring is the cortex's outer ring (the owner's design, 2026-11-07); hud=0: the page's own info box
# repeated the HUD's MEMORY panel, one of the things that made the desktop look chaotic.
PAGE_ARGS = "src=push&fps={fps}&hudpos={hudpos}&spin=1&orb=1&hud=0"

_user32 = ctypes.windll.user32
_user32.FindWindowW.restype = wintypes.HWND
_user32.FindWindowExW.restype = wintypes.HWND
_user32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
_user32.SetParent.restype = wintypes.HWND
_user32.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
_user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_uint]
_user32.GetWindowLongW.restype = ctypes.c_long
_user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
GWL_STYLE = -16
WS_CHILD, WS_POPUP = 0x40000000, 0x80000000
SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x0010, 0x0040


def mode():
    return os.environ.get("UMAY_CORTEX_WALLPAPER", "on").strip().lower()


def available():
    """Why it can't run ("" when it can)."""
    if mode() in ("off", "0", "false", "no"):
        return "off (UMAY_CORTEX_WALLPAPER)"
    try:
        from PySide6 import QtWebEngineWidgets  # noqa: F401
    except ImportError as e:
        return f"no QtWebEngine ({e})"
    return ""


def prepare():
    """Before the QApplication is made: QtWebEngine needs shared OpenGL contexts, set before that."""
    if available():
        return
    from PySide6.QtCore import QCoreApplication, Qt
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    from PySide6 import QtWebEngineWidgets  # noqa: F401


def page_path():
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / "cortex.html"


def desktop_layer():
    """(parent, insert_after) for a window drawn behind the desktop icons, or None.
    Older Windows: the WorkerW after the one holding the icons (SHELLDLL_DefView), no insert_after.
    Windows 11 24H2+: Progman itself, with the window put just under the icon layer."""
    progman = _user32.FindWindowW("Progman", None)
    if not progman:
        return None
    result = ctypes.c_size_t()
    _user32.SendMessageTimeoutW(progman, 0x052C, 0xD, 0x1, 0, 1000, ctypes.byref(result))
    _user32.SendMessageTimeoutW(progman, 0x052C, 0, 0, 0, 1000, ctypes.byref(result))
    defview = _user32.FindWindowExW(progman, None, "SHELLDLL_DefView", None)
    if defview:  # 24H2+: the icons stay in Progman, the wallpaper's WorkerW is under them, inside it too
        worker = _user32.FindWindowExW(progman, None, "WorkerW", None)
        if worker:  # inside it: over the plain wallpaper it draws, under the icons (Progman's order kept them on top)
            return worker, None
        return progman, defview
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(top, _):
        if _user32.FindWindowExW(top, None, "SHELLDLL_DefView", None):
            found.append(_user32.FindWindowExW(None, top, "WorkerW", None))
        return True
    _user32.EnumWindows(visit, 0)
    worker = next((w for w in found if w), None)
    return (worker, None) if worker else None


def attach(hwnd, rect):
    """Makes `hwnd` a child of the desktop layer over `rect` (physical px). False: no desktop layer."""
    layer = desktop_layer()
    if layer is None:
        return False
    parent, under = layer
    style = _user32.GetWindowLongW(hwnd, GWL_STYLE)
    _user32.SetWindowLongW(hwnd, GWL_STYLE, (style & ~WS_POPUP) | WS_CHILD)
    _user32.SetParent(hwnd, parent)
    origin = wintypes.POINT(0, 0)
    _user32.ClientToScreen(parent, ctypes.byref(origin))
    l, t, r, b = rect
    _user32.SetWindowPos(hwnd, under or 0, l - origin.x, t - origin.y, r - l, b - t,
                         SWP_NOACTIVATE | SWP_SHOWWINDOW | (0 if under else 0x0004))  # 0x4: keep the z-order
    return under or True


def keep_under(hwnd, icons):
    """24H2+: the window right under the icon layer, above the WorkerW that draws the plain wallpaper. Explorer
    puts its WorkerW back over it after 0x052C (seen 2026-11-07), so this is checked again now and then."""
    _user32.GetWindow.restype = wintypes.HWND
    if _user32.GetWindow(hwnd, 3) != icons:  # GW_HWNDPREV: the sibling just above
        _user32.SetWindowPos(hwnd, icons, 0, 0, 0, 0, SWP_NOACTIVATE | 0x0001 | 0x0002)  # NOSIZE | NOMOVE


def primary_rect():
    import hud_windows
    return hud_windows.monitor_at(0, 0)


def covered(own_pids):
    """Something hides the desktop of the primary monitor: an app window over it, or a fullscreen one."""
    import hud_windows
    try:
        return hud_windows.judge(hud_windows.top_windows(), primary_rect(), own_pids) != "free"
    except OSError:
        return False


class Wallpaper:
    """The page, its placement, the data it gets and when it draws."""

    def __init__(self, log=print, read_graph=None, own_pids=(), demo=False, hudpos="bl"):
        from PySide6.QtCore import Qt, QTimer, QUrl
        from PySide6.QtGui import QColor
        from PySide6.QtWebEngineWidgets import QWebEngineView
        self.log, self.read_graph, self.own_pids = log, read_graph, set(own_pids)
        self.view = QWebEngineView()
        self.view.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
                                 | Qt.WindowType.WindowStaysOnBottomHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.view.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.view.page().setBackgroundColor(QColor("#000"))
        self.view.setWindowTitle("Umay Cortex")
        url = QUrl.fromLocalFile(str(page_path()))
        url.setQuery("demo=1&orb=1&hud=0" if demo else PAGE_ARGS.format(fps=FPS, hudpos=hudpos))
        self.view.load(url)
        self.ready, self.paused, self.last = False, False, None
        self.view.loadFinished.connect(self._loaded)
        rect = primary_rect()
        self.view.setGeometry(rect[0], rect[1], rect[2] - rect[0], rect[3] - rect[1])
        self.view.show()
        hwnd = int(self.view.winId())
        placed = attach(hwnd, rect)
        self.placed = bool(placed) or mode() == "window"
        self.hwnd, self.icons = hwnd, placed if not isinstance(placed, bool) else None
        if placed:
            if self.icons:
                keep_under(hwnd, self.icons)
            self.log("cortex wallpaper: behind the desktop icons")
        elif mode() == "window":
            self.log("cortex wallpaper: no desktop layer, a bottom window instead (UMAY_CORTEX_WALLPAPER=window)")
        else:
            self.log("cortex wallpaper: no desktop layer found, not shown")
            self.view.hide()
            return
        self.timers = []
        if not demo:
            self._every(PUSH_MS, self.push)
        self._every(CHECK_MS, self.check)

    def _every(self, ms, fn):
        from PySide6.QtCore import QTimer
        t = QTimer()
        t.timeout.connect(fn)
        t.start(ms)
        self.timers.append(t)

    def _loaded(self, ok):
        self.ready = bool(ok)
        if not ok:
            self.log(f"cortex wallpaper: the page didn't load ({page_path()})")
        else:
            self.last = None
            self.push()

    def push(self):
        if not self.ready or self.read_graph is None or self.paused:
            return
        try:
            data = json.dumps(self.read_graph(), ensure_ascii=False)
        except Exception as e:  # the file in use, or not made yet: the next push tries again
            self.log(f"cortex wallpaper: {type(e).__name__}: {e}")
            return
        if data != self.last:
            self.last = data
            self.view.page().runJavaScript(f"window.cortexFeed({data})")

    def showing(self):
        """Umay is drawn here (her ring, her clock) rather than by the HUD's orb: placed, shown and drawing."""
        return self.placed and self.ready and not getattr(self, "hidden", False) and not self.paused

    def orb(self, state):
        """Her state for the ring: {"label", "color", "level"}; sent only when it changed."""
        if not self.ready or state == getattr(self, "last_orb", None):
            return
        self.last_orb = state
        self.view.page().runJavaScript(f"window.cortexOrb({json.dumps(state)})")

    def set_shown(self, on):
        """The HUD hidden (Ctrl+F1 / Umay's {"visible": false}): the wallpaper too, drawing nothing."""
        self.hidden = not on
        self.view.setVisible(on)
        self.check()

    def check(self):
        if getattr(self, "hidden", False):
            if not self.paused and self.ready:
                self.paused = True
                self.view.page().runJavaScript("window.cortexPause(true)")
            return
        if self.icons:
            keep_under(self.hwnd, self.icons)
        pause = covered(self.own_pids)
        if pause != self.paused and self.ready:
            self.paused = pause
            self.view.page().runJavaScript(f"window.cortexPause({'true' if pause else 'false'})")
            if not pause:
                self.push()


def start(log=print, own_pids=()):
    """The wallpaper with Umay's memory, or None (off, no QtWebEngine, no desktop layer)."""
    why = available()
    if why:
        log(f"cortex wallpaper: {why}")
        return None
    import cortex_graph  # never memory_cortex here: its numpy import hung this process in the exe
    w = Wallpaper(log=log, read_graph=cortex_graph.read, own_pids=own_pids,
                  hudpos=os.environ.get("UMAY_CORTEX_HUDPOS", "bl").strip() or "bl")
    return w if w.placed else None  # never a window over the desktop icons


if __name__ == "__main__":
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    import signal
    app = QApplication(sys.argv[:1])
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    keep = QTimer()
    keep.timeout.connect(lambda: None)
    keep.start(250)  # lets Ctrl+C through
    seconds = next((int(a.split("=")[1]) for a in sys.argv if a.startswith("--seconds=")), 0)
    if seconds:
        QTimer.singleShot(seconds * 1000, app.quit)
    w = Wallpaper(demo="--demo" in sys.argv, own_pids=(os.getpid(),)) if "--demo" in sys.argv else start(own_pids=(os.getpid(),))
    sys.exit(app.exec())
