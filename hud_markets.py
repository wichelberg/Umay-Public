"""07 // MARKETS's detail view (hud_detail.py), painted: every symbol of UMAY_MARKET_SYMBOLS on the left (a
click picks one), the picked one on the right: its price and change, a chart over 1D / 5D / 1M / 6M / 1Y / 5Y
(the cursor over it reads a point), the day's and the year's range, volume. The chart is read again every
CHART_SECONDS while the view is open; the list follows the panel's quotes.

spark() and the up / down colors are the panel's too (hud_app.MarketsPanel).
"""
import threading
import time

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainterPath, QPen

import hud_feeds as F
from hud_detail import Painted
from hud_paint import C, color, font, rule, text

CHART_SECONDS = 10
SPARK_LEAST = 0.004  # the small charts' height spans at least 0.4% of the price
UP, DOWN, FLAT = C["mint"], "#FF5C6C", C["muted"]
TIME_FORMATS = {"1D": "%H:%M", "5D": "%a %H:%M", "1M": "%d %b", "6M": "%d %b", "1Y": "%b %Y", "5Y": "%b %Y"}


def trend(value):
    return FLAT if not value else UP if value > 0 else DOWN


def spark(p, r, points, base=None, col=None):
    """A small line of `points` in rect r, with `base` (the last close) dotted across it."""
    if len(points) < 2:
        return
    lo, hi = min(points), max(points)
    if base is not None:
        lo, hi = min(lo, base), max(hi, base)
    least = abs(lo + hi) / 2 * SPARK_LEAST or 1  # a calm day (a currency) isn't blown up into a storm
    if hi - lo < least:
        mid = (hi + lo) / 2
        lo, hi = mid - least / 2, mid + least / 2
    col = col or trend(points[-1] - (base if base is not None else points[0]))

    def at(i, v):
        return QPointF(r.left() + r.width() * i / (len(points) - 1), r.bottom() - (v - lo) / (hi - lo) * r.height())
    if base is not None:
        p.setPen(QPen(color(C["muted"], 0.6), 1, Qt.PenStyle.DotLine))
        y = at(0, base).y()
        p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
    path = QPainterPath(at(0, points[0]))
    for i, v in enumerate(points[1:], 1):
        path.lineTo(at(i, v))
    area = QPainterPath(path)
    area.lineTo(r.right(), r.bottom())
    area.lineTo(r.left(), r.bottom())
    area.closeSubpath()
    p.fillPath(area, color(col, 0.10))
    pen = QPen(QColor(col), 1.4)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.strokePath(path, pen)


class MarketsView(Painted):
    NAME, INDEX, TITLE = "markets", "07", "MARKETS"
    got = Signal(object)

    def __init__(self, shared):
        super().__init__(shared)
        self.setMouseTracking(True)
        self.selected, self.span = None, "1D"
        self.chart, self.error = None, None
        self.rows, self.buttons, self.plot, self.mouse = {}, {}, None, None
        self.active, self.wake = False, threading.Event()
        self.got.connect(self._got)

    def sub(self):
        return "YAHOO FINANCE"

    def quotes(self):
        return (self.shared.data.get("markets") or {}).get("quotes") or []

    def opened(self):
        if self.selected is None and self.quotes():
            self.selected = self.quotes()[0]["symbol"]
        self.active = True

        def run():
            while self.active:
                key = (self.selected, self.span)
                if key[0] is not None:
                    try:
                        self.got.emit((key, F.read_chart(*key), None))
                    except Exception as e:
                        self.shared.log(f"markets chart {key} failed: {type(e).__name__}: {e}")
                        self.got.emit((key, None, str(e)))
                self.wake.wait(CHART_SECONDS)
                self.wake.clear()
        threading.Thread(target=run, daemon=True).start()

    def closing(self):
        self.active = False
        self.wake.set()

    def _got(self, answer):
        key, chart, error = answer
        if key == (self.selected, self.span):
            self.chart, self.error = chart, error
            self.update()

    def changed(self, what):
        if what == "markets":
            if self.selected is None and self.quotes():
                self.selected = self.quotes()[0]["symbol"]
                self.wake.set()
            self.update()

    def pick(self, symbol=None, span=None):
        self.selected = symbol or self.selected
        self.span = span or self.span
        self.chart, self.error = None, None
        self.wake.set()
        self.update()

    def mousePressEvent(self, ev):
        pos = ev.position()
        for symbol, r in self.rows.items():
            if r.contains(pos) and symbol != self.selected:
                return self.pick(symbol=symbol)
        for span, r in self.buttons.items():
            if r.contains(pos) and span != self.span:
                return self.pick(span=span)

    def mouseMoveEvent(self, ev):
        pos = ev.position()
        self.mouse = pos if self.plot is not None and self.plot.contains(pos) else None
        hand = any(r.contains(pos) for r in (*self.rows.values(), *self.buttons.values()))
        self.setCursor(Qt.CursorShape.PointingHandCursor if hand else Qt.CursorShape.ArrowCursor)
        self.update()

    def leaveEvent(self, _):
        self.mouse = None
        self.update()

    def draw(self, p, w, h):
        quotes = self.quotes()
        if not quotes:
            text(p, 0, 30, "ACQUIRING…", font(15, 600, 2.6), C["muted"])
            return
        left = min(400, w * 0.3)
        self._list(p, quotes, QRectF(0, 0, left, h))
        self._detail(p, QRectF(left + 34, 0, w - left - 34, h))

    def _list(self, p, quotes, r):
        updated = time.strftime("%H:%M:%S", time.localtime((self.shared.data.get("markets") or {}).get("at", 0)))
        text(p, r.left(), 14, "WATCHLIST", font(13, 700, 3.0), C["cyan2"])
        text(p, r.right(), 14, f"UPDATED {updated}", font(11, 400, mono=True), C["muted"], "right")
        rule(p, r.left(), 22, r.width())
        self.rows = {}
        row_h = max(46, min(64, (r.height() - 34) / max(1, len(quotes))))
        for i, q in enumerate(quotes):
            y = 34 + i * row_h
            if y + row_h > r.bottom() + 1:
                break
            box = QRectF(r.left(), y, r.width(), row_h - 4)
            self.rows[q["symbol"]] = box
            if q["symbol"] == self.selected:
                p.fillRect(box, color(C["cyan"], 0.12))
                p.fillRect(QRectF(box.left(), box.top(), 3, box.height()), QColor(C["cyan"]))
            mid = box.top() + box.height() / 2
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(UP if q["live"] else C["dim"]))
            p.drawEllipse(QPointF(box.left() + 14, mid - 6), 3, 3)
            text(p, box.left() + 26, mid - 1, q["name"], font(17, 600, 1.4), C["text"])
            text(p, box.left() + 26, mid + 16, q["symbol"], font(11, 400, mono=True), C["muted"])
            spark(p, QRectF(box.left() + r.width() * 0.44, mid - 14, r.width() * 0.22, 28), q["points"], q["prev"])
            text(p, box.right() - 10, mid - 1, F.price_text(q["price"]), font(16, 400, mono=True), C["text"], "right")
            text(p, box.right() - 10, mid + 16, F.pct_text(q["pct"]), font(12, 400, mono=True), trend(q["pct"]), "right")

    def _detail(self, p, r):
        q = next((q for q in self.quotes() if q["symbol"] == self.selected), None)
        c = self.chart
        x = r.left()
        text(p, x, 30, (c["name"] if c else q["name"] if q else self.selected or "").upper(), font(30, 600, 3), C["text"])
        meta = " · ".join(v for v in (self.selected, c and c["exchange"], c and c["currency"]) if v)
        text(p, x, 54, meta, font(12, 400, mono=True), C["muted"])
        # range buttons
        self.buttons = {}
        bx = r.right()
        for span in reversed(list(F.RANGES)):
            bw = 54
            bx -= bw
            b = QRectF(bx, 4, bw - 8, 32)
            self.buttons[span] = b
            on = span == self.span
            p.setPen(QPen(color(C["cyan"], 0.9 if on else 0.35), 1))
            p.setBrush(color(C["cyan"], 0.28 if on else 0.06))
            p.drawRect(b)
            text(p, b.center().x(), b.top() + 22, span, font(14, 700, 1.6), C["text"] if on else C["cyan2"], "center")
        price = c["price"] if c else q["price"] if q else None
        change, pct = (c["change"], c["pct"]) if c else ((q["change"], q["pct"]) if q else (None, None))
        pw = text(p, x, 118, F.price_text(price), font(56, 500), C["text"])
        sign = "" if change is None else "+" if change >= 0 else ""
        tcol = trend(change)
        text(p, x + pw + 20, 92, f"{sign}{F.price_text(change, price)}" if change is not None else "—",
             font(20, 400, mono=True), tcol)
        since = "TODAY" if self.span == "1D" else f"IN {self.span}"
        text(p, x + pw + 20, 116, f"{F.pct_text(pct)}  {since}", font(14, 400, mono=True), tcol)
        live = q and q["live"]
        when = c and c.get("market_at")
        state = "● LIVE" if live else "CLOSED"
        if when:
            state += f" · LAST TRADE {time.strftime('%d %b %H:%M', time.localtime(when)).upper()}"
        text(p, r.right(), 116, state, font(12, 600, 2), UP if live else C["muted"], "right")

        plot = QRectF(x, 146, r.width() - 60, r.height() - 146 - 96)
        self.plot = plot
        if c is None:
            self.plot = None
            msg = f"NO CHART: {self.error[:80]}" if self.error else "LOADING CHART…"
            p.fillRect(plot, color(C["cyan"], 0.04))
            text(p, plot.left() + 12, plot.top() + 24, msg.upper(), font(13, 600, 2), C["muted"])
        else:
            self._chart(p, c, plot)
        self._stats(p, c, QRectF(x, r.bottom() - 70, r.width(), 70))

    def _chart(self, p, c, r):
        closes, times = c["closes"], c["times"]
        p.fillRect(r, color(C["cyan"], 0.04))
        if len(closes) < 2:
            text(p, r.left() + 12, r.top() + 24, "NO TRADES IN THIS RANGE", font(13, 600, 2), C["muted"])
            self.plot = None
            return
        base = c["base"] if self.span == "1D" else None
        lo, hi = min(closes), max(closes)
        if base is not None:
            lo, hi = min(lo, base), max(hi, base)
        pad = (hi - lo) * 0.06 or 1
        lo, hi = lo - pad, hi + pad
        n = len(closes) - 1

        def at(i, v):
            return QPointF(r.left() + r.width() * i / n, r.bottom() - (v - lo) / (hi - lo) * r.height())
        p.setPen(QPen(color(C["cyan"], 0.10), 1, Qt.PenStyle.DotLine))
        for k in range(1, 4):
            y = r.top() + r.height() * k / 4
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
        f = font(11, 400, mono=True)
        for k in range(5):
            v = hi - (hi - lo) * k / 4
            text(p, r.right() + 8, r.top() + r.height() * k / 4 + 4, F.price_text(v), f, C["muted"])
        fmt = TIME_FORMATS[self.span]
        for k in range(5):
            i = round(n * k / 4)
            align = "left" if k == 0 else "right" if k == 4 else "center"
            text(p, at(i, lo).x(), r.bottom() + 18, time.strftime(fmt, time.localtime(times[i])).upper(), f,
                 C["muted"], align)
        col = trend(closes[-1] - (base if base is not None else closes[0]))
        if base is not None:
            y = at(0, base).y()
            p.setPen(QPen(color(C["soft"], 0.5), 1, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            text(p, r.left() + 6, y - 6, f"PREV CLOSE {F.price_text(base)}", font(10, 400, mono=True), C["soft"])
        path = QPainterPath(at(0, closes[0]))
        for i, v in enumerate(closes[1:], 1):
            path.lineTo(at(i, v))
        area = QPainterPath(path)
        area.lineTo(r.right(), r.bottom())
        area.lineTo(r.left(), r.bottom())
        area.closeSubpath()
        p.fillPath(area, color(col, 0.12))
        pen = QPen(QColor(col), 1.8)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.strokePath(path, pen)
        last = at(n, closes[-1])
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(col))
        p.drawEllipse(last, 3.5, 3.5)
        if self.mouse is not None:
            i = max(0, min(n, round((self.mouse.x() - r.left()) / r.width() * n)))
            pt = at(i, closes[i])
            p.setPen(QPen(color(C["text"], 0.5), 1, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(pt.x(), r.top()), QPointF(pt.x(), r.bottom()))
            p.setPen(QPen(QColor(C["text"]), 1.5))
            p.setBrush(QColor("#000000"))
            p.drawEllipse(pt, 4, 4)
            stamp = time.strftime("%d %b %Y %H:%M" if self.span in ("1D", "5D", "1M") else "%d %b %Y",
                                  time.localtime(times[i])).upper()
            label = f"{F.price_text(closes[i])}   {stamp}"
            fl = font(13, 400, mono=True)
            p.setFont(fl)
            lw = p.fontMetrics().horizontalAdvance(label) + 20
            bx = min(max(r.left(), pt.x() - lw / 2), r.right() - lw)
            box = QRectF(bx, r.top() + 6, lw, 26)
            p.fillRect(box, QColor(6, 16, 22, 235))
            p.setPen(QPen(color(C["cyan"], 0.5), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(box)
            text(p, box.center().x(), box.top() + 18, label, fl, C["text"], "center")

    def _stats(self, p, c, r):
        rule(p, r.left(), r.top(), r.width())
        c = c or {}
        cells = [("PREV CLOSE", F.price_text(c.get("prev_close"))), ("DAY HIGH", F.price_text(c.get("day_high"))),
                 ("DAY LOW", F.price_text(c.get("day_low"))), ("52W HIGH", F.price_text(c.get("year_high"))),
                 ("52W LOW", F.price_text(c.get("year_low"))), ("VOLUME", F.volume_text(c.get("volume") or None))]
        cw = r.width() / len(cells)
        for i, (lab, val) in enumerate(cells):
            text(p, r.left() + i * cw, r.top() + 30, lab, font(12, 600, 2.4), C["muted"])
            text(p, r.left() + i * cw, r.top() + 56, val, font(19, 400, mono=True), C["text"])
