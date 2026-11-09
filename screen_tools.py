"""Tools that let Umay see the PC screen and send it to the user.

look_at_screen returns the screenshot to the model itself (ai_assistant attaches
it to the conversation as an image), so Umay can check things like "is the
video really paused?". It sends one monitor at its full resolution, the one with
the window in front unless she asks for another: both monitors shrunk into one
1280 px image left each ~600 px wide, too small for a tab's name or a file list,
and the local model made such text up instead of saying it couldn't read it.
send_screenshot sends the whole screen to the user over Telegram.

The latest screenshot is also what input_tools' clicks aim at: points are given from 0 to 1000 across
it (the local model's own way: asked for pixels it gave nonsense, measured 2026-11-02), and `zoom`
shows a part of it bigger, for small things in a dense list.
"""
import base64
import contextlib
import ctypes
import io
import os
from ctypes import wintypes

from PIL import Image, ImageDraw, ImageGrab

import hud_windows
import pc_tools

MODEL_MAX_WIDTH = 1920  # a monitor at full resolution (a 4K one is shrunk to this)
MODEL_MAX_HEIGHT = 1200
ZOOM_MAX = 3            # how much bigger a zoomed part gets at most
PHOTO_MAX_WIDTH = 1920
JPEG_QUALITY = 80
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
PER_MONITOR_AWARE_V2 = -4
WHICH = ("active", "other", "all")
READ_RULE = ("Read only what you can clearly see; if some text is too small or blurry, say you can't read it "
             "instead of guessing.")
POINT_RULE = ("To click, scroll or zoom at something, give x and y from 0 to 1000 across this screenshot "
              "(0,0 = top-left, 1000,1000 = bottom-right).")

_user32 = ctypes.windll.user32
_user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
# What the latest screenshot showed (physical px): its part of the screen, the monitor it is on, and
# how many images it was (clicks need exactly one).
_view = {"rect": None, "monitor": None, "count": 0}
DESKTOP_WIDTH = 2000


def capture(max_width):
    image = ImageGrab.grab(all_screens=True).convert("RGB")
    return _jpeg(image, max_width)


def _jpeg(image, max_width):
    image = image.convert("RGB")
    if image.width > max_width:
        image = image.resize((max_width, round(image.height * max_width / image.width)))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=JPEG_QUALITY)
    return buffer.getvalue()


@contextlib.contextmanager
def physical_px():
    """Win32 answers in physical px on this thread, like Pillow's grab (Umay's Tk is DPI-unaware)."""
    old = _user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(PER_MONITOR_AWARE_V2))
    try:
        yield
    finally:
        if old:
            _user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(old))


def monitors():
    """Every monitor's full rect (physical px), left to right."""
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    def visit(_monitor, _dc, rect, _data):
        r = rect.contents
        found.append((r.left, r.top, r.right, r.bottom))
        return True
    _user32.EnumDisplayMonitors(None, None, visit, 0)
    return sorted(found)


def front_window(windows, own_pids):
    """The topmost app window that isn't Umay's (her text box doesn't count): what the user is looking at."""
    return next((w for w in windows if hud_windows.counts(w, own_pids)), None)


def pick(which, screens, active):
    """The monitors to send: the active one, the others (the active one if there is no other), or all."""
    if which == "all":
        return list(screens)
    if which == "other":
        return [s for s in screens if s != active] or [active]
    return [active]


def zoom_rect(view, box):
    """The part of `view` (l, t, r, b) that `box` [x1, y1, x2, y2] (0..1000 across it) covers."""
    l, t, r, b = view
    x1, y1, x2, y2 = (min(1000.0, max(0.0, float(v))) for v in box)
    x1, x2 = sorted((x1, x2))
    y1, y2 = sorted((y1, y2))
    rect = (l + round(x1 / 1000 * (r - l)), t + round(y1 / 1000 * (b - t)),
            l + round(x2 / 1000 * (r - l)), t + round(y2 / 1000 * (b - t)))
    if rect[2] - rect[0] < 40 or rect[3] - rect[1] < 40:
        raise ValueError("That zoom box is too small; give a box at least a few lines tall.")
    return rect


def point(view, x, y):
    """A point given 0..1000 across `view` (l, t, r, b), in screen px."""
    l, t, r, b = view
    x, y = (min(1000.0, max(0.0, float(v))) for v in (x, y))
    return round(l + x / 1000 * (r - l - 1)), round(t + y / 1000 * (b - t - 1))


def to_screen(x, y):
    """A point on the latest screenshot (0..1000), in physical screen px; None if there isn't exactly one."""
    return point(_view["rect"], x, y) if _view["count"] == 1 else None


def _scale(width, height, zoomed):
    factor = min(MODEL_MAX_WIDTH / width, MODEL_MAX_HEIGHT / height)
    return min(ZOOM_MAX, factor) if zoomed else min(1.0, factor)


def _mark(image, x, y):
    """A red ring with a dot where the click went, so the model sees what it hit."""
    draw = ImageDraw.Draw(image)
    for r, w in ((14, 3), (3, 3)):
        draw.ellipse((x - r, y - r, x + r, y + r), outline="#FF2020", width=w)


def _shots(rects, zoomed=False, mark=None):
    """Data URIs of these parts of the screen (physical px); `mark`: a point (screen px) to ring."""
    with physical_px():
        origin = _user32.GetSystemMetrics(SM_XVIRTUALSCREEN), _user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    screen = ImageGrab.grab(all_screens=True).convert("RGB")
    out = []
    for l, t, r, b in rects:
        part = screen.crop((l - origin[0], t - origin[1], r - origin[0], b - origin[1]))
        k = _scale(part.width, part.height, zoomed)
        if k != 1:
            part = part.resize((round(part.width * k), round(part.height * k)), Image.LANCZOS)
        if mark and l <= mark[0] < r and t <= mark[1] < b:
            _mark(part, (mark[0] - l) * k, (mark[1] - t) * k)
        buffer = io.BytesIO()
        part.save(buffer, "JPEG", quality=JPEG_QUALITY)
        out.append("data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode())
    return out


def _front_and_active():
    with physical_px():
        screens = monitors()
        front = front_window(hud_windows.top_windows(), {os.getpid()})
        active = hud_windows.monitor_of_rect(front.rect) if front else hud_windows.monitor_at(0, 0)
    return screens, front, active if active in screens else screens[0]


def use_monitor(rect):
    """That monitor (physical px) is the view now: the next screenshot, click and scroll are on it
    (input_tools.move_mouse)."""
    _view.update(rect=rect, monitor=rect, count=1)


def refresh(mark=None):
    """A new screenshot of the monitor the latest one was on (after a click or a key), as the view; `mark`:
    where the click went (screen px), ringed in red."""
    monitor = _view["monitor"] or _front_and_active()[2]
    _view.update(rect=monitor, monitor=monitor, count=1)
    return _shots([monitor], mark=mark)


def desktop_jpeg():
    """A fresh JPEG of all the monitors side by side, for the heavy model's check of a plan step (plan 26)."""
    return capture(DESKTOP_WIDTH)


def view_jpeg():
    """A fresh JPEG of what the latest screenshot showed (the same part of the screen, so a point 0..1000 on it
    is a point on the latest screenshot), or None if that wasn't exactly one image. input_tools.aim sends it to
    the heavy model."""
    if _view["count"] != 1 or not _view["rect"]:
        return None
    return base64.b64decode(_shots([_view["rect"]])[0].split(",", 1)[1])


def look_at_screen(monitor: str = "active", zoom: list = None) -> dict:
    if zoom:
        if _view["count"] != 1:
            return "Zoom works on a screenshot of one monitor: use look_at_screen without zoom first."
        try:
            rect = zoom_rect(_view["rect"], zoom)
        except (ValueError, TypeError) as e:
            return f"Couldn't zoom: {e}"
        _view.update(rect=rect, count=1)
        return {"text": "Zoomed into that part of the latest screenshot; it's attached in the next message. "
                        f"{POINT_RULE} {READ_RULE}",
                "images": _shots([rect], zoomed=True)}
    which = monitor if monitor in WHICH else "active"
    screens, front, active = _front_and_active()
    chosen = pick(which, screens, active)
    _view.update(rect=chosen[0], monitor=chosen[0], count=len(chosen))
    seen = {"active": "the monitor with the window in front", "other": "the other monitor",
            "all": f"all {len(chosen)} monitors, one image each, left to right"}[which]
    front_text = f" The window in front: \"{front.title}\"." if front else ""
    point_text = POINT_RULE if len(chosen) == 1 else "To click on one of them, look at that monitor alone first."
    return {
        "text": f"Took a screenshot of {seen}; it's attached in the next message.{front_text} "
                f"Answer from what you see. {READ_RULE} {point_text}",
        "images": _shots(chosen),
    }


def send_screenshot(caption: str = "") -> str:
    send = pc_tools.current_photo_sender()
    if send is None:
        return "The user is at the PC (Umay's window), so there's nothing to send; use look_at_screen to see it yourself."
    send(capture(PHOTO_MAX_WIDTH), caption)
    return "Sent the screenshot to the user on Telegram."


def _tool(name, description, properties, required):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


TOOLS = [
    _tool(
        "look_at_screen",
        "Takes a screenshot of the PC screen so you can see it. Never for web pages or websites: those are "
        "web_read_page. Use it when the user asks what's on the screen, to check the result of an action you "
        "can't otherwise verify, or before using the mouse and keyboard in an app. It shows the "
        "monitor with the window in front unless you ask for another.",
        {"monitor": {"type": "string", "enum": list(WHICH),
                     "description": "active (default): the monitor with the window in front; other: the other "
                                    "monitor; all: every monitor. Ask for other or all only when what you need "
                                    "isn't on the active one."},
         "zoom": {"type": "array", "items": {"type": "number"},
                  "description": "Optional [x1, y1, x2, y2] (0..1000 across the latest screenshot): shows that "
                                 "part bigger, to read or click small things precisely."}},
        [],
    ),
    _tool(
        "send_screenshot",
        "Sends a screenshot of the PC screen to the user (over Telegram). Use when they ask to see the screen.",
        {"caption": {"type": "string", "description": "Optional short caption."}},
        [],
    ),
]

TOOL_FUNCTIONS = {"look_at_screen": look_at_screen, "send_screenshot": send_screenshot}
