"""Umay's HUD: the orb with the clock, and the panels (weather, vitals, storage, calendar, credits, news,
markets, memory: a click opens the memory editor), in Qt (PySide6), and the memory wallpaper (hud_cortex).
A process of its own, started by hud.py (`main.py --hud <Umay's pid>`): Qt and Umay's Tk can't share a
main thread, and a crash here never takes Umay down.

- The orb shrinks into the bottom-right corner of the active window, on whichever monitor it is, and
  follows it; a cursor coming close pushes it away, and it drifts back when the cursor leaves. Something
  fullscreen there (a game, a video) hides it. With no active window (the desktop) it sits in the middle
  of its monitor with the clock under it when nothing is open there, else in that monitor's corner. Its
  color and motion show what Umay is doing (STATES). Dragged to another monitor, it moves there.
- While VS Code is open (not minimized) the orb sits in that window's bottom-right corner instead,
  whichever window is active; idle there it shows a red WORKING (CODE_STATE). What Umay is doing
  (listening, thinking, speaking...) still shows in its own color.
- Each panel is a window of its own: drag it anywhere, on any monitor; where it is dropped is saved per
  monitor (hud_layout). A monitor that is gone sends its panels to their default spots on the primary,
  and back when it returns. A click (no drag) grows it into its detail view (hud_detail.py) in the middle
  of its monitor; Esc or a click outside shrinks it back. The credits panel and the storage panel's detail
  view took the place of the Ctrl+F1 panel's Credits and Disk tabs.
- The panels stay below other windows, the orb above them.

stdin (from Umay), one JSON object per line: {"state": "idle|listening|thinking|researching|working|
speaking"}, {"level": 0..1} (her voice while she speaks), {"say": text, "id": n, "seconds": s},
{"visible": bool}, {"muted": bool} (she isn't listening), {"open": panel, "toggle": bool} (its detail view; "audio":
Ctrl+F3's panel, hud_audio.py), {"balances": {"openrouter", "tavily"}}, {"disk": {"running", "progress"}},
{"disk_result": {"id", "text", "ok"}}, {"audio": {...}} (the audio panel's state). Its end (Umay closed) ends the HUD.
stdout (to Umay): {"event": "ready"}, {"event": "click"} (the orb), {"event": "say_click", "id": n},
{"orb": [x, y, w, h]} (where the orb is, Qt's logical px), {"event": "balances" | "disk_state" | "disk_scan"},
{"event": "disk_delete", "id": item id}, {"event": "audio_state", "scan": bool},
{"event": "audio_set", "input" | "output": name, "muted": bool}.

Saved in %LOCALAPPDATA%\\Umay\\hud.json (UMAY_HUD_FILE). Log: %LOCALAPPDATA%\\Umay\\hud.log.
"""
import json
import math
import os
import sys
import threading
import time
import traceback
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QConicalGradient, QCursor, QGuiApplication, QLinearGradient, QPainter,
                           QPainterPath, QPen, QRadialGradient)
from PySide6.QtWidgets import QApplication, QMenu, QWidget

import hud_audio
import hud_credits
import hud_data
import hud_detail
import hud_disk
import hud_feeds
import hud_markets
import hud_news
import hud_views
import panel_text
from hud_paint import (C, bar, color, font, gauge, lerp, load_fonts, mix, ring, rule, split_bar, text,
                      weather_icon)
import cortex_graph
import hud_cortex
import hud_layout
import memory_editor
import hud_windows

FPS = 30
CHECK_MS = 400          # how often the orb looks at what is on its monitor
FREE_AFTER_S = 1.2      # the monitor clear this long before the orb comes back to the middle
DODGE_RADIUS = 150      # px: a cursor this close pushes the docked orb away
DODGE_PUSH = 170        # px: how far at most
DOCK_MARGIN = 24
DRAG_PX = 6             # a panel moved further than this was dragged, not clicked
HISTORY = 150           # vitals kept for the detail view's charts: 5 minutes at one reading every 2 s
BALANCES_SECONDS = 600
NEWS_SECONDS = 600
MARKETS_SECONDS = 10    # one request for every symbol
CODE_MARGIN = (14, 30)  # px from VS Code's right and bottom edges: off its scrollbar and status bar
LABEL_H = 20            # the WORKING label under the orb in VS Code
ORB_BIG, ORB_SMALL = 420, 96

STATES = {  # color, label
    "idle": ("#38D6FF", "STANDBY"), "listening": ("#3DF5A8", "LISTENING"), "thinking": ("#A07BFF", "THINKING"),
    "researching": ("#FFB23E", "RESEARCHING"), "working": ("#FF4FA3", "WORKING"), "speaking": ("#E8F6FF", "SPEAKING"),
}
CODE_STATE = ("working", "#FF2E3B")  # the orb in VS Code while Umay is idle or working


def hud_file():
    custom = os.environ.get("UMAY_HUD_FILE", "").strip()
    return Path(custom) if custom else Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Umay" / "hud.json"


def log(text):
    try:
        path = hud_file().with_name("hud.log")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}\n")
    except OSError:
        pass


class Store:
    """hud.json: where each window was dropped, and the weather's place."""

    def __init__(self):
        try:
            self.data = json.loads(hud_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}
        self.data.setdefault("windows", {})

    def save(self):
        path = hud_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)


def screens():
    primary = QGuiApplication.primaryScreen()
    out = []
    for s in QGuiApplication.screens():
        g = s.availableGeometry()
        out.append(hud_layout.Screen(hud_layout.screen_key(s.manufacturer(), s.model(), s.serialNumber(), s.name()),
                                     s.name(), (g.x(), g.y(), g.width(), g.height()), s is primary))
    return out


def logical_rect(rect):
    """A Win32 rect (physical px) as (x, y, w, h) in Qt's logical px. Qt keeps each monitor's top-left at
    its physical place and divides what lies past it by the monitor's scale."""
    l, t, r, b = rect
    cx, cy = (l + r) / 2, (t + b) / 2
    found = QGuiApplication.primaryScreen()
    for s in QGuiApplication.screens():
        g, k = s.geometry(), s.devicePixelRatio()
        if g.x() <= cx < g.x() + g.width() * k and g.y() <= cy < g.y() + g.height() * k:
            found = s
            break
    g, k = found.geometry(), found.devicePixelRatio()
    return g.x() + (l - g.x()) / k, g.y() + (t - g.y()) / k, (r - l) / k, (b - t) / k


def send(obj):
    try:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()
    except (OSError, ValueError):
        pass


# --- the orb ----------------------------------------------------------------------------------------------

def _fibonacci(n):
    pts = []
    for i in range(n):
        y = 1 - 2 * (i + 0.5) / n
        r, th = math.sqrt(1 - y * y), i * 2.399963
        pts.append((math.cos(th) * r, y, math.sin(th) * r))
    return pts


GLOBE = _fibonacci(160)


def paint_orb(p, cx, cy, S, state, col, t, level=None, label=True):
    """The orb (the design's 200-unit drawing scaled to S px) centred on (cx, cy)."""
    k = S / 200
    c = col
    # glow
    g = QRadialGradient(QPointF(cx, cy), S * 0.5)
    g.setColorAt(0, color(c.name(), 0.16 if state == "idle" else 0.26))
    g.setColorAt(0.6, color(c.name(), 0.07))
    g.setColorAt(1, color(c.name(), 0))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(g)
    p.drawEllipse(QPointF(cx, cy), S * 0.5, S * 0.5)

    if state in ("idle", "thinking"):  # the particle globe, turning
        a, b, R = t * 0.35, 0.38, S * 0.25
        ca, sa, cb, sb = math.cos(a), math.sin(a), math.cos(b), math.sin(b)
        p.setPen(Qt.PenStyle.NoPen)
        for i, (x, y, z) in enumerate(GLOBE):
            x2, z2 = x * ca + z * sa, -x * sa + z * ca
            y3, z3 = y * cb - z2 * sb, y * sb + z2 * cb
            depth = (z3 + 1) / 2
            tw = 0.6 + 0.4 * math.sin(t * 1.8 + i * 0.37)
            p.setBrush(color(c.name(), (0.12 + 0.62 * depth) * tw))
            d = max(0.6, k * (0.55 + 1.0 * depth))
            p.drawEllipse(QPointF(cx + x2 * R, cy + y3 * R), d, d)

    if state == "listening":  # ripples
        for i in range(3):
            ph = ((t + i * 0.8) % 2.4) / 2.4
            ring(p, cx, cy, S * 0.28 * (0.72 + 0.7 * ph), color(c.name(), 0.75 * (1 - ph)), max(1.0, k))

    if state == "researching":  # radar sweep
        gr = QConicalGradient(QPointF(cx, cy), (-t * 138) % 360)
        gr.setColorAt(0, color(c.name(), 0.45))
        gr.setColorAt(0.155, color(c.name(), 0))
        gr.setColorAt(1, color(c.name(), 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(gr)
        p.drawEllipse(QPointF(cx, cy), S * 0.33, S * 0.33)

    if state == "speaking":  # bars round the ring, with her voice
        B = 72 if S >= 160 else 36
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(B):
            wave = abs(math.sin(i * 1.37 + t * 6) * math.cos(i * 0.53 - t * 4))
            f = (0.25 + 0.75 * level) * (0.5 + 0.5 * wave) if level is not None else wave
            h = S * (0.02 + 0.08 * f)
            ang = math.radians(i * 360 / B)
            r0 = S * 0.34
            p.save()
            p.translate(cx + math.sin(ang) * r0, cy - math.cos(ang) * r0)
            p.rotate(i * 360 / B)
            p.setBrush(color(c.name(), 0.95))
            p.drawRoundedRect(QRectF(-k, -h, 2 * k, h), k, k)
            p.restore()

    # fixed rings, ticks, dotted orbit (with a soft glow)
    breathe = 1 + (0.035 * math.sin(t * 2 * math.pi / 3.2) if state in ("idle", "listening") else 0)
    main_a = 0.6 if state == "idle" else 1.0
    for w, a in ((7, 0.06), (4.2, 0.12), (2.6, main_a)):
        ring(p, cx, cy, 60 * k * breathe, color(c.name(), a), w * k)
    ring(p, cx, cy, 53 * k, color(c.name(), 0.5), max(0.6, 0.6 * k))
    ring(p, cx, cy, 86 * k, color(c.name(), 0.45), max(0.8, 0.8 * k), dash=[0.6, 4.0])
    pen = QPen(color(c.name(), 0.7), max(1.0, 1.2 * k))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    for i in range(8):
        a = math.radians(i * 45)
        r1, r2 = (91, 96) if i % 2 == 0 else (91, 95)
        p.drawLine(QPointF(cx + math.sin(a) * r1 * k, cy - math.cos(a) * r1 * k),
                   QPointF(cx + math.sin(a) * r2 * k, cy - math.cos(a) * r2 * k))

    # arcs going round
    speed = 360 / 3.2 if state in ("thinking", "researching") else 360 / 22
    rot = -(t * speed) % 360
    for r, start, span, w, a in ((70, 0, 47.5, 1.6, 1), (70, 180, 21.3, 1.6, 1), (77, 90, 11.9, 1.0, 0.7)):
        ring(p, cx, cy, r * k, color(c.name(), a), max(1.0, w * k), start + rot, span, Qt.PenCapStyle.RoundCap)
    if state == "thinking":
        rot2 = (t * 72) % 360
        for start, span in ((0, 56), (180, 25)):
            ring(p, cx, cy, 45 * k, color(c.name(), 1), max(1.0, 1.4 * k), start + rot2, span, Qt.PenCapStyle.RoundCap)
        a = math.radians(-rot2)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(c))
        p.drawEllipse(QPointF(cx + math.cos(a) * 45 * k, cy + math.sin(a) * 45 * k), 2.4 * k, 2.4 * k)
    if state == "working":  # segmented ring turning in steps
        step = int(t / 0.2) % 12 * 30
        ring(p, cx, cy, 66 * k, color(c.name(), 0.75), 3.2 * k, 90 - step, -360, dash=[9 / 3.2, 5.4 / 3.2])
        ring(p, cx, cy, 66 * k, color("#FFFFFF", 0.9), 3.2 * k, 90 - step, -8)

    # name and state
    fw = font(S * 0.08, 600, S * 0.08 * 0.32)
    p.setFont(fw)
    tw = p.fontMetrics().horizontalAdvance("UMAY")
    text(p, cx - tw / 2 + S * 0.08 * 0.16, cy + S * 0.025, "UMAY", fw, color(C["text"], 0.96))
    if label and S >= 160:
        fl = font(max(10, S * 0.028), 600, max(10, S * 0.028) * 0.3)
        p.setFont(fl)
        lw = p.fontMetrics().horizontalAdvance(STATES[state][1])
        text(p, cx - lw / 2, cy + S * 0.09, STATES[state][1], fl, c)


class Orb(QWidget):
    """The orb and, in the middle of its monitor, the clock under it."""

    clicked = Signal()

    def __init__(self, store, umay_pid):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.store, self.own = store, {os.getpid(), umay_pid}
        self.state, self.level = "idle", None
        self.col = QColor(STATES["idle"][0])
        self.t0 = time.monotonic()
        self.mode, self.dock = "center", 0.0      # dock: 0 = middle .. 1 = corner (animated)
        self.target = None                        # the window (x, y, w, h), logical px, for modes "code" and "active"
        self.corner = None                        # where the docked orb is going, eased (VS Code moves)
        self.tag = 0.0                            # the WORKING label under it in VS Code, 0..1 (animated)
        self.target_dock, self.fade = 0.0, 1.0
        self.free_since = None
        self.offset = QPointF(0, 0)               # the dodge
        self.press, self.dragged = None, False
        self.screen_saved = store.data["windows"].get("orb")
        self.home = None                          # hud_layout.Screen it lives on
        self.relocate()
        frame = QTimer(self)
        frame.timeout.connect(self.tick)
        frame.start(int(1000 / FPS))
        check = QTimer(self)
        check.timeout.connect(self.check)
        check.start(CHECK_MS)

    # where it is
    def relocate(self):
        _, _, self.home = hud_layout.place(self.screen_saved, "center", (10, 10), screens())
        self.apply()

    def dock_area(self):
        """Where the docked orb lives: VS Code's or the active window, or its monitor's work area."""
        return self.target if self.mode in ("code", "active") and self.target else self.home.area

    def target_corner(self):
        ax, ay, aw, ah = self.dock_area()
        right, bottom = (CODE_MARGIN[0], CODE_MARGIN[1] + LABEL_H) if self.mode == "code" else (DOCK_MARGIN, DOCK_MARGIN)
        return QPointF(ax + aw - right - ORB_SMALL / 2 - 12, ay + ah - bottom - ORB_SMALL / 2 - 12)

    def centers(self):
        ax, ay, aw, ah = self.home.area
        big = QPointF(ax + aw / 2, ay + ah / 2 - 90)
        if self.merged_here():  # the wallpaper's ring is centred on the whole monitor
            g = QGuiApplication.primaryScreen().geometry()
            big = QPointF(g.x() + g.width() / 2, g.y() + g.height() / 2)
        return big, self.corner or self.target_corner()

    # On the primary monitor's desktop the wallpaper draws her (hud_cortex: her ring is the memory's outer ring,
    # the owner's design of 2026-11-07); the orb is then only a click target there, and shows as it docks.
    wallpaper = None  # hud_cortex.Wallpaper, set by main()

    def merged_here(self):
        w = self.wallpaper
        return bool(w is not None and self.home is not None and self.home.primary and w.showing())

    def apply(self):
        """Window geometry for the current dock amount and dodge offset."""
        e = self.dock * self.dock * (3 - 2 * self.dock)
        big, small = self.centers()
        centre = QPointF(lerp(big.x(), small.x(), e), lerp(big.y(), small.y(), e)) + self.offset * e
        w, h = lerp(520, ORB_SMALL + 24, e), lerp(620, ORB_SMALL + 24, e) + LABEL_H * self.tag * e
        oy = lerp(240, (ORB_SMALL + 24) / 2, e)
        self.setGeometry(int(centre.x() - w / 2), int(centre.y() - oy), int(w), int(h))

    def orb_centre(self):
        e = self.dock * self.dock * (3 - 2 * self.dock)
        y = lerp(240, lerp(620, ORB_SMALL + 24, e) / 2, e)  # the window without the label
        return QPointF(self.width() / 2, y), lerp(ORB_BIG, ORB_SMALL, e), e

    def shown(self):
        """The state drawn and its color: in VS Code, idle or working is a red WORKING."""
        if self.mode == "code" and self.state in ("idle", "working"):
            return CODE_STATE
        return self.state, STATES[self.state][0]

    # what is on its monitor
    def check(self):
        if not self.isVisible() and self.mode != "hidden":
            return
        try:
            windows = hud_windows.top_windows()
            seen, w = hud_windows.follow(windows, hud_windows.foreground(), self.own, hud_windows.monitor_of_rect)
            if seen == "keep":
                return
            if w is not None:
                self.target = logical_rect(hud_windows.frame_rect(w.hwnd))
            elif seen == "none":
                seen = hud_windows.judge(windows, hud_windows.monitor_of(int(self.winId())), self.own)
        except Exception as e:
            log(f"orb check failed: {e}")
            return
        now = time.monotonic()
        if seen == "free":
            self.free_since = self.free_since or now
            if now - self.free_since < FREE_AFTER_S:
                return
        else:
            self.free_since = None
        mode = {"free": "center", "window": "dock", "fullscreen": "hidden", "hidden": "hidden", "code": "code",
                "active": "active"}[seen]
        if mode != self.mode:
            self.mode = mode
            self.target_dock = 0.0 if mode == "center" else 1.0

    def tick(self):
        t = time.monotonic() - self.t0
        self.col = mix(self.col, QColor(self.shown()[1]), 0.15)
        target = self.target_corner()
        self.corner = target if self.corner is None or self.dock < 0.05 else self.corner + (target - self.corner) * 0.2
        if self.mode == "code":
            self.tag = min(1.0, self.tag + 0.1)
        elif self.mode != "hidden":  # hidden in VS Code (a fullscreen video over it): it comes back as it was
            self.tag = max(0.0, self.tag - 0.1)
        if self.dock != self.target_dock:
            step = 1 / (0.45 * FPS)
            self.dock = min(self.target_dock, self.dock + step) if self.target_dock > self.dock \
                else max(self.target_dock, self.dock - step)
        self.fade = max(0.0, self.fade - 0.12) if self.mode == "hidden" else min(1.0, self.fade + 0.12)
        if self.fade <= 0 and self.isVisible():
            self.hide()
        elif self.fade > 0 and not self.isVisible() and self.wanted:
            self.show()
        self.dodge()
        if self.press is None:
            self.apply()
        self.t = t
        self.frames = getattr(self, "frames", 0) + 1
        if self.wallpaper is not None and self.frames % 3 == 0:  # ~10 a second: her state on the ring
            state = self.shown()[0]
            self.wallpaper.orb({"label": STATES[state][1], "color": self.col.name(),
                                "level": round(self.level or 0, 2) if state == "speaking" else 0})
        self.update()

    def dodge(self):
        """Docked: pushed away from a cursor that comes close, drifting back when it leaves."""
        target = QPointF(0, 0)
        if self.mode in ("dock", "code", "active") and self.dock > 0.95:
            _, small = self.centers()
            cur = QPointF(QCursor.pos())
            d = small - cur
            dist = math.hypot(d.x(), d.y())
            if dist < DODGE_RADIUS:
                if dist < 1:
                    d, dist = QPointF(-1, -1), math.sqrt(2)
                push = (DODGE_RADIUS - dist) / DODGE_RADIUS * DODGE_PUSH + 30
                target = QPointF(d.x() / dist * push, d.y() / dist * push)
                ax, ay, aw, ah = self.dock_area()
                half = (ORB_SMALL + 24) / 2
                x = min(max(small.x() + target.x(), ax + half), ax + aw - half)
                y = min(max(small.y() + target.y(), ay + half), ay + ah - half)
                target = QPointF(x, y) - small
        self.offset += (target - self.offset) * 0.2

    wanted = True

    # input
    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.press, self.dragged = ev.globalPosition(), False
            self.grab_at = ev.position()

    def mouseMoveEvent(self, ev):
        if self.press is None or self.mode in ("dock", "code", "active"):
            return
        if (ev.globalPosition() - self.press).manhattanLength() > 6:
            self.dragged = True
        if self.dragged:
            g = ev.globalPosition() - self.grab_at
            self.move(int(g.x()), int(g.y()))

    def mouseReleaseEvent(self, ev):
        if self.press is None:
            return
        self.press = None
        if not self.dragged:
            self.clicked.emit()
            return
        g = self.geometry()
        self.screen_saved = hud_layout.remember(g.x(), g.y(), (g.width(), g.height()), screens())
        self.screen_saved["offset"] = [0, 0]
        self.store.data["windows"]["orb"] = self.screen_saved
        self.store.save()
        self.relocate()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        centre, S, e = self.orb_centre()
        merged = self.merged_here()
        if merged and e < 0.5:  # nearly invisible, but it still takes the click that opens the chat box
            p.setOpacity(1.0)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 2))
            p.drawEllipse(centre, S * 0.4, S * 0.4)
        p.setOpacity(self.fade * (e if merged else 1.0))  # it shows as it leaves for the corner
        state = self.shown()[0]
        paint_orb(p, centre.x(), centre.y(), S, state, self.col, getattr(self, "t", 0.0), self.level)
        if self.tag * e > 0.01:  # too small for paint_orb's own label: it goes under the orb
            p.setOpacity(self.fade * self.tag * e)
            text(p, centre.x(), centre.y() + S / 2 + 14, STATES[state][1], font(13, 700, 2.4), self.col, "center")
            p.setOpacity(self.fade)
        a = 0.0 if merged else (1 - e) ** 2  # merged: the wallpaper shows the clock inside her ring
        if a > 0.01:
            p.setOpacity(self.fade * a)
            now = datetime.now()
            cx, y = self.width() / 2, centre.y() + S / 2 + 100
            hm = now.strftime("%H:%M")
            f = font(112, 400, 112 * 0.06)
            p.setFont(f)
            w = p.fontMetrics().horizontalAdvance(hm)
            text(p, cx - w / 2 - 14, y, hm, f, C["text"])
            text(p, cx + w / 2 - 6, y, now.strftime("%S"), font(22, 400, mono=True), C["cyan"])
            date = now.strftime("%A · %d %B %Y").upper()
            text(p, cx, y + 40, date, font(15, 600, 15 * 0.42), C["cyan2"], "center")
        p.end()


# --- panels ------------------------------------------------------------------------------------------------

class Panel(QWidget):
    """A HUD panel: a window of its own, below other windows, dragged anywhere; its place is saved."""

    NAME, SLOT, SIZE, INDEX, TITLE = "", "top-left", (432, 500), "01", ""

    def __init__(self, store):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
                         | Qt.WindowType.WindowStaysOnBottomHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.store, self.data = store, None
        self.press = self.press_at = None
        self.dragged = self.hover = False
        self.setFixedSize(*self.SIZE)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.relocate()

    clicked = Signal(object)

    def relocate(self):
        x, y, _ = hud_layout.place(self.store.data["windows"].get(self.NAME), self.SLOT, self.SIZE, screens())
        self.move(x, y)

    def set_data(self, data):
        self.data = data
        self.update()

    def sub(self):
        return ""

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.press, self.press_at, self.dragged = ev.position(), ev.globalPosition(), False

    def mouseMoveEvent(self, ev):
        if self.press is None:
            return
        if not self.dragged and (ev.globalPosition() - self.press_at).manhattanLength() <= DRAG_PX:
            return
        self.dragged = True
        g = ev.globalPosition() - self.press
        self.move(int(g.x()), int(g.y()))

    def enterEvent(self, _):
        self.hover = True
        self.update()

    def leaveEvent(self, _):
        self.hover = False
        self.update()

    def mouseReleaseEvent(self, ev):
        if self.press is None:
            return
        self.press = None
        if not self.dragged:  # a click: its detail view
            self.clicked.emit(self)
            return
        saved = hud_layout.remember(self.x(), self.y(), self.SIZE, screens())
        self.store.data["windows"][self.NAME] = saved
        self.store.save()
        self.relocate()  # clamped inside the monitor it was dropped on

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        w, h = self.width(), self.height()
        g = QLinearGradient(0, 0, 0, h)
        g.setColorAt(0, QColor(9, 22, 30, 205))
        g.setColorAt(1, QColor(3, 9, 13, 175))
        p.fillRect(QRectF(0, 0, w, h), g)
        p.setPen(QPen(color(C["cyan"], 0.45 if self.hover else 0.16), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(QRectF(0.5, 0.5, w - 1, h - 1))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(C["cyan"]))
        for x, y, sx, sy in ((0, 0, 1, 1), (w, 0, -1, 1), (0, h, 1, -1), (w, h, -1, -1)):
            p.drawRect(QRectF(x if sx > 0 else x - 14, y if sy > 0 else y - 2, 14, 2))
            p.drawRect(QRectF(x if sx > 0 else x - 2, y if sy > 0 else y - 14, 2, 14))
        text(p, 26, 38, f"{self.INDEX} // {self.TITLE}", font(13, 600, 3.1), C["cyan2"])
        text(p, w - 26, 38, self.sub(), font(12, 500, 1.9), C["muted"], "right")
        if self.data is None:
            text(p, 26, 80, "ACQUIRING…", font(13, 600, 2.6), C["muted"])
        else:
            try:
                self.content(p, w, h)
            except Exception as e:  # a surprising answer from a source: the panel shows it, the HUD lives on
                log(f"{self.NAME} paint failed: {e}")
                text(p, 26, 80, "NO DATA", font(13, 600, 2.6), C["muted"])
        p.end()

    def content(self, p, w, h):
        raise NotImplementedError


def label_value(p, x, y, label, value, size=18):
    text(p, x, y, label.upper(), font(12, 600, 2.4), C["muted"])
    text(p, x, y + 24, value, font(size, 400, mono=True), C["text"])


class WeatherPanel(Panel):
    NAME, SLOT, SIZE, INDEX, TITLE = "weather", "top-left", (432, 500), "01", "ATMOSPHERE"

    def sub(self):
        return self.data["place"] if self.data else ""


    def content(self, p, w, h):
        d = self.data

        def n(key, unit=""):  # a value Open-Meteo didn't send shows as a dash
            return "—" if d.get(key) is None else f"{d[key]:.0f}{unit}"
        weather_icon(p, 26, 64, d["icon"])
        tw = text(p, 116, 136, n("temp"), font(84, 500), C["text"])
        text(p, 120 + tw, 104, "°C", font(30, 500), C["cyan2"])
        text(p, 230, 98, d["text"], font(16, 600, 3.5), C["cyan2"])
        text(p, 230, 122, f"FEELS {n('feels', '°')} · H {n('high', '°')} · L {n('low', '°')}", font(12, 400, mono=True),
             C["muted"])
        cells = [("Humidity", n("humidity", "%")), ("Wind", f"{n('wind')} km/h {d['wind_dir']}"),
                 ("Rain", n("rain_chance", "%")), ("Pressure", n("pressure", " hPa")),
                 ("UV index", n("uv")), ("Visibility", n("visibility_km", " km"))]
        for i, (lab, val) in enumerate(cells):
            label_value(p, 26 + (i % 3) * 132, 176 + (i // 3) * 54, lab, val)
        rule(p, 26, 290, w - 52)
        text(p, 26, 316, "NEXT 24 H", font(12, 600, 2.4), C["muted"])
        text(p, w - 26, 316, "TEMP   ▮ RAIN %", font(11, 400, mono=True), C["muted"], "right")
        hours = d["hourly"]
        if len(hours) >= 2:
            x0, y0, cw, ch = 26, 328, w - 52, 104
            temps = [hh["temp"] for hh in hours]
            lo, hi = min(temps) - 1, max(temps) + 1
            xs = [x0 + 8 + i * (cw - 16) / (len(hours) - 1) for i in range(len(hours))]
            ty = [y0 + ch - 10 - (tt - lo) / (hi - lo) * (ch - 26) for tt in temps]
            ry = [y0 + ch - 10 - hh["rain"] * 0.6 for hh in hours]
            for ys, line_col, fill_col, dash in ((ry, color(C["amber"], 0.7), color(C["amber"], 0.14), [2, 3]),
                                                 (ty, QColor(C["cyan"]), color(C["cyan"], 0.10), None)):
                path = QPainterPath(QPointF(xs[0], ys[0]))
                for x, y in zip(xs[1:], ys[1:]):
                    path.lineTo(x, y)
                area = QPainterPath(path)
                area.lineTo(xs[-1], y0 + ch - 10)
                area.lineTo(xs[0], y0 + ch - 10)
                area.closeSubpath()
                p.fillPath(area, fill_col)
                pen = QPen(line_col, 1.8 if dash is None else 1)
                if dash:
                    pen.setDashPattern(dash)
                pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                p.strokePath(path, pen)
            p.setPen(QPen(color(C["text"], 0.6), 1, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(xs[0], y0 + 4), QPointF(xs[0], y0 + ch - 10))
            p.setPen(QPen(QColor(C["text"]), 1.5))
            p.setBrush(QColor("#000000"))
            p.drawEllipse(QPointF(xs[0], ty[0]), 3.5, 3.5)
            text(p, xs[0] + 7, ty[0] - 8, f"{temps[0]:.0f}°", font(11, 400, mono=True), C["text"])
            for i in range(0, len(hours), 3):
                text(p, xs[i], y0 + ch + 6, hours[i]["hour"], font(10, 400, mono=True), C["muted"], "center")
        text(p, 26, h - 26, "SUNRISE", font(12, 400, mono=True), C["muted"])
        text(p, 90, h - 26, d["sunrise"], font(12, 400, mono=True), C["text"])
        text(p, w - 26, h - 26, d["sunset"], font(12, 400, mono=True), C["text"], "right")
        text(p, w - 72, h - 26, "SUNSET", font(12, 400, mono=True), C["muted"], "right")


def rate(bps):
    if bps is None:
        return "—"
    for unit, size in (("GB/s", 2 ** 30), ("MB/s", 2 ** 20), ("KB/s", 2 ** 10)):
        if bps >= size:
            return f"{bps / size:.1f} {unit}"
    return f"{bps:.0f} B/s"


class VitalsPanel(Panel):
    NAME, SLOT, SIZE, INDEX, TITLE = "vitals", "top-right", (432, 500), "02", "SYSTEM VITALS"
    cpu = "CPU"

    def sub(self):
        if not self.data:
            return ""
        gpu = self.data.get("gpu")
        hot = self.data["cpu"] > 90 or (gpu and gpu["temp"] >= 85)
        return "UNDER LOAD" if hot else "NOMINAL"

    def content(self, p, w, h):
        d, gpu = self.data, self.data.get("gpu")
        gauge(p, 26 + 66, 128, d["cpu"] / 100, C["cyan"], f"{d['cpu']:.0f}%", "CPU", self.cpu)
        if gpu:
            gauge(p, w / 2, 128, gpu["util"] / 100, C["cyan"], f"{gpu['util']:.0f}%", "GPU", gpu["name"][:18])
            tcol = C["mint"] if gpu["temp"] < 70 else C["amber"] if gpu["temp"] < 85 else C["hot"]
            gauge(p, w - 26 - 66, 128, gpu["temp"] / 100, tcol, f"{gpu['temp']:.0f}°", "GPU TEMP", "LIMIT 87°C")
        rule(p, 26, 262, w - 52)
        text(p, 26, 292, "MEMORY", font(12, 600, 2.4), C["muted"])
        text(p, w - 26, 292, f"{d['ram_used']:.1f} / {d['ram_total']:.0f} GB", font(12, 400, mono=True), C["text"], "right")
        # Umay's models in their own color inside the used RAM (the owner, 2026-11-11), like the VRAM below
        models = min(d.get("ram_models", 0.0), d["ram_used"])
        split_bar(p, 26, 302, w - 52, d["ram_total"], ((models, C["amber"], 1.0, f"UMAY MODELS {models:.1f}"),
                                                       (d["ram_used"] - models, C["cyan2"], 0.45,
                                                        f"OTHER {d['ram_used'] - models:.1f}")))
        if gpu:
            text(p, 26, 356, "VRAM", font(12, 600, 2.4), C["muted"])
            text(p, w - 26, 356, f"{gpu['mem_used']:.1f} / {gpu['mem_total']:.1f} GB", font(12, 400, mono=True),
                 C["text"], "right")
            llm = min(d["llm_vram"], gpu["mem_used"])
            other = max(0.0, gpu["mem_used"] - llm)
            split_bar(p, 26, 366, w - 52, gpu["mem_total"], ((llm, C["cyan"], 1.0, f"UMAY LLM {llm:.1f}"),
                                                             (other, C["cyan2"], 0.45, f"OTHER {other:.1f}")))
        rule(p, 26, 420, w - 52)
        up = int(d["uptime"])
        f = font(12, 400, mono=True)
        for i, (lab, val) in enumerate((("NET ↓", rate(d["down"])), ("NET ↑", rate(d["up"])),
                                        ("UP", f"{up // 3600:02}:{up % 3600 // 60:02}:{up % 60:02}"))):
            x = 26 + i * 132
            lw = text(p, x, 452, lab + " ", f, C["muted"])
            text(p, x + lw, 452, val, f, C["text"])


class StoragePanel(Panel):
    NAME, SLOT, SIZE, INDEX, TITLE = "storage", "bottom-right", (432, 364), "03", "STORAGE"

    def sub(self):
        return f"LOCAL DISK {self.data['drives'][0]['name']}" if self.data and self.data["drives"] else ""

    def content(self, p, w, h):
        drives = self.data["drives"]
        if not drives:
            return
        main = drives[0]
        frac = main["used"] / main["total"]
        cx, cy = 26 + 64, 64 + 64
        ring(p, cx, cy, 54, color(C["cyan"], 0.12), 8, dash=[3 / 8 * 4, 2 / 8 * 4])
        ring(p, cx, cy, 54, QColor(C["cyan"]), 8, 90, -360 * frac)
        ring(p, cx, cy, 44, color(C["cyan"], 0.3), 1)
        text(p, cx, cy + 6, f"{frac * 100:.0f}%", font(24, 400, mono=True), C["text"], "center")
        text(p, cx, cy + 24, "USED", font(10, 600, 2), C["muted"], "center")
        fw = text(p, 176, 124, f"{main['free']:.0f}", font(40, 500), C["text"])
        text(p, 184 + fw, 124, "GB FREE", font(18, 500), C["cyan2"])
        text(p, 176, 148, f"{main['used']:.0f} OF {main['total']:.0f} GB USED", font(12, 400, mono=True), C["muted"])
        rule(p, 26, 214, w - 52)
        y = 244
        for dr in drives[1:3]:
            text(p, 26, y, f"DRIVE {dr['name']}", font(12, 600, 2.4), C["muted"])
            text(p, w - 26, y, f"{dr['free']:.0f} GB FREE", font(12, 400, mono=True), C["text"], "right")
            bar(p, 26, y + 9, w - 52, dr["used"] / dr["total"])
            y += 40
        for row in self.data["umay"][: max(0, 3 - (len(drives) - 1))]:
            text(p, 26, y, row["name"], font(12, 600, 2.4), C["muted"])
            text(p, w - 26, y, f"{row['size']:.1f} GB", font(12, 400, mono=True), C["text"], "right")
            y += 34


class CalendarPanel(Panel):
    NAME, SLOT, SIZE, INDEX, TITLE = "calendar", "bottom-left", (432, 364), "04", "CHRONOLOG"

    def sub(self):
        return self.data["today"].strftime("%B %Y").upper() if self.data else ""

    def content(self, p, w, h):
        today = self.data["today"]
        first = today.replace(day=1)
        lead = first.weekday()
        days = ((first.replace(month=first.month % 12 + 1, year=first.year + first.month // 12) - timedelta(days=1)).day)
        cw = (w - 52) / 7
        f = font(13, 400, mono=True)
        for i, wd in enumerate(("MO", "TU", "WE", "TH", "FR", "SA", "SU")):
            text(p, 26 + cw * i + cw / 2, 70, wd, font(10, 400, 1.2, mono=True), C["muted"], "center")
        for d in range(1, days + 1):
            cell = lead + d - 1
            x, y = 26 + cw * (cell % 7), 80 + 26 * (cell // 7)
            if d == today.day:
                p.fillRect(QRectF(x + 2, y, cw - 4, 22), QColor(C["cyan"]))
                text(p, x + cw / 2, y + 16, f"{d:02}", f, "#000000", "center")
                continue
            text(p, x + cw / 2, y + 16, f"{d:02}", f, C["dim"] if d < today.day else C["soft"], "center")
            if d in self.data["marked"]:
                p.fillRect(QRectF(x + cw / 2 - 10, y + 20, 20, 2), QColor(C["amber"]))
            elif d in self.data.get("holidays", ()):
                p.fillRect(QRectF(x + cw / 2 - 10, y + 20, 20, 2), QColor(C["mint"]))
        top = 80 + 26 * ((lead + days + 6) // 7) + 10
        rule(p, 26, top, w - 52)
        events = self.data["events"]
        if not events:
            text(p, 26, top + 30, "NO ALARMS SET", font(12, 600, 2.4), C["muted"])
        for i, ev in enumerate(events[:3]):
            y = top + 30 + i * 26
            when = ev["at"].strftime("%a %d").upper() + (f"  {ev['holiday']}" if ev.get("holiday")
                                                         else ev["at"].strftime(" %H:%M"))
            text(p, 26, y, when, font(12, 400, mono=True), C["mint"] if ev.get("holiday") else C["cyan2"])
            name = ev["name"] if len(ev["name"]) <= 30 else ev["name"][:29] + "…"
            text(p, 136, y, name, font(16, 500), C["text"])


class CreditsPanel(Panel):
    NAME, SLOT, SIZE, INDEX, TITLE = "credits", "top-center", (432, 236), "05", "CREDITS"
    balances, lang = None, "en"

    def sub(self):
        return "API"

    def content(self, p, w, h):
        d = self.data
        o, t = hud_credits.balances_text(self.lang, self.balances)
        label_value(p, 26, 72, "OpenRouter", o, 20)
        label_value(p, 232, 72, "Tavily", t, 20)
        rule(p, 26, 118, w - 52)
        for i, (key, lab) in enumerate((("today", "TODAY"), ("week", "7 DAYS"), ("month", "30 DAYS"))):
            cost, count = d[key]
            x = 26 + i * 132
            text(p, x, 144, lab, font(12, 600, 2.4), C["muted"])
            text(p, x, 168, panel_text.money(cost, 3), font(17, 400, mono=True), C["text"])
            text(p, x, 186, f"{count} REQ", font(10, 400, mono=True), C["muted"])
        last = d.get("last")
        if last:
            when = time.strftime("%H:%M", time.localtime(last["at"]))
            what = (last["what"] or "—").replace("\n", " ")
            what = what if len(what) <= 40 else what[:39] + "…"
            text(p, 26, h - 22, f"LAST {when}", font(11, 400, mono=True), C["muted"])
            text(p, 112, h - 22, what, font(13, 500), C["soft"])


class MemoryPanel(Panel):
    """What Umay remembers (memory_cortex, read through cortex_graph). A click opens the memory editor, the
    owner's button next to "open the memory editor" (2026-11-07)."""
    NAME, SLOT, SIZE, INDEX, TITLE = "memory", "bottom-center", (432, 236), "08", "MEMORY CORTEX"

    def sub(self):
        return "CLICK · EDIT"

    def content(self, p, w, h):
        d = self.data
        if not d:
            text(p, 26, 80, "NO MEMORY YET", font(13, 600, 2.6), C["muted"])
            return
        neurons = f"{d.get('neurons', 0):,}"
        f = font(36, 500, mono=True)
        text(p, 26, 92, neurons, f, C["text"])
        p.setFont(f)
        text(p, 26 + p.fontMetrics().horizontalAdvance(neurons) + 8, 92, "NEURONS", font(10, 600, 2.2), C["cyan2"])
        for i, (key, lab) in enumerate((("user", "FACTS"), ("self", "SELF"), ("inbox", "INBOX"),
                                        ("episodes", "MEMORIES"))):
            label_value(p, 194 + i * 58, 62, lab, f"{d[key]:,}", 15)
        rule(p, 26, 116, w - 52)
        state = (d.get("state") or "idle").upper()
        text(p, 26, 142, "STATE", font(11, 600, 2.4), C["muted"])
        text(p, 96, 142, state, font(12, 600, 2.4), C["mint"] if state != "IDLE" else C["muted"])
        last = d.get("last")
        text(p, 26, 172, "LAST FILED", font(11, 600, 2.4), C["muted"])
        if last:
            path = last["path"].upper()
            text(p, 140, 172, path if len(path) <= 34 else path[:33] + "…", font(10, 500, 1.6), C["cyan2"])
            label = last["label"] if len(last["label"]) <= 44 else last["label"][:43] + "…"
            text(p, 26, 200, label, font(15, 500), C["soft"])
        else:
            text(p, 140, 172, "—", font(12, 500), C["muted"])


class PanelMenu(QMenu):
    """The panels button's list: a click turns one panel on or off and the list stays open for the next (the
    owner's ask, 2026-11-07: "one by one, not all at once")."""

    def mouseReleaseEvent(self, ev):
        action = self.actionAt(ev.position().toPoint())
        if action is not None and action.isCheckable():
            action.trigger()
            return
        super().mouseReleaseEvent(ev)


MENU_STYLE = (
    "QMenu{background:rgba(3,12,17,235);border:1px solid rgba(56,214,255,90);padding:6px 0;"
    "font-family:'Rajdhani';font-size:13px;font-weight:600;letter-spacing:2px;color:#B8D4DE}"
    "QMenu::item{padding:6px 22px 6px 30px}"
    "QMenu::item:selected{background:rgba(56,214,255,40);color:#E6F7FF}"
    "QMenu::indicator{width:12px;height:12px;left:10px}"
    "QMenu::indicator:checked{background:#38D6FF;border:1px solid #38D6FF}"
    "QMenu::indicator:unchecked{border:1px solid rgba(56,214,255,140)}"
    "QMenu::separator{height:1px;background:rgba(56,214,255,50);margin:5px 12px}")


class PanelsButton(QWidget):
    """The primary monitor's bottom-right button: a list of the panels, each turned on or off on its own (the
    owner's asks, 2026-11-07: "the UI gets too busy", then "one by one"). The orb, her ring and the memory stay.
    The hidden ones are remembered in hud.json ("panels_hidden": [names])."""

    SIZE, MARGIN = 44, 14
    toggled = Signal(object)  # the set of hidden panel names, after a change

    def __init__(self, store, panels=()):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
                         | Qt.WindowType.WindowStaysOnBottomHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.store, self.hover = store, False
        self.panels = [(p.NAME, p.TITLE.title()) for p in panels]  # (name, "Atmosphere") in the HUD's order
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("HUD panels")
        self.relocate()

    def hidden(self):
        """The names of the hidden panels (an older hud.json kept True / False for all of them)."""
        saved = self.store.data.get("panels_hidden")
        if isinstance(saved, bool):
            return {name for name, _ in self.panels} if saved else set()
        return set(saved or [])

    def set_hidden(self, names):
        self.store.data["panels_hidden"] = sorted(names)
        self.store.save()
        self.update()
        self.toggled.emit(set(names))

    def menu(self):
        m = PanelMenu(self)
        m.setStyleSheet(MENU_STYLE)
        hidden = self.hidden()
        for name, title in self.panels:
            a = m.addAction(title.upper())
            a.setCheckable(True)
            a.setChecked(name not in hidden)
            a.toggled.connect(lambda on, n=name: self.set_hidden(self.hidden() - {n} if on else self.hidden() | {n}))
        m.addSeparator()

        def everything(show):
            self.set_hidden(set() if show else {n for n, _ in self.panels})
            for a in m.actions():
                if a.isCheckable():
                    a.blockSignals(True)
                    a.setChecked(show)
                    a.blockSignals(False)
        m.addAction("SHOW ALL").triggered.connect(lambda: everything(True))
        m.addAction("HIDE ALL").triggered.connect(lambda: everything(False))
        return m

    def relocate(self):
        g = QGuiApplication.primaryScreen().availableGeometry()
        self.move(g.x() + g.width() - self.SIZE - self.MARGIN, g.y() + g.height() - self.SIZE - self.MARGIN)

    def enterEvent(self, _):
        self.hover = True
        self.update()

    def leaveEvent(self, _):
        self.hover = False
        self.update()

    def mouseReleaseEvent(self, ev):
        if ev.button() != Qt.MouseButton.LeftButton:
            return
        m = self.menu()
        size = m.sizeHint()
        g = self.geometry()
        m.exec(QPointF(g.right() - size.width(), g.top() - size.height() - 6).toPoint())  # above it, right-aligned

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        s, on = self.SIZE, len(self.hidden()) < len(self.panels)  # filled while any panel shows
        p.setBrush(QColor(3, 12, 17, 200))
        p.setPen(QPen(color(C["cyan"], 0.8 if self.hover else 0.45), 1.2))
        p.drawEllipse(QRectF(1, 1, s - 2, s - 2))
        cell, gap = 7, 3  # four panels: filled when they show, outlined when hidden
        x0 = (s - 2 * cell - gap) / 2
        for i in range(2):
            for j in range(2):
                r = QRectF(x0 + i * (cell + gap), x0 + j * (cell + gap), cell, cell)
                if on:
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(color(C["cyan"], 0.9 if self.hover else 0.7))
                else:
                    p.setPen(QPen(color(C["cyan"], 0.7), 1))
                    p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(r)
        p.end()


class MutedMic(QWidget):
    """A struck-through microphone right above the panels button, only while she isn't listening (the audio
    panel's STOP LISTENING). A click opens that panel (Ctrl+F3), which also grows from this spot."""

    SIZE, MARGIN, GAP = PanelsButton.SIZE, PanelsButton.MARGIN, 8

    def __init__(self):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
                         | Qt.WindowType.WindowStaysOnBottomHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.muted = False
        self.on_click = None
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setToolTip("Not listening. Click (or Ctrl+F3): the audio panel")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.relocate()

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton and self.on_click is not None:
            self.on_click()

    def relocate(self):
        g = QGuiApplication.primaryScreen().availableGeometry()
        self.move(g.x() + g.width() - self.SIZE - self.MARGIN,
                  g.y() + g.height() - 2 * self.SIZE - self.MARGIN - self.GAP)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        s = self.SIZE
        p.setBrush(QColor(3, 12, 17, 200))
        p.setPen(QPen(color(C["hot"], 0.7), 1.2))
        p.drawEllipse(QRectF(1, 1, s - 2, s - 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(color(C["soft"], 0.9), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawRoundedRect(QRectF(17, 9, 10, 17), 5, 5)  # the microphone
        p.drawArc(QRectF(13, 15, 18, 16), 180 * 16, 180 * 16)  # its cradle
        p.drawLine(QPointF(22, 31), QPointF(22, 35))
        p.drawLine(QPointF(17, 35), QPointF(27, 35))
        p.setPen(QPen(color(C["hot"]), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(12, 11), QPointF(32, 34))
        p.end()


def two_lines(p, s, f, width):
    """`s` wrapped into at most two lines of `width` px, the second cut with "…"."""
    p.setFont(f)
    fm = p.fontMetrics()
    words, first = s.split(), ""
    while words and fm.horizontalAdvance((first + " " + words[0]).strip()) <= width:
        first = (first + " " + words.pop(0)).strip()
    if not first:  # one long word
        return [fm.elidedText(s, Qt.TextElideMode.ElideRight, int(width))]
    rest = " ".join(words)
    return [first] + ([fm.elidedText(rest, Qt.TextElideMode.ElideRight, int(width))] if rest else [])


class NewsPanel(Panel):
    """The latest articles about who is followed (hud_feeds.watch_list), then the sections' headlines."""

    NAME, SLOT, SIZE, INDEX, TITLE = "news", "middle-left", (432, 560), "06", "NEWSFEED"
    LINE_H, WATCH_ROWS, TOPIC_ROWS = 17, 4, 3

    def sub(self):
        return f"UPDATED {time.strftime('%H:%M', time.localtime(self.data['at']))}" if self.data else ""

    def content(self, p, w, h):
        items, now = self.data["items"], time.time()
        watch = hud_feeds.spread([i for i in items if i["kind"] == "watch"], self.WATCH_ROWS)
        topic = [i for i in items if i["kind"] == "topic"][: self.TOPIC_ROWS]
        y = self.rows(p, w, 72, h * 0.6, "FOLLOWING", watch, C["amber"], now)
        rule(p, 26, y + 6, w - 52)
        self.rows(p, w, y + 28, h - 12, "HEADLINES", topic, C["cyan2"], now)

    def rows(self, p, w, y, bottom, title, items, tag_col, now):
        """A section's rows from y, as many as end above `bottom`; returns where it ended."""
        text(p, 26, y, title, font(12, 600, 2.4), C["muted"])
        y += 10
        if not items:
            text(p, 26, y + 22, "NOTHING NEW", font(13, 500), C["dim"])
        for item in items:
            lines = two_lines(p, item["title"], font(15, 500), w - 52)
            if y + 26 + len(lines) * self.LINE_H > bottom:
                break
            f = font(11, 700, 1.6)
            tw = text(p, 26, y + 14, " · ".join(item["tags"][:2]), f, tag_col)
            text(p, 26 + tw, y + 14, f"  {item['source'][:24]} · {hud_feeds.age(item['at'], now)}",
                 font(11, 400, mono=True), C["muted"])
            for k, line in enumerate(lines):
                text(p, 26, y + 33 + k * self.LINE_H, line, font(15, 500), C["text"])
            y += 26 + len(lines) * self.LINE_H + 8
        return y if items else y + 34


class MarketsPanel(Panel):
    """UMAY_MARKET_SYMBOLS: price, change since the last close, today's curve; a dot for an open market."""

    NAME, SLOT, SIZE, INDEX, TITLE = "markets", "middle-right", (432, 600), "07", "MARKETS"
    ROW_H = 46

    def sub(self):
        return f"UPDATED {time.strftime('%H:%M:%S', time.localtime(self.data['at']))}" if self.data else ""

    def content(self, p, w, h):
        y = 58
        for q in self.data["quotes"]:
            if y + self.ROW_H > h - 30:
                break
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(hud_markets.UP if q["live"] else C["dim"]))
            p.drawEllipse(QPointF(30, y + 13), 3, 3)
            text(p, 40, y + 18, q["name"], font(15, 600, 1.4), C["text"])
            text(p, 40, y + 34, q["symbol"], font(10, 400, mono=True), C["muted"])
            hud_markets.spark(p, QRectF(186, y + 6, 92, 28), q["points"], q["prev"])
            text(p, w - 26, y + 18, hud_feeds.price_text(q["price"]), font(15, 400, mono=True), C["text"], "right")
            text(p, w - 26, y + 34, hud_feeds.pct_text(q["pct"]), font(12, 400, mono=True), hud_markets.trend(q["pct"]),
                 "right")
            y += self.ROW_H
        live = sum(1 for q in self.data["quotes"] if q["live"])
        text(p, 26, h - 22, f"YAHOO FINANCE · {live} OF {len(self.data['quotes'])} MARKETS OPEN",
             font(11, 400, mono=True), C["muted"])


class Toast(QWidget):
    """A line Umay says on her own (a problem, the disk scan's findings), beside the orb; click to act."""

    def __init__(self, orb):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.orb, self.msg, self.id, self.until = orb, "", None, 0.0
        self.setFixedWidth(380)
        timer = QTimer(self)
        timer.timeout.connect(self.follow)
        timer.start(100)

    def show_text(self, msg, id_, seconds):
        self.msg, self.id, self.until = msg, id_, time.monotonic() + seconds
        lines = max(1, len(msg) // 44 + 1)
        self.setFixedHeight(28 + 22 * lines)
        self.follow()
        self.show()
        self.update()

    def follow(self):
        if not self.isVisible():
            return
        if time.monotonic() > self.until:
            self.hide()
            return
        g = self.orb.geometry()
        x = g.x() + g.width() / 2 - self.width() / 2 if self.orb.dock < 0.5 else g.right() - self.width()
        y = g.bottom() + 8 if self.orb.dock < 0.5 else g.y() - self.height() - 8
        self.move(int(x), int(y))

    def mousePressEvent(self, _):
        send({"event": "say_click", "id": self.id})
        self.hide()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(QRectF(0, 0, self.width(), self.height()), QColor(6, 16, 22, 220))
        p.setPen(QPen(color(C["cyan"], 0.35), 1))
        p.drawRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1))
        p.fillRect(QRectF(0, 0, 3, self.height()), QColor(STATES[self.orb.state][0]))
        p.setPen(QColor(C["text"]))
        p.setFont(font(15, 500))
        p.drawText(QRectF(16, 10, self.width() - 28, self.height() - 16),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop) | int(Qt.TextFlag.TextWordWrap), self.msg)
        p.end()


# --- data, Umay's messages, screens --------------------------------------------------------------------------

class Hub(QObject):
    weather = Signal(object)
    vitals = Signal(object)
    drives = Signal(object)
    calendar = Signal(object)
    credits = Signal(object)
    news = Signal(object)
    markets = Signal(object)
    memory = Signal(object)
    message = Signal(object)
    closed = Signal()


class Shared:
    """What the detail views read: the panels' latest data, the vitals of the last 5 minutes, the language,
    and how to reach Umay (send) and the log."""

    def __init__(self, lang):
        self.data, self.history, self.lang = {}, deque(maxlen=HISTORY), lang
        self.send, self.log = send, log
        self.wake = {}  # name -> threading.Event: set() reads that source now (_every)

    def add_vitals(self, v):
        gpu = v.get("gpu") or {}
        self.history.append({"cpu": v["cpu"], "gpu": gpu.get("util"), "temp": gpu.get("temp"), "ram": v["ram_used"],
                             "down": v.get("down"), "up": v.get("up")})


def _every(seconds, read, signal, retry=None):
    """Reads `read` every `seconds` (`retry` after a failure) off the UI thread. Returns an Event whose set()
    reads it again now."""
    wake = threading.Event()

    def run():
        while True:
            try:
                signal.emit(read())
                wait = seconds
            except Exception as e:
                log(f"{getattr(read, '__name__', 'read')} failed: {type(e).__name__}: {e}")
                wait = retry or seconds
            wake.wait(wait)
            wake.clear()
    threading.Thread(target=run, daemon=True).start()
    return wake


def _weather_reader(store):
    def read_weather():
        setting = os.environ.get("UMAY_WEATHER_LOCATION", "").strip()
        place = store.data.get("place")
        if not place or place.get("setting") != setting:
            place = {**hud_data.find_place(setting), "setting": setting}
            store.data["place"] = place
            store.save()
        return hud_data.read_weather(place)
    return read_weather


def _stdin(hub):
    for line in sys.stdin:
        line = line.strip()
        if line:
            try:
                hub.message.emit(json.loads(line))
            except ValueError:
                pass
    hub.closed.emit()


def main(umay_pid=0):
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    try:
        hud_cortex.prepare()
    except Exception as e:  # the wallpaper is extra: the HUD runs without it
        log(f"cortex wallpaper: {type(e).__name__}: {e}")
    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    load_fonts()
    store = Store()
    orb = Orb(store, umay_pid)
    toast = Toast(orb)
    panels = {cls.NAME: cls(store) for cls in (WeatherPanel, VitalsPanel, StoragePanel, CalendarPanel, CreditsPanel,
                                               NewsPanel, MarketsPanel, MemoryPanel)}
    panels["vitals"].cpu = hud_data.cpu_name()
    shown = {"on": True}
    cortex = {"it": None}
    shared = Shared(os.environ.get("UMAY_LANGUAGE", "").strip().lower() or "en")
    shared.data["cpu_name"] = panels["vitals"].cpu
    panels["credits"].lang = shared.lang
    views = {"weather": hud_views.WeatherView, "vitals": hud_views.VitalsView, "storage": hud_disk.StorageView,
             "calendar": hud_views.CalendarView, "credits": hud_credits.CreditsView, "news": hud_news.NewsView,
             "markets": hud_markets.MarketsView, "audio": hud_audio.AudioView}
    detail = {"it": None}

    def open_detail(name, toggle=False):
        """A panel's detail view, grown from where the panel is (one at a time). "audio" (Ctrl+F3) has no panel:
        it grows from the microphone's spot above the panels button, also while the HUD is hidden. toggle: an
        open one closes instead."""
        panel = panels.get(name) if name != "audio" else mic
        if panel is None or name not in views or (not shown["on"] and name != "audio"):
            return
        current = detail["it"]
        if current is not None and not current.closing:
            if current.name() == name:
                if toggle:
                    current.close_detail()
                else:
                    current.raise_()
                    current.activateWindow()
                return
            current.close_detail()
        try:
            screen = panel.screen() if name != "audio" else QGuiApplication.primaryScreen()
            d = hud_detail.Detail(views[name](shared), panel.geometry(), screen, closed)
        except Exception:
            log("detail view failed:\n" + traceback.format_exc())
            return
        detail["it"] = d
        d.open()

    def closed(d):
        if detail["it"] is d:
            detail["it"] = None

    def notify(what):
        d = detail["it"]
        if d is not None and not d.closing:
            d.view.changed(what)

    def panel_clicked(pnl):
        if pnl.NAME == "memory":  # no detail view: the memory editor (its own process)
            try:
                memory_editor.open_editor()
            except OSError as e:
                log(f"memory editor: {e}")
            return
        open_detail(pnl.NAME)

    for panel in panels.values():
        panel.clicked.connect(panel_clicked)

    button = PanelsButton(store, panels.values())
    mic = MutedMic()
    mic.on_click = lambda: open_detail("audio", toggle=True)

    def show_all(on):
        shown["on"] = on
        orb.wanted = on
        hidden = button.hidden()
        orb.setVisible(on and orb.mode != "hidden")
        for name, panel in panels.items():
            panel.setVisible(on and name not in hidden)
        button.setVisible(on)
        mic.setVisible(on and mic.muted)
        if cortex["it"] is not None:
            cortex["it"].set_shown(on)
        if not on:
            toast.hide()
            if detail["it"] is not None:
                detail["it"].close_detail()

    def panels_toggled(hidden):
        for name, panel in panels.items():
            panel.setVisible(shown["on"] and name not in hidden)
        d = detail["it"]
        if d is not None and not d.closing and d.name() in hidden:  # its panel went: its detail view too
            d.close_detail()
    button.toggled.connect(panels_toggled)

    def relocate_now():
        orb.relocate()
        button.relocate()
        mic.relocate()
        for panel in panels.values():
            panel.relocate()

    # Later, not at once: screenRemoved comes while the monitor is still in app.screens(), and a
    # monitor (re)connecting sends several of these in a row.
    settle = QTimer()
    settle.setSingleShot(True)
    settle.timeout.connect(relocate_now)

    def relocate_all(*_):
        settle.start(500)

    def on_screen(screen):
        screen.availableGeometryChanged.connect(relocate_all)
        relocate_all()

    for s in app.screens():
        s.availableGeometryChanged.connect(relocate_all)
    app.screenAdded.connect(on_screen)
    app.screenRemoved.connect(relocate_all)
    app.primaryScreenChanged.connect(relocate_all)

    hub = Hub()

    def feed(name, panel=None):
        def got(data):
            shared.data[name] = data
            if name == "vitals":
                shared.add_vitals(data)
            if panel is not None:
                panel.set_data(data)
            notify(name)
        return got
    hub.weather.connect(feed("weather", panels["weather"]))
    hub.vitals.connect(feed("vitals", panels["vitals"]))
    hub.drives.connect(feed("drives", panels["storage"]))
    hub.calendar.connect(panels["calendar"].set_data)
    hub.credits.connect(panels["credits"].set_data)
    hub.news.connect(feed("news", panels["news"]))
    hub.markets.connect(feed("markets", panels["markets"]))
    hub.memory.connect(panels["memory"].set_data)

    def on_message(m):
        if "state" in m and m["state"] in STATES:
            orb.state = m["state"]
            if orb.state != "speaking":
                orb.level = None
        if "level" in m:
            orb.level = float(m["level"])
        if "say" in m:
            toast.show_text(str(m["say"]), m.get("id"), float(m.get("seconds", 10)))
        if "visible" in m:
            show_all(bool(m["visible"]))
        if "muted" in m:
            mic.muted = bool(m["muted"])
            mic.setVisible(shown["on"] and mic.muted)
        if "open" in m:
            open_detail(str(m["open"]), bool(m.get("toggle")))
        if "audio" in m:
            shared.data["audio"] = m["audio"]
            mic.muted = bool((m["audio"] or {}).get("muted"))
            mic.setVisible(shown["on"] and mic.muted)
            notify("audio")
        if "refresh" in m and shared.wake.get(str(m["refresh"])) is not None:  # who is followed changed
            shared.wake[str(m["refresh"])].set()
        if "balances" in m:
            shared.data["balances"] = m["balances"]
            panels["credits"].balances = m["balances"]
            panels["credits"].update()
            notify("balances")
        if "disk" in m:
            shared.data["disk_state"] = m["disk"]
            notify("disk")
        if "disk_result" in m and detail["it"] is not None and detail["it"].name() == "storage":
            detail["it"].view.message(m)

    hub.message.connect(on_message)
    hub.closed.connect(app.quit)
    orb.clicked.connect(lambda: send({"event": "click"}))
    last_orb = {"rect": None}

    def report_orb():
        # Qt's logical px: what Umay's DPI-unaware Tk sees on the primary monitor too.
        g = orb.geometry()
        rect = [g.x(), g.y(), g.width(), g.height()]
        if rect != last_orb["rect"] and orb.isVisible():
            last_orb["rect"] = rect
            send({"orb": rect})
    reporter = QTimer()
    reporter.timeout.connect(report_orb)
    reporter.start(500)

    _every(600, _weather_reader(store), hub.weather, retry=120)
    _every(2, hud_data.read_vitals, hub.vitals)
    _every(300, hud_data.read_drives, hub.drives, retry=60)
    _every(30, hud_data.read_calendar, hub.calendar)
    _every(5, cortex_graph.summary, hub.memory)
    _every(60, hud_data.read_credits, hub.credits, retry=60)
    shared.wake["news"] = _every(NEWS_SECONDS, hud_feeds.read_news, hub.news, retry=120)
    shared.wake["markets"] = _every(MARKETS_SECONDS, hud_feeds.read_markets, hub.markets, retry=30)
    balances = QTimer()
    balances.timeout.connect(lambda: send({"event": "balances"}))
    balances.start(BALANCES_SECONDS * 1000)
    threading.Thread(target=_stdin, args=(hub,), daemon=True).start()

    show_all(True)
    try:  # Umay's memory as the primary monitor's wallpaper, behind the icons and these panels (hud_cortex)
        cortex["it"] = hud_cortex.start(log=log, own_pids={os.getpid(), umay_pid})
        orb.wallpaper = cortex["it"]
    except Exception as e:
        log(f"cortex wallpaper: {type(e).__name__}: {e}")
    send({"event": "ready"})
    send({"event": "balances"})
    log("started")
    return app.exec()


if __name__ == "__main__":
    try:
        sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 0))
    except Exception:
        log("crashed:\n" + traceback.format_exc())
        raise
