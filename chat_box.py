"""The text box (Ctrl+F1, or a click on the HUD's orb), beside the orb: in the HUD's look, dark glass with a
thin cyan edge, corner brackets and a faint glow, and its fonts (Rajdhani, JetBrains Mono). Typed messages
go to Umay; her answer shows here (silently) while the orb thinks and then talks. The same look asks yes/no
before she changes something on the PC (confirm).

The glow is a click-through layered window (per-pixel alpha); the panel itself is a plain Tk window
inside it. Everything runs on this module's own Tk thread; the functions at the bottom are
thread-safe.
"""
import ctypes
import threading
import tkinter as tk
from ctypes import wintypes

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

import tkpost
from layered import (AC_SRC_ALPHA, BLENDFUNCTION, GWL_EXSTYLE, ULW_ALPHA, WS_EX_LAYERED, WS_EX_TOOLWINDOW,
                     WS_EX_TRANSPARENT, Frame, gdi32, user32)
from layered import work_area as _work_area

gdi32.DeleteDC.argtypes = [wintypes.HDC]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]

# The HUD's colors (hud_paint.C): NAVY is the panel's glass, flat so the Tk window sits in it without a seam.
NAVY = "#07141a"
NAVY_LIGHT = "#0c2129"
TEXT = "#e6f7ff"
PINK = "#ff4fa3"
CYAN = "#38d6ff"
CYAN2 = "#7fdfff"
MUTED = "#6e8a96"
DIM = "#1d3a45"
PANEL_RGB = (7, 20, 26)
CYAN_RGB = np.array([56, 214, 255], np.float32)
MARGIN = 22         # room around the panel for the glow
INSET = 8           # from the frame's edge to the Tk window
CORNER = 18         # the corner brackets' length
BOX_SIZE = (460, 340)
CONFIRM_SIZE = (400, 180)
CONFIRM_TIMEOUT_SECONDS = 300
LABEL_FONT = ("Rajdhani", 11, "bold")
TEXT_FONT = ("Rajdhani", 12, "bold")
MONO_FONT = ("JetBrains Mono", 10)
FR_PRIVATE = 0x10
_fonts = {"loaded": False}


def load_fonts():
    """The HUD's fonts for this process only (Tk finds them by name once Windows has them)."""
    if _fonts["loaded"]:
        return
    _fonts["loaded"] = True
    import paths
    for f in (paths.resource_dir() / "assets" / "fonts").glob("*.ttf"):
        ctypes.windll.gdi32.AddFontResourceExW(str(f), FR_PRIVATE, 0)


def neon_frame(width, height):
    """The frame as a PIL RGBA image (width x height, the panel MARGIN inside), like the HUD's panels: dark
    glass, a thin cyan edge, cyan corner brackets and a faint cyan glow outside. Straight alpha (layered.Frame
    premultiplies it)."""
    w, h = width, height
    m = MARGIN
    edge = Image.new("L", (w, h), 0)
    ImageDraw.Draw(edge).rectangle((m, m, w - m - 1, h - m - 1), outline=255, width=1)
    brackets = Image.new("L", (w, h), 0)
    bd = ImageDraw.Draw(brackets)

    def box(x0, y0, x1, y1):
        bd.rectangle((min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)), fill=255)
    for x, y, sx, sy in ((m, m, 1, 1), (w - m - 1, m, -1, 1), (m, h - m - 1, 1, -1), (w - m - 1, h - m - 1, -1, -1)):
        box(x, y, x + sx * (CORNER - 1), y + sy)
        box(x, y, x + sx, y + sy * (CORNER - 1))
    inside = np.zeros((h, w), np.float32)
    inside[m:h - m, m:w - m] = 0.97
    light = np.maximum(np.asarray(edge, np.float32) / 255 * 0.4, np.asarray(brackets, np.float32) / 255)
    glow = np.clip(np.asarray(edge.filter(ImageFilter.GaussianBlur(7)), np.float32) / 255 * 0.5, 0, 1)
    glow[inside > 0] = 0
    alpha = np.clip(np.maximum(np.maximum(inside, light), glow), 0, 1)
    rgb = np.array(PANEL_RGB, np.float32) * (1 - light)[..., None] + CYAN_RGB * light[..., None]
    rgb[inside == 0] = CYAN_RGB
    return Image.fromarray(np.dstack([np.clip(rgb, 0, 255), alpha * 255]).astype(np.uint8), "RGBA")


class _Neon:
    """A panel of `size` with the neon frame around it, at (x, y) (the panel's top left)."""

    def __init__(self, root, size, x, y):
        self.root = root
        pw, ph = size
        fw, fh = pw + 2 * (MARGIN + INSET), ph + 2 * (MARGIN + INSET)
        self.glow_win = tk.Toplevel(root)
        self.glow_win.overrideredirect(True)
        self.glow_win.attributes("-topmost", True)
        self.glow_win.geometry(f"{fw}x{fh}+{x - MARGIN - INSET}+{y - MARGIN - INSET}")
        self.glow_win.update_idletasks()
        hwnd = user32.GetParent(self.glow_win.winfo_id())
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_TRANSPARENT)
        screen_dc = user32.GetDC(None)
        mem_dc = gdi32.CreateCompatibleDC(screen_dc)
        self.frame = Frame(neon_frame(fw, fh), screen_dc)
        old = gdi32.SelectObject(mem_dc, self.frame.bitmap)
        user32.UpdateLayeredWindow(hwnd, screen_dc, ctypes.byref(wintypes.POINT(x - MARGIN - INSET, y - MARGIN - INSET)),
                                   ctypes.byref(wintypes.SIZE(fw, fh)), mem_dc, ctypes.byref(wintypes.POINT(0, 0)), 0,
                                   ctypes.byref(BLENDFUNCTION(0, 0, 255, AC_SRC_ALPHA)), ULW_ALPHA)
        gdi32.SelectObject(mem_dc, old)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(None, screen_dc)
        self.win = tk.Toplevel(root)  # after the glow, so it's drawn above it
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=NAVY)
        self.win.geometry(f"{pw}x{ph}+{x}+{y}")

    def destroy(self):
        self.win.destroy()
        self.glow_win.destroy()
        self.frame.free()


def _place(size):
    """Where a panel of `size` goes: beside the HUD's orb (the side with more room), level with its upper
    part; without it, the bottom-right corner."""
    import hud

    left, top, right, bottom = _work_area()
    pw, ph = size
    pad = MARGIN + INSET
    at = hud.anchor()
    if at is None:
        return right - pw - pad - 20, bottom - ph - pad - 20
    x, y, w, h = at
    if x + w / 2 > (left + right) / 2:
        px = x + w * 0.2 - pw - pad  # to her left
    else:
        px = x + w * 0.8 + pad
    py = y + h * 0.25
    px = int(min(max(left + pad, px), right - pw - pad))
    py = int(min(max(top + pad, py), bottom - ph - pad))
    return px, py


def _button(parent, text, command, color):
    """A HUD button: a thin colored edge round a dark face (Tk has no border color, so a frame draws it)."""
    edge = tk.Frame(parent, bg=color, padx=1, pady=1)
    tk.Button(edge, text=text, command=command, bg=NAVY_LIGHT, fg=color, activebackground=NAVY, activeforeground=TEXT,
              relief="flat", font=LABEL_FONT, padx=16, pady=2, borderwidth=0, cursor="hand2").pack()
    return edge


def _header(win, title, row=0):
    head = tk.Frame(win, bg=NAVY)
    head.grid(row=row, column=0, sticky="ew", padx=14, pady=(10, 0))
    tk.Label(head, text=title, bg=NAVY, fg=CYAN2, font=LABEL_FONT).pack(side="left")
    tk.Label(head, text="ESC", bg=NAVY, fg=MUTED, font=("Rajdhani", 9, "bold")).pack(side="right")
    tk.Frame(win, bg=DIM, height=1).grid(row=row + 1, column=0, sticky="ew", padx=14, pady=(6, 0))


class ChatBox:
    def __init__(self, root, on_send, on_close, hint, thinking_text, focus=True):
        self.root, self.on_send, self.on_close = root, on_send, on_close
        self.thinking_text = thinking_text
        load_fonts()
        self.neon = _Neon(root, BOX_SIZE, *_place(BOX_SIZE))
        win = self.neon.win
        win.columnconfigure(0, weight=1)
        win.rowconfigure(2, weight=1)
        _header(win, "UMAY  //  TEXT LINK")
        self.history = tk.Text(win, bg=NAVY, fg=TEXT, font=TEXT_FONT, wrap="word", relief="flat",
                               borderwidth=0, highlightthickness=0, padx=14, pady=8, cursor="arrow",
                               insertbackground=NAVY, spacing1=2, spacing3=2)
        self.history.grid(row=2, column=0, sticky="nsew")
        self.history.tag_configure("you", foreground=PINK, font=LABEL_FONT)
        self.history.tag_configure("her", foreground=CYAN, font=LABEL_FONT)
        self.history.tag_configure("note", foreground=MUTED, font=("Rajdhani", 11))
        self.history.configure(state="disabled")
        self.status = tk.Label(win, text="", bg=NAVY, fg=MUTED, font=("Rajdhani", 10, "bold"), anchor="w", padx=14)
        self.status.grid(row=3, column=0, sticky="ew")
        line = tk.Frame(win, bg=DIM, height=1)
        line.grid(row=4, column=0, sticky="ew", padx=14)
        row = tk.Frame(win, bg=NAVY_LIGHT)
        row.grid(row=5, column=0, sticky="ew", padx=14, pady=(8, 12))
        row.columnconfigure(1, weight=1)
        tk.Label(row, text="›", bg=NAVY_LIGHT, fg=CYAN, font=("JetBrains Mono", 12, "bold"), padx=8).grid(
            row=0, column=0)
        self.entry = tk.Entry(row, bg=NAVY_LIGHT, fg=TEXT, insertbackground=CYAN, relief="flat",
                              disabledbackground=NAVY_LIGHT, disabledforeground=MUTED,
                              font=("JetBrains Mono", 11), highlightthickness=0)
        self.entry.grid(row=0, column=1, sticky="ew", ipady=6, padx=(0, 8))
        self.hint = hint
        self.status.configure(text=hint)  # the entry has the focus at once: the hint stays above it
        self.entry.bind("<Return>", self._send)
        for w in (win, self.entry, self.history):
            w.bind("<Escape>", lambda e: self.close())
        self._dots = 0
        self._thinking = False
        win.update_idletasks()
        win.lift()
        if focus:  # not when Umay opens it herself: the owner may be typing somewhere else
            win.focus_force()
            self.entry.focus_set()

    def _send(self, event=None):
        text = self.entry.get().strip()
        if not text or self._thinking:
            return
        self.entry.delete(0, "end")
        self.on_send(text)

    def add(self, who, text, tag):
        self.history.configure(state="normal")
        if self.history.index("end-1c") != "1.0":
            self.history.insert("end", "\n\n")
        if who:
            self.history.insert("end", f"{who}: ", tag)
            self.history.insert("end", text)
        else:
            self.history.insert("end", text, tag)
        self.history.see("end")
        self.history.configure(state="disabled")

    def thinking(self, on):
        self._thinking = on
        self.entry.configure(state="disabled" if on else "normal")
        if on:
            self._tick()
        else:
            self.status.configure(text=self.hint)
            self.entry.focus_set()

    def _tick(self):
        if not self._thinking:
            return
        self._dots = self._dots % 3 + 1
        self.status.configure(text=self.thinking_text + "." * self._dots)
        self.root.after(400, self._tick)

    def close(self):
        self._thinking = False
        self.neon.destroy()
        self.on_close()


class Confirm:
    """Yes/no in the same look, beside the pet; answer(bool) is called once."""

    def __init__(self, root, title, question, yes, no, answer):
        self.answer, self.done = answer, False
        load_fonts()
        self.neon = _Neon(root, CONFIRM_SIZE, *_place(CONFIRM_SIZE))
        win = self.neon.win
        win.columnconfigure(0, weight=1)
        win.rowconfigure(2, weight=1)
        _header(win, title.upper())
        tk.Label(win, text=question, bg=NAVY, fg=TEXT, font=TEXT_FONT, wraplength=CONFIRM_SIZE[0] - 28,
                 justify="left", anchor="nw").grid(row=2, column=0, sticky="nsew", padx=14, pady=(8, 0))
        buttons = tk.Frame(win, bg=NAVY)
        buttons.grid(row=3, column=0, sticky="ew", padx=14, pady=(4, 12))
        for text, value, color in ((no, False, PINK), (yes, True, CYAN)):
            _button(buttons, text.upper(), lambda v=value: self._pick(v), color).pack(side="right", padx=(8, 0))
        win.bind("<Escape>", lambda e: self._pick(False))
        win.bind("<Return>", lambda e: self._pick(True))
        win.lift()
        win.focus_force()

    def _pick(self, value):
        if self.done:
            return
        self.done = True
        self.neon.destroy()
        self.answer(value)


_state = {"root": None, "box": None, "mailbox": None}
_lock = threading.Lock()


def _ensure():
    with _lock:
        if _state["root"] is not None:
            return
        ready = threading.Event()

        def ui_thread():
            root = tk.Tk()
            root.withdraw()
            _state["mailbox"] = tkpost.Mailbox(root)  # how other threads reach this one: never waits
            _state["root"] = root
            ready.set()
            root.mainloop()

        threading.Thread(target=ui_thread, daemon=True).start()
        ready.wait()


def toggle(on_open, on_send, on_close, hint, thinking_text, only_open=False):
    """Thread-safe: opens the box (on_open() first) or closes it (on_close() after). only_open: Umay opens it
    herself (companion.py): an open box stays, and it doesn't take the keyboard's focus."""
    _ensure()

    def run():
        if _state["box"] is not None:
            if not only_open:
                _state["box"].close()
            return
        on_open()

        def closed():
            _state["box"] = None
            on_close()

        _state["box"] = ChatBox(_state["root"], on_send, closed, hint, thinking_text, focus=not only_open)

    post(run)


def post(fn):
    """Thread-safe, never waits: fn() runs on this module's Tk thread (tkpost.Mailbox)."""
    _ensure()
    _state["mailbox"].post(fn)


def is_open():
    return _state["box"] is not None


def add(who, text, tag="her"):
    """Thread-safe: a line in the box ("you", "her" or "note"), if it's open."""
    if _state["root"] is not None:
        post(lambda: _state["box"] and _state["box"].add(who, text, tag))


def thinking(on):
    if _state["root"] is not None:
        post(lambda: _state["box"] and _state["box"].thinking(on))


def confirm(title, question, yes, no, timeout=CONFIRM_TIMEOUT_SECONDS):
    """Blocks the calling (worker) thread until yes/no is clicked; no after `timeout` seconds."""
    _ensure()
    result, done = {"ok": False}, threading.Event()

    def answer(value):
        result["ok"] = value
        done.set()

    box = {}
    post(lambda: box.setdefault("it", Confirm(_state["root"], title, question, yes, no, answer)))
    if not done.wait(timeout):
        post(lambda: box.get("it") and box["it"]._pick(False))
    return result["ok"]
