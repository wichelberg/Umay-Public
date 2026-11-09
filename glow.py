"""The light around the screen while a voice conversation is on: a neon frame with plasma wisps,
purple to cyan, that rises and falls with the voice (Umay's while she speaks, the microphone's
while the owner does), like an equalizer.

The frame is one rounded rectangle: every pixel knows its distance from the frame's line and its
place along it, and round the corners that place runs along an arc, so the light and the wisps
flow from one edge into the next without a seam. It is drawn into four click-through layered
windows (one per edge of the primary monitor) with per-pixel alpha (UpdateLayeredWindow).

Speed: the light is worked out on the frame unrolled into a strip (a column every COL_STEP px
along it, a row per px across it), where everything along the frame is one value per column; each
screen pixel then reads its cell through a fixed index map (one 32-bit gather). float32, split over
four threads (numpy releases the GIL), FPS times a second from a loop of its own (1 ms timer
resolution while shown). show(level, mode) / set_mode / hide() are thread-safe; level() -> 0..1.
"""
import ctypes
import math
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes

import numpy as np

from layered import (AC_SRC_ALPHA, BITMAPINFOHEADER, BLENDFUNCTION, DIB_RGB_COLORS, GWL_EXSTYLE, ULW_ALPHA,
                     WS_EX_LAYERED, WS_EX_TOOLWINDOW, WS_EX_TRANSPARENT, gdi32, user32)

FPS = 60
HAZE_STEP = 4            # rows per haze sample (it's smooth)
COL_STEP = 3             # px along the frame per column of the unrolled frame (across it: every px)
THICKNESS = 0.11         # of the screen's shorter side; also the corners' radius
FADE_SECONDS = 0.4
WS_EX_NOACTIVATE = 0x08000000
MAGENTA = np.array([255, 40, 210], np.float32)
BLUE = np.array([70, 90, 255], np.float32)
CYAN = np.array([40, 225, 255], np.float32)
# Calm while listening, a slow swirl while she thinks, the voice while she speaks.
MODES = {"listen": (0.55, 0.6), "think": (0.45, 1.6), "speak": (0.6, 1.0)}  # (base level, swirl speed)


def _palette(p):
    """Colour along the frame (p in 0..1, wrapping): magenta, blue, cyan, blue, magenta. (n, 3)"""
    p = (p % 1.0) * 4
    seg = np.floor(p).astype(int) % 4
    f = (p - np.floor(p))[:, None]
    stops = np.stack([MAGENTA, BLUE, CYAN, BLUE, MAGENTA])
    return (stops[seg] * (1 - f) + stops[seg + 1] * f).astype(np.float32)


class _Wave:
    """A smooth wave round the frame that loops seamlessly (integer frequencies)."""

    def __init__(self, rng, terms=5):
        self.freq = rng.integers(2, 13, terms)
        self.speed = rng.uniform(-0.08, 0.08, terms)
        self.phase = rng.uniform(0, 2 * math.pi, terms)
        self.amp = (1.0 / (1 + np.arange(terms))).astype(np.float32)
        self.freq = self.freq.astype(np.float32)

    def __call__(self, p, t):
        angle = (np.float32(2 * math.pi) * (self.freq[:, None] * p[None])
                 + (2 * math.pi * self.speed * t * 10 + self.phase).astype(np.float32)[:, None])
        return np.float32(0.5) + np.float32(0.5 / self.amp.sum()) * (self.amp[:, None] * np.sin(angle)).sum(0)


def frame_geometry(xs, ys, width, height, radius):
    """Place along the frame (p, 0..1 clockwise from the top-left corner) and distance in from its
    line (d, px; negative toward the screen's very corner) for pixel centres xs, ys. The line runs
    along the screen's edges and round each corner on a quarter circle of `radius`."""
    w, h, r = float(width), float(height), float(radius)
    arc = math.pi * r / 2
    top, side = w - 2 * r, h - 2 * r
    perimeter = 2 * top + 2 * side + 4 * arc
    x, y = np.broadcast_arrays(xs, ys)
    x, y = x.astype(np.float64), y.astype(np.float64)
    p = np.zeros_like(x)
    d = np.zeros_like(x)
    # corners: (centre, the angle its arc starts at, how far along the frame that arc starts)
    corners = [((r, r), math.pi, 0.0), ((w - r, r), -math.pi / 2, arc + top),
               ((w - r, h - r), 0.0, 2 * arc + top + side), ((r, h - r), math.pi / 2, 3 * arc + 2 * top + side)]
    done = np.zeros(x.shape, bool)
    for (cx, cy), start, at in corners:
        inside = (np.abs(x - cx) <= r) & (np.abs(y - cy) <= r) & ((x < r) | (x > w - r)) & ((y < r) | (y > h - r))
        vx, vy = x[inside] - cx, y[inside] - cy
        angle = (np.arctan2(vy, vx) - start) % (2 * math.pi)
        angle = np.where(angle > 3 * math.pi / 2, angle - 2 * math.pi, angle)  # just before the arc's start
        p[inside] = at + np.clip(angle, 0, math.pi / 2) / (math.pi / 2) * arc
        d[inside] = r - np.hypot(vx, vy)
        done |= inside
    rest = ~done
    xr, yr = x[rest], y[rest]
    to_edge = np.stack([yr, w - xr, h - yr, xr])                        # top, right, bottom, left
    nearest = to_edge.argmin(0)
    d[rest] = to_edge.min(0)
    p[rest] = np.choose(nearest, [arc + (xr - r), 2 * arc + top + (yr - r), 3 * arc + top + side + (w - r - xr),
                                  4 * arc + 2 * top + side + (h - r - yr)])
    return (p / perimeter) % 1.0, d


class _Edge:
    """One edge's window; `map` says which cell of the unrolled frame (Glow.texture) each of its
    pixels shows."""

    def __init__(self, root, x, y, w, h, screen, radius, cols, rows, screen_dc):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.geometry(f"{w}x{h}+{x}+{y}")
        self.win.update_idletasks()
        self.hwnd = user32.GetParent(self.win.winfo_id())
        style = user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(self.hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_TRANSPARENT
                              | WS_EX_NOACTIVATE)
        xs = x + np.arange(w) + 0.5
        ys = y + np.arange(h) + 0.5
        p, d = frame_geometry(xs[None, :], ys[:, None], *screen, radius)
        col = np.minimum((p * cols).astype(np.int64), cols - 1)
        row = np.clip(np.where(d < 0, -d * 2.5, d), 0, rows - 1).astype(np.int64)  # past a corner's arc: fades fast
        self.map = (row * cols + col).astype(np.intp)
        header = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=w, biHeight=-h, biPlanes=1,
                                  biBitCount=32, biCompression=0)
        self.bits = ctypes.c_void_p()
        self.bitmap = gdi32.CreateDIBSection(screen_dc, ctypes.byref(header), DIB_RGB_COLORS,
                                             ctypes.byref(self.bits), None, 0)
        self.view = np.ctypeslib.as_array(ctypes.cast(self.bits, ctypes.POINTER(ctypes.c_uint32)), shape=(h, w))
        self.mem_dc = gdi32.CreateCompatibleDC(screen_dc)
        gdi32.SelectObject(self.mem_dc, self.bitmap)

    def draw(self, texture):
        np.take(texture, self.map, out=self.view)

    def show(self, blend, screen_dc):
        user32.UpdateLayeredWindow(self.hwnd, screen_dc, ctypes.byref(wintypes.POINT(self.x, self.y)),
                                   ctypes.byref(wintypes.SIZE(self.w, self.h)), self.mem_dc,
                                   ctypes.byref(wintypes.POINT(0, 0)), 0, ctypes.byref(blend), ULW_ALPHA)


class Glow:
    def __init__(self, root):
        self.root = root
        self.level_fn = lambda: 0.0
        self.mode = "listen"
        self.target = 0.0      # 1 shown, 0 hidden (faded)
        self.fade = 0.0
        self.smooth = 0.0      # the level, smoothed: fast up, slower down
        self.edges = []
        self.screen_dc = user32.GetDC(None)
        rng = np.random.default_rng(7)
        self.reach_wave, self.bright_wave = _Wave(rng), _Wave(rng)
        self.strands = [(_Wave(rng), _Wave(rng), np.float32(shade)) for shade in (1.15, 0.9, 1.0, 0.8, 1.1)]
        self.pool = ThreadPoolExecutor(4)
        self.wake = threading.Event()
        self.t0 = time.monotonic()
        self.fps_seen = 0.0    # measured while shown (for a look at how it keeps up)
        threading.Thread(target=self._loop, daemon=True).start()

    def build(self):
        """The four windows and the unrolled frame (Tk thread)."""
        w, h = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        depth = int(min(w, h) * THICKNESS)
        perimeter = 2 * (w - 2 * depth) + 2 * (h - 2 * depth) + 2 * math.pi * depth
        self.cols, self.rows, self.depth = int(math.ceil(perimeter / COL_STEP)), depth, depth
        self.p = ((np.arange(self.cols) + 0.5) / self.cols).astype(np.float32)
        self.d = (np.arange(self.rows, dtype=np.float32) + 0.5)[:, None]           # px from the line
        self.core = np.exp(-((self.d - 2.5) / 2.0) ** 2).astype(np.float32)         # the neon line itself
        self.texture = np.zeros((self.rows, self.cols), np.uint32)
        self.chunks = np.array_split(np.arange(self.cols), 4)
        self.buffers = {}
        self.d_coarse = self.d[::HAZE_STEP] + np.float32(HAZE_STEP / 2 - 0.5)
        args = ((w, h), depth, self.cols, self.rows, self.screen_dc)
        self.edges = [_Edge(self.root, 0, 0, w, depth, *args),
                      _Edge(self.root, w - depth, depth, depth, h - 2 * depth, *args),
                      _Edge(self.root, 0, h - depth, w, depth, *args),
                      _Edge(self.root, 0, depth, depth, h - 2 * depth, *args)]

    def look(self, t, level):
        """One frame's look along the frame: a value per column of the unrolled frame."""
        base, swirl = MODES.get(self.mode, MODES["listen"])
        energy = base + (1 - base) * level
        depth, p, tt = self.depth, self.p, t * swirl
        reach = depth * (0.12 + 0.3 * energy * (0.4 + 0.6 * self.reach_wave(p, tt)))
        strands = []
        for along, wobble, shade in self.strands:
            at = depth * (0.04 + 0.6 * energy * along(p, tt * 1.7))
            width = 2.5 + 7.0 * wobble(p, tt)
            strands.append((at.astype(np.float32), (1 / width).astype(np.float32), shade))
        return {"inv_reach": (1 / reach).astype(np.float32),
                "bright": ((0.6 + 0.4 * self.bright_wave(p, tt)) * (0.6 + 0.4 * energy)).astype(np.float32),
                "color": _palette(p + 0.03 * tt), "strands": strands,
                "strength": np.float32((0.4 + 0.5 * energy) * 1.1)}

    def _columns(self, k, look):
        """Works out chunk k of the unrolled frame's columns into the texture as BGRA, in place in
        buffers kept per chunk (allocating them every frame cost as much as the maths)."""
        cols = self.chunks[k]
        sl = slice(cols[0], cols[-1] + 1)
        n = len(cols)
        buf = self.buffers.get(k)
        if buf is None:
            buf = self.buffers[k] = {name: np.empty((self.rows, n), np.float32)
                                     for name in ("glow", "alpha", "r", "g", "b", "u", "tmp")}
            buf["out"] = np.empty((self.rows, n), np.uint32)
            buf["out2"] = np.empty((self.rows, n), np.uint32)
        glow, alpha, r, g, b, u, tmp = (buf[x] for x in ("glow", "alpha", "r", "g", "b", "u", "tmp"))
        d, core = self.d, self.core
        inv_reach, bright = look["inv_reach"][sl], look["bright"][sl]
        np.multiply(d, -inv_reach, out=glow)
        np.exp(glow, out=glow)
        glow *= bright                                                  # the light
        # the wide soft haze is smooth: worked out every HAZE_STEP rows and repeated
        hz = np.exp(self.d_coarse * (-inv_reach * np.float32(1 / 2.6))) * (np.float32(0.3) * bright)
        np.add(glow, np.repeat(hz, HAZE_STEP, axis=0)[:self.rows], out=alpha)
        alpha += core
        np.multiply(glow, np.float32(0.2), out=tmp)
        tmp += alpha                                                    # light = alpha + 0.2 glow
        color = look["color"][sl]
        white = np.float32(50) * core
        np.multiply(tmp, color[:, 0], out=r); r += white
        np.multiply(tmp, color[:, 1], out=g); g += white
        np.multiply(tmp, color[:, 2], out=b); b += white
        strength = look["strength"]
        for at, inv_width, shade in look["strands"]:
            np.subtract(d, at[sl], out=u)
            u *= inv_width[sl]
            np.square(u, out=u)
            np.negative(u, out=u)
            np.exp(u, out=u)
            u *= strength * shade                                       # the wisp, in the frame's colour there
            np.multiply(u, color[:, 0], out=tmp); r += tmp
            np.multiply(u, color[:, 1], out=tmp); g += tmp
            np.multiply(u, color[:, 2], out=tmp); b += tmp
            u *= np.float32(0.85)
            np.maximum(alpha, u, out=alpha)
        np.clip(alpha, 0, 1, out=alpha)
        alpha *= np.float32(255)
        out, out2 = buf["out"], buf["out2"]
        np.minimum(alpha, b, out=tmp); out[...] = tmp                    # premultiplied BGRA
        np.minimum(alpha, g, out=tmp); out2[...] = tmp; out2 <<= 8; out |= out2
        np.minimum(alpha, r, out=tmp); out2[...] = tmp; out2 <<= 16; out |= out2
        out2[...] = alpha; out2 <<= 24; out |= out2
        self.texture[:, sl] = out

    def _frame(self, now):
        if not self.edges:
            return
        step = 1 / (FPS * FADE_SECONDS)
        self.fade = min(self.target, self.fade + step) if self.fade < self.target else max(self.target,
                                                                                           self.fade - step)
        try:
            level = max(0.0, min(1.0, float(self.level_fn())))
        except Exception:
            level = 0.0
        self.smooth = level if level > self.smooth else self.smooth * 0.9 + level * 0.1
        look = self.look(now - self.t0, self.smooth)
        list(self.pool.map(lambda k: self._columns(k, look), range(len(self.chunks))))
        flat = self.texture.ravel()
        list(self.pool.map(lambda e: e.draw(flat), self.edges))
        blend = BLENDFUNCTION(0, 0, int(255 * self.fade), AC_SRC_ALPHA)
        for edge in self.edges:
            edge.show(blend, self.screen_dc)

    def _loop(self):
        winmm = ctypes.windll.winmm
        while True:
            self.wake.wait()
            winmm.timeBeginPeriod(1)  # sleep() to the millisecond while drawing
            frames, since = 0, time.monotonic()
            try:
                while self.target > 0 or self.fade > 0:
                    started = time.monotonic()
                    self._frame(started)
                    frames += 1
                    if started - since >= 2.0:
                        self.fps_seen, frames, since = frames / (started - since), 0, started
                    time.sleep(max(0.0, 1 / FPS - (time.monotonic() - started)))
            except Exception as e:
                print(f"Glow: {e!r}")
            finally:
                winmm.timeEndPeriod(1)
            self.wake.clear()

    def show(self, level_fn, mode="listen"):
        self.level_fn, self.mode, self.target = level_fn, mode, 1.0
        if not self.edges:
            self.build()
        self.wake.set()

    def set_mode(self, mode):
        self.mode = mode

    def hide(self):
        self.target = 0.0


_glow = {"it": None, "root": None}
_lock = threading.Lock()


def _ensure():
    with _lock:
        if _glow["root"] is not None:
            return
        ready = threading.Event()

        def ui_thread():
            root = tk.Tk()
            root.withdraw()
            _glow["root"] = root
            _glow["it"] = Glow(root)
            ready.set()
            root.mainloop()

        threading.Thread(target=ui_thread, daemon=True).start()
        ready.wait()


def show(level_fn, mode="listen"):
    """Thread-safe: fades the light in (level_fn() -> 0..1 drives it)."""
    _ensure()
    _glow["root"].after(0, lambda: _glow["it"].show(level_fn, mode))


def set_mode(mode):
    """Thread-safe: "listen", "think" or "speak"."""
    if _glow["it"] is not None:
        _glow["it"].set_mode(mode)


def hide():
    """Thread-safe: fades the light out."""
    if _glow["it"] is not None:
        _glow["it"].hide()
