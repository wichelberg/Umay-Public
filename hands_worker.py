"""Umay's eyes ("Umay, eyes on"): the laptop camera follows up to two hands and moves the mouse; hand_gesture.py
says what they mean (one hand like a trackpad: open moves, a fist holds the left button, a drag over both
monitors; a tap of the thumb on the index: a click, two quick taps: a double click; ✌ held: off. A fist thrown:
left / right sends the grabbed window to the monitor that way, or closes it if there is none; up maximizes it,
down minimizes it. A side-on hand pushed aside: the active window closes. Two open hands: apart / together
zoom; two side-on hands pushed outwards: every window (Win+Tab); two fists: the window under the cursor
stretches with them). Runs in hands_env
(setup_voice_env.py --hands: MediaPipe and OpenCV, on the CPU), started by eyes.py with this file's folder
as its working folder.

- The camera: the laptop's own, chosen by its name (pygrabber lists them in the order OpenCV numbers them,
  and Windows numbers them differently from one start to the next). Never the owner's phone (Phone Link offers
  it as "<phone> (Windows Virtual Camera)": 2026-11-02 the eyes opened it while the owner was out), an IR,
  virtual, NVIDIA Broadcast or OBS camera (hand_gesture.NOT_CAMERAS); no other camera: the eyes stay off.
  UMAY_EYES_CAMERA_NAME picks one by part of its name. A camera's first frames are black, so it gets up to
  WARM_UP_SECONDS for a picture. (No guessing by the picture: in a dark room the laptop's camera gives an
  almost gray one, and the phone's a colored one.) Of several cameras, the one giving the most frames a second
  (measured for MEASURE_SECONDS each); each is asked for 640 x 480 MJPG at 60 a second (the laptop's gives 20
  whatever is asked, 2026-11-03; a 60 fps webcam then gives 60).
- Frames are shrunk to TRACK_WIDTH px wide before tracking. Two hands cost ~10 ms a frame on the CPU.
- A small preview at the bottom left of the main monitor shows what the camera sees and the hand it found
  (OPEN / GRAB / NO HAND). Clicks go through it and it never takes the focus.
- No hand for IDLE_SECONDS: it stops by itself (the camera's light goes off).
- stdin: "stop", or its end (Umay closed), stops it; a held button is always let go first. "snap <file>" saves
  the next picture there as a JPEG (a photo for the owner while the eyes are on: the camera is theirs).
- `hands_worker.py --snap <file>`: only that photo, from the same camera, and nothing else.
UMAY_EYES_DRY=1: everything but the mouse (for trying it out).
stdout, one JSON object per line: {"event": "ready", "camera": n}, {"event": "hand", "seen": bool},
{"event": "error", "text": ...}, {"event": "idle"}, {"event": "peace"} (✌: off), {"event": "closed", "title"}
(a window thrown away), {"event": "snap", "path"}, {"event": "stopped"}.
argv: process ids whose windows are never closed by a throw (Umay's, the HUD's).
"""
import ctypes
import json
import os
import sys
import threading
import time
from ctypes import wintypes

import cv2
import mediapipe as mp

import hand_gesture as G

IDLE_SECONDS = 300
WARM_UP_SECONDS = 3.0
MEASURE_SECONDS = 1.0
MIN_W, MIN_H = 320, 200   # a stretched window gets no smaller
TRACK_WIDTH = 640
PREVIEW = (256, 192)
TITLE = "UMAY EYES"
CYAN, HOT, MUTED = (255, 214, 56), (163, 79, 255), (150, 138, 110)   # BGR
GWL_STYLE, GWL_EXSTYLE = -16, -20
WS_POPUP, WS_VISIBLE = 0x80000000, 0x10000000
WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_NOACTIVATE = 0x8, 0x80, 0x80000, 0x20, 0x08000000
SWP_NOACTIVATE, SWP_FRAMECHANGED, SWP_SHOWWINDOW = 0x10, 0x20, 0x40
HWND_TOPMOST = -1
LEFTDOWN, LEFTUP, WHEEL, WHEEL_DELTA = 0x2, 0x4, 0x800, 120
KEYUP, VK_CONTROL, VK_LWIN, VK_TAB = 0x2, 0x11, 0x5B, 0x09
SW_MAXIMIZE, SW_MINIMIZE, SW_RESTORE, SWP_NOZORDER = 3, 6, 9, 0x4
WM_CLOSE, GA_ROOT = 0x10, 2
SHELL = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}

user32 = ctypes.windll.user32
user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # physical px, as SetCursorPos wants them
user32.FindWindowW.restype = wintypes.HWND
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetAncestor.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
user32.PostMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
user32.GetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.restype = ctypes.c_long
# HWND_TOPMOST is (HWND)-1: as a plain int ctypes would pass 32 bits of it and the call would fail silently.
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_uint]
stop = threading.Event()
snaps = []   # files a photo is asked for ("snap <file>")
DRY = os.environ.get("UMAY_EYES_DRY", "").strip() == "1"


def say(obj):
    try:
        print(json.dumps(obj), flush=True)
    except OSError:  # Umay is gone (its end of the pipe closed): nobody to tell
        pass


def desktop():
    """Every monitor together (left, top, right, bottom), physical px."""
    x, y = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
    return x, y, x + user32.GetSystemMetrics(78), y + user32.GetSystemMetrics(79)


def cameras():
    """[(index, name)] as OpenCV's DirectShow numbers them, or None if the names can't be read."""
    try:
        from pygrabber.dshow_graph import FilterGraph
        return list(enumerate(FilterGraph().get_input_devices()))
    except Exception:
        return None


def start_camera(index):
    """The camera at `index` once it gives pictures, or None."""
    cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
    if not cap.isOpened():
        return None
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    cap.set(cv2.CAP_PROP_FPS, 60)
    end, got = time.monotonic() + WARM_UP_SECONDS, False
    while time.monotonic() < end:  # a webcam's first frames are black; a dark room stays dark: still it
        ok, frame = cap.read()
        got = got or (ok and frame is not None)
        if got and frame.mean() > 3:
            break
    if got:
        return cap
    cap.release()
    return None


def frames_a_second(cap):
    end, n = time.monotonic() + MEASURE_SECONDS, 0
    while time.monotonic() < end:
        n += bool(cap.read()[0])
    return n / MEASURE_SECONDS


def open_camera():
    listed = cameras()
    if listed is None:  # no names: only what is clearly a color camera, never with a phone's name to check
        return None, None
    indexes = G.cameras_to_try(listed, os.environ.get("UMAY_EYES_CAMERA_NAME", "").strip())
    if len(indexes) > 1:  # several: the fastest
        measured = []
        for index in indexes:
            cap = start_camera(index)
            measured.append((index, frames_a_second(cap) if cap is not None else 0.0))
            if cap is not None:
                cap.release()
        best = G.best_camera(measured)
        indexes = [] if best is None else [best]
    for index in indexes:
        cap = start_camera(index)
        if cap is not None:
            return cap, dict(listed)[index]
    return None, None


def small(frame):
    h, w = frame.shape[:2]
    return frame if w <= TRACK_WIDTH else cv2.resize(frame, (TRACK_WIDTH, round(h * TRACK_WIDTH / w)))


def window_under_cursor():
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return user32.GetAncestor(user32.WindowFromPoint(pt), GA_ROOT)


def closable(hwnd, skip):
    """A window a throw may close: an app's, not the desktop's, the taskbar's, Umay's or this preview's."""
    if not hwnd:
        return False
    cls, title = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, cls, 256)
    user32.GetWindowTextW(hwnd, title, 256)
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return cls.value not in SHELL and title.value != TITLE and pid.value not in skip and pid.value != os.getpid()


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD)]


MONITOR_ENUM = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT),
                                  wintypes.LPARAM)


def monitors():
    """Each monitor's work area (left, top, right, bottom), physical px."""
    found = []

    def each(hmon, _hdc, _rect, _data):
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(info)
        user32.GetMonitorInfoW(hmon, ctypes.byref(info))
        w = info.rcWork
        found.append((w.left, w.top, w.right, w.bottom))
        return True
    user32.EnumDisplayMonitors(None, None, MONITOR_ENUM(each), 0)
    return found


def rect_of(hwnd):
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def title_of(hwnd):
    title = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, title, 256)
    return title.value or "a window"


def throw(hwnd, way, skip):
    """The thrown window: to the monitor that way (left / right), or closed the way its ✕ does (WM_CLOSE: an app
    with unsaved work still asks) if there is none; up: maximized; down: minimized. Returns what to tell Umay
    (only a close: the rest shows on the screen), or None."""
    if DRY or not closable(hwnd, skip):
        G.note(f"thrown {way}: nothing to do (dry run, or no app's window was grabbed)")
        return None
    if way in ("up", "down"):
        user32.ShowWindow(hwnd, SW_MAXIMIZE if way == "up" else SW_MINIMIZE)
        return None
    screens = monitors()
    left, top, right, bottom = rect_of(hwnd)
    here = G.monitor_of(screens, (left + right) // 2, (top + bottom) // 2)
    there = G.next_monitor(screens, here, way)
    if there is None:
        title = title_of(hwnd)
        G.note(f"thrown {way} off {here}: closing {title!r}")
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        return {"event": "closed", "title": title}
    G.note(f"thrown {way}: {title_of(hwnd)!r} from {here} to {there}")
    zoomed = user32.IsZoomed(hwnd)
    if zoomed:
        user32.ShowWindow(hwnd, SW_RESTORE)
    x, y, w, h = G.place_on(rect_of(hwnd), here, there)
    user32.SetWindowPos(hwnd, None, x, y, w, h, SWP_NOZORDER | SWP_NOACTIVATE)
    if zoomed:
        user32.ShowWindow(hwnd, SW_MAXIMIZE)
    return None


def close_active(skip):
    """A push: the active window is closed the way its ✕ does. Returns what to tell Umay, or None."""
    hwnd = user32.GetAncestor(user32.GetForegroundWindow(), GA_ROOT)
    if DRY or not closable(hwnd, skip):
        G.note("push: nothing to close (dry run, or the active window is the desktop, the taskbar or Umay's)")
        return None
    title = title_of(hwnd)
    G.note(f"push: closing {title!r}")
    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    return {"event": "closed", "title": title}


class Stretch:
    """Two fists stretching the window under the cursor (from its middle, no smaller than MIN_W x MIN_H)."""

    def __init__(self, skip):
        hwnd = window_under_cursor()
        self.hwnd = hwnd if not DRY and closable(hwnd, skip) else None
        if self.hwnd is None:
            return
        if user32.IsZoomed(self.hwnd):
            user32.ShowWindow(self.hwnd, SW_RESTORE)
        self.start = rect_of(self.hwnd)

    def to(self, grow_x, grow_y):
        if self.hwnd is None:
            return
        left, top, right, bottom = self.start
        w, h = max(MIN_W, right - left + grow_x), max(MIN_H, bottom - top + grow_y)
        cx, cy = (left + right) // 2, (top + bottom) // 2
        user32.SetWindowPos(self.hwnd, None, cx - w // 2, cy - h // 2, w, h, SWP_NOZORDER | SWP_NOACTIVATE)


def keys(*codes):
    """Presses the keys in order and lets them go in reverse (a shortcut)."""
    for code in codes:
        user32.keybd_event(code, 0, 0, 0)
    for code in reversed(codes):
        user32.keybd_event(code, 0, KEYUP, 0)


def mouse(action):
    if DRY:
        return
    if action[0] == "move":
        user32.SetCursorPos(int(action[1]), int(action[2]))
    elif action[0] == "down":
        user32.mouse_event(LEFTDOWN, 0, 0, 0, 0)
    elif action[0] == "up":
        user32.mouse_event(LEFTUP, 0, 0, 0, 0)
    elif action[0] == "click":
        user32.mouse_event(LEFTDOWN, 0, 0, 0, 0)
        user32.mouse_event(LEFTUP, 0, 0, 0, 0)
    elif action[0] == "zoom":  # Ctrl + the wheel, where the cursor is
        user32.keybd_event(VK_CONTROL, 0, 0, 0)
        user32.mouse_event(WHEEL, 0, 0, WHEEL_DELTA * action[1], 0)
        user32.keybd_event(VK_CONTROL, 0, KEYUP, 0)
    elif action[0] == "overview":
        keys(VK_LWIN, VK_TAB)


class Preview:
    """The small picture at the bottom left: borderless, on top, clicks go through, never focused."""

    def __init__(self):
        self.hwnd = None

    def show(self, frame, hands, state):
        img = cv2.resize(cv2.flip(frame, 1), PREVIEW)
        img = (img * 0.55).astype("uint8")
        w, h = PREVIEW
        hot = state in ("GRAB", "STRETCH", "PUSH")
        for lm in hands:
            pts = [(int((1 - x) * w), int(y * h)) for x, y, _ in lm]
            for a, b in mp.solutions.hands.HAND_CONNECTIONS:
                cv2.line(img, pts[a], pts[b], CYAN, 1, cv2.LINE_AA)
            for p in pts:
                cv2.circle(img, p, 2, CYAN, -1, cv2.LINE_AA)
            px, py = G.palm(lm)
            cv2.circle(img, (int((1 - px) * w), int(py * h)), 6, HOT if hot else CYAN, 2, cv2.LINE_AA)
        cv2.rectangle(img, (0, 0), (w - 1, h - 1), CYAN, 1)
        cv2.putText(img, "UMAY // EYES", (8, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.42, CYAN, 1, cv2.LINE_AA)
        cv2.putText(img, state, (8, h - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    HOT if hot else CYAN if state in ("OPEN", "ZOOM") else MUTED, 1, cv2.LINE_AA)
        cv2.imshow(TITLE, img)
        if self.hwnd is None:
            self._style()
        cv2.waitKey(1)

    def _style(self):
        self.hwnd = user32.FindWindowW(None, TITLE)
        if not self.hwnd:
            return
        user32.SetWindowLongW(self.hwnd, GWL_STYLE, WS_POPUP | WS_VISIBLE)
        ex = user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(self.hwnd, GWL_EXSTYLE, ex | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_LAYERED
                              | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE)
        user32.SetLayeredWindowAttributes(self.hwnd, 0, 225, 2)  # LWA_ALPHA
        work = wintypes.RECT()
        user32.SystemParametersInfoW(0x30, 0, ctypes.byref(work), 0)  # SPI_GETWORKAREA: the main monitor
        user32.SetWindowPos(self.hwnd, wintypes.HWND(HWND_TOPMOST), work.left + 16, work.bottom - PREVIEW[1] - 16, PREVIEW[0],
                            PREVIEW[1], SWP_NOACTIVATE | SWP_FRAMECHANGED | SWP_SHOWWINDOW)


def listen():
    for line in sys.stdin:
        line = line.strip()
        if line == "stop":
            break
        if line.startswith("snap "):
            snaps.append(line[5:].strip())
    stop.set()  # "stop", or Umay closed


def save_photo(frame, path):
    cv2.imwrite(path, frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    say({"event": "snap", "path": path})


def snap_only(path):
    """`--snap <file>`: one photo from the laptop's camera, then the camera is closed."""
    cap, camera = open_camera()
    if cap is None:
        say({"event": "error", "text": "the laptop's camera didn't answer (a phone's or a virtual camera is never used)"})
        return
    try:
        best = None
        end = time.monotonic() + WARM_UP_SECONDS
        while time.monotonic() < end:  # its exposure settles in the first second or two
            ok, frame = cap.read()
            if ok and frame is not None:
                best = frame
        if best is None:
            say({"event": "error", "text": "the camera gave no picture"})
        else:
            save_photo(best, path)
    finally:
        cap.release()


def note(text):
    """A line in hands_worker.log (stderr, which eyes.py sends there): why a throw or a sweep went as it did."""
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}", file=sys.stderr, flush=True)


def main():
    G.LOG = note
    skip = {int(a) for a in sys.argv[1:] if a.isdigit()}
    grabbed = None
    threading.Thread(target=listen, daemon=True).start()
    cap, camera = open_camera()
    if cap is None:
        say({"event": "error", "text": "the laptop's camera didn't answer (a phone's or a virtual camera is never used)"})
        return
    tracker = mp.solutions.hands.Hands(max_num_hands=2, model_complexity=0, min_detection_confidence=0.6,
                                       min_tracking_confidence=0.5)
    width, height = cap.get(cv2.CAP_PROP_FRAME_WIDTH), cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
    eyes, preview = G.Eyes(desktop(), height / width if width and height else 0.75), Preview()
    stretch = None
    say({"event": "ready", "camera": camera})
    seen, last_seen, failures = None, time.monotonic(), 0
    cursor = wintypes.POINT()
    try:
        while not stop.is_set():
            ok, frame = cap.read()
            if not ok or frame is None:
                failures += 1
                if failures > 60:
                    say({"event": "error", "text": "the camera stopped giving pictures"})
                    break
                time.sleep(0.03)
                continue
            failures = 0
            frame = small(frame)
            found = tracker.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)).multi_hand_landmarks or []
            hands = [[(p.x, p.y, p.z) for p in f.landmark] for f in found]
            now = time.monotonic()
            user32.GetCursorPos(ctypes.byref(cursor))
            actions = eyes.update(hands, now, (cursor.x, cursor.y))
            if ("stop",) in actions:
                say({"event": "peace"})
                break
            for action in actions:
                if action[0] == "down":
                    grabbed = window_under_cursor()
                elif action[0] == "fling":
                    told = throw(grabbed, action[1], skip)
                    if told:
                        say(told)
                    grabbed = None
                    continue
                elif action[0] == "close":   # a push: the active window, the one just clicked
                    told = close_active(skip)
                    if told:
                        say(told)
                    continue
                elif action[0] == "stretch_start":
                    stretch = Stretch(skip)
                    continue
                elif action[0] == "stretch":
                    if stretch is not None:
                        stretch.to(action[1], action[2])
                    continue
                elif action[0] == "stretch_end":
                    stretch = None
                    continue
                mouse(action)
            if bool(hands) != seen:
                seen = bool(hands)
                say({"event": "hand", "seen": seen})
            if hands:
                last_seen = now
            elif now - last_seen > IDLE_SECONDS:
                say({"event": "idle"})
                break
            state = ("NO HAND" if not hands else "STRETCH" if eyes.mode == "fist" else "ZOOM" if eyes.mode == "open"
                     else "PUSH" if eyes.pushing else "GRAB" if eyes.pressed else "TAP" if eyes.pinching
                     else "OFF IN 1 S" if eyes.peace_since else "OPEN")
            preview.show(frame, hands, state)
            while snaps:
                save_photo(frame, snaps.pop(0))
    finally:
        for action in eyes.release():
            mouse(action)
        cap.release()
        cv2.destroyAllWindows()
        say({"event": "stopped"})


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--snap":
        snap_only(sys.argv[2])
    else:
        main()
