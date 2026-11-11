"""The HUD's drawing helpers and colors, shared by its panels (hud_app.py) and their detail views
(hud_detail.py, hud_disk.py, hud_credits.py): fonts, text, rings, bars, gauges, the weather icons.
"""
import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QLinearGradient, QPainterPath, QPen

import paths

C = {"cyan": "#38D6FF", "cyan2": "#7FDFFF", "text": "#E6F7FF", "muted": "#6E8A96", "dim": "#3E5560",
     "soft": "#B8D4DE", "amber": "#FFB23E", "mint": "#3DF5A8", "hot": "#FF4FA3"}


def color(hex_, alpha=1.0):
    c = QColor(hex_)
    c.setAlphaF(max(0.0, min(1.0, alpha)))
    return c


def load_fonts():
    for f in sorted((paths.resource_dir() / "assets" / "fonts").glob("*.ttf")):
        QFontDatabase.addApplicationFont(str(f))


def font(px, weight=600, spacing=0.0, mono=False):
    f = QFont("JetBrains Mono" if mono else "Rajdhani")
    f.setPixelSize(max(1, int(round(px))))
    f.setWeight(QFont.Weight(weight))
    if spacing:
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
    f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    return f


def text(p, x, y, s, f, col, align="left", width=None):
    """Draws `s` with its baseline at y; align left / right (x = right edge) / center (x = middle)."""
    p.setFont(f)
    p.setPen(QColor(col) if isinstance(col, str) else col)
    w = p.fontMetrics().horizontalAdvance(s)
    if align == "right":
        x -= w
    elif align == "center":
        x -= w / 2
    p.drawText(QPointF(x, y), s)
    return w


def ring(p, cx, cy, r, col, width, start=90.0, span=-360.0, cap=Qt.PenCapStyle.FlatCap, dash=None):
    pen = QPen(col, width)
    pen.setCapStyle(cap)
    if dash:
        pen.setDashPattern(dash)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    rect = QRectF(cx - r, cy - r, 2 * r, 2 * r)
    if abs(span) >= 360:
        p.drawEllipse(rect)
    else:
        p.drawArc(rect, int(start * 16), int(span * 16))


def bar(p, x, y, w, frac, col=C["cyan"], h=4):
    p.fillRect(QRectF(x, y, w, h), color(C["cyan"], 0.12))
    p.fillRect(QRectF(x, y, w * max(0.0, min(1.0, frac)), h), QColor(col))


def split_bar(p, x, y, w, total, parts, h=4):
    """A bar of `total` with parts side by side [(amount, color, alpha, legend)], each legend under it with its
    color's square (the vitals panel: Umay's share of the RAM and the VRAM)."""
    p.fillRect(QRectF(x, y, w, h), color(C["cyan"], 0.12))
    at = x
    for amount, col, alpha, _ in parts:
        seg = w * max(0.0, amount) / total if total else 0
        p.fillRect(QRectF(at, y, min(seg, x + w - at), h), color(col, alpha))
        at += seg + (2 if seg else 0)
    f = font(11, 400, mono=True)
    at = x
    for _, col, alpha, legend in parts:
        text(p, at, y + 26, "■", f, color(col, min(1.0, alpha + 0.15)))
        at += 14 + text(p, at + 14, y + 26, legend, f, C["muted"]) + 18


def rule(p, x, y, w):
    g = QLinearGradient(x, y, x + w, y)
    g.setColorAt(0, color(C["cyan"], 0.35))
    g.setColorAt(1, color(C["cyan"], 0))
    p.fillRect(QRectF(x, y, w, 1), g)


def lerp(a, b, t):
    return a + (b - a) * t


def mix(c1, c2, t):
    return QColor.fromRgbF(lerp(c1.redF(), c2.redF(), t), lerp(c1.greenF(), c2.greenF(), t),
                           lerp(c1.blueF(), c2.blueF(), t))


def gauge(p, cx, cy, frac, col, value, label, sub):
    ring(p, cx, cy, 42, color(C["cyan"], 0.12), 5)
    ring(p, cx, cy, 42, QColor(col), 5, 90, -360 * max(0.0, min(1.0, frac)))
    ring(p, cx, cy, 34, color(C["cyan"], 0.25), 1, dash=[1, 4])
    text(p, cx, cy + 8, value, font(22, 400, mono=True), C["text"], "center")
    text(p, cx, cy + 72, label, font(12, 600, 2.4), C["muted"], "center")
    text(p, cx, cy + 92, sub, font(11, 400, mono=True), C["muted"], "center")


def weather_icon(p, x, y, kind):
    """The weather panel's icon (74 x 74 px) at (x, y)."""
    pen = QPen(QColor(C["cyan2"]), 1.6)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    if kind in ("sun", "cloud_sun"):
        sx, sy, r = (x + 36, y + 36, 13) if kind == "sun" else (x + 46, y + 24, 9)
        p.drawEllipse(QPointF(sx, sy), r, r)
        for i in range(8):
            a = math.radians(i * 45)
            p.drawLine(QPointF(sx + math.cos(a) * (r + 5), sy + math.sin(a) * (r + 5)),
                       QPointF(sx + math.cos(a) * (r + 10), sy + math.sin(a) * (r + 10)))
    if kind != "sun":
        cloud = QPainterPath()
        cloud.moveTo(x + 22, y + 50)
        cloud.lineTo(x + 54, y + 50)
        cloud.arcTo(QRectF(x + 43, y + 28, 22, 22), -90, 180)
        cloud.arcTo(QRectF(x + 22, y + 16, 30, 30), 20, 140)
        cloud.arcTo(QRectF(x + 10, y + 26, 24, 24), 90, 180)
        p.drawPath(cloud)
    p.setPen(QPen(QColor(C["cyan"]), 1.6))
    if kind in ("rain", "storm"):
        for dx in (27, 37, 47):
            p.drawLine(QPointF(x + dx, y + 57), QPointF(x + dx - 2, y + 62))
    if kind == "snow":
        for dx in (27, 37, 47):
            p.drawEllipse(QPointF(x + dx, y + 59), 1.6, 1.6)
    if kind == "fog":
        for dy in (56, 61):
            p.drawLine(QPointF(x + 18, y + dy), QPointF(x + 56, y + dy))
    if kind == "storm":
        p.setPen(QPen(QColor(C["amber"]), 1.6))
        p.drawPolyline([QPointF(x + 40, y + 52), QPointF(x + 35, y + 60), QPointF(x + 41, y + 60),
                        QPointF(x + 36, y + 68)])
