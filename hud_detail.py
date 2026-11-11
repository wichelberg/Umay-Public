"""A HUD panel's detail view: a click on a panel grows it from where it sits into a big view in the middle of
its monitor (the rest of the monitor dims); Esc, a click outside it or its ✕ shrinks it back. One is open
at a time.

The overlay (Detail) draws the frame, the header and the dimmed screen; what is inside is a View: painted
(hud_views.py: weather, vitals, calendar) or made of widgets in the HUD's look (hud_disk.py: storage and
the disk scan, hud_credits.py: the API credit log). QSS is that look for tables, lists, buttons and menus.
"""
from PySide6.QtCore import QEasingCurve, QPointF, QRectF, Qt, QVariantAnimation
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from hud_paint import C, color, font, lerp, text

MAX_SIZE = (1500, 940)
EDGE = (50, 40)              # px kept free around it on its monitor
PAD = (30, 72, 30, 26)       # left, top (the header), right, bottom inside the frame
OPEN_MS, CLOSE_MS = 280, 200
CLOSE_BOX = (118, 34)

QSS = f"""
QWidget {{ color: {C['text']}; font-family: 'Rajdhani'; font-size: 15px; font-weight: 600; }}
QLabel {{ background: transparent; }}
QLabel[role="muted"] {{ color: {C['muted']}; font-size: 13px; }}
QLabel[role="head"] {{ color: {C['cyan2']}; font-size: 14px; letter-spacing: 3px; }}
QLabel[role="mono"] {{ font-family: 'JetBrains Mono'; font-size: 12px; font-weight: 400; }}
QLabel[role="ok"] {{ color: {C['cyan']}; }}
QLabel[role="bad"] {{ color: {C['hot']}; }}
QTreeWidget {{ background: rgba(4, 12, 17, 150); border: 1px solid rgba(56, 214, 255, 40);
    font-family: 'JetBrains Mono'; font-size: 12px; font-weight: 400; outline: 0;
    selection-background-color: rgba(56, 214, 255, 55); selection-color: {C['text']};
    alternate-background-color: rgba(56, 214, 255, 8); }}
QTreeWidget::item {{ padding: 3px 4px; border: none; }}
QTreeWidget::item:selected {{ background: rgba(56, 214, 255, 55); }}
QHeaderView {{ background: transparent; }}
QHeaderView::section {{ background: rgba(56, 214, 255, 18); color: {C['cyan2']}; border: none;
    border-right: 1px solid rgba(56, 214, 255, 30); padding: 6px 6px; font-family: 'Rajdhani';
    font-size: 13px; font-weight: 700; }}
QPushButton {{ background: rgba(56, 214, 255, 22); border: 1px solid rgba(56, 214, 255, 110);
    color: {C['cyan2']}; padding: 6px 18px; font-size: 14px; font-weight: 700; letter-spacing: 2px; }}
QPushButton:hover {{ background: rgba(56, 214, 255, 50); color: {C['text']}; }}
QPushButton:disabled {{ color: {C['dim']}; border-color: rgba(62, 85, 96, 140); background: transparent; }}
QPushButton[role="danger"] {{ color: {C['hot']}; border-color: rgba(255, 79, 163, 140);
    background: rgba(255, 79, 163, 22); }}
QPushButton[role="danger"]:hover {{ background: rgba(255, 79, 163, 60); color: {C['text']}; }}
QPushButton[role="danger"]:disabled {{ color: {C['dim']}; border-color: rgba(62, 85, 96, 140);
    background: transparent; }}
QComboBox {{ background: rgba(56, 214, 255, 14); border: 1px solid rgba(56, 214, 255, 80); padding: 4px 10px;
    min-width: 120px; color: {C['text']}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{ background: #08151c; border: 1px solid rgba(56, 214, 255, 90);
    selection-background-color: rgba(56, 214, 255, 60); color: {C['text']}; outline: 0; }}
QScrollBar:vertical {{ background: transparent; width: 9px; margin: 0; }}
QScrollBar::handle:vertical {{ background: rgba(56, 214, 255, 80); min-height: 30px; }}
QScrollBar:horizontal {{ background: transparent; height: 9px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: rgba(56, 214, 255, 80); min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QPlainTextEdit {{ background: rgba(4, 12, 17, 150); border: 1px solid rgba(56, 214, 255, 40);
    font-family: 'JetBrains Mono'; font-size: 12px; font-weight: 400; color: {C['text']}; }}
"""


def ease(t):
    return 1 - (1 - t) ** 3


def frame(p, r, alpha=1.0, corner=22):
    """The HUD panel's frame, bigger: dark glass, a thin cyan edge and the corner brackets."""
    g = QLinearGradient(r.topLeft(), r.bottomLeft())
    g.setColorAt(0, QColor(9, 22, 30, int(252 * alpha)))
    g.setColorAt(1, QColor(3, 9, 13, int(250 * alpha)))
    p.fillRect(r, g)
    p.setPen(QPen(color(C["cyan"], 0.22 * alpha), 1))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRect(r.adjusted(0.5, 0.5, -0.5, -0.5))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color(C["cyan"], alpha))
    x0, y0, x1, y1 = r.left(), r.top(), r.right(), r.bottom()
    for x, y, sx, sy in ((x0, y0, 1, 1), (x1, y0, -1, 1), (x0, y1, 1, -1), (x1, y1, -1, -1)):
        p.drawRect(QRectF(x if sx > 0 else x - corner, y if sy > 0 else y - 2, corner, 2))
        p.drawRect(QRectF(x if sx > 0 else x - 2, y if sy > 0 else y - corner, 2, corner))


def chart(p, r, series, lo=None, hi=None, labels=True, unit=""):
    """A line chart in rect r. series: [(values, color, fill, dashed)], values may hold None."""
    p.fillRect(r, color(C["cyan"], 0.04))
    p.setPen(QPen(color(C["cyan"], 0.10), 1, Qt.PenStyle.DotLine))
    for i in range(1, 4):
        y = r.top() + r.height() * i / 4
        p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
    values = [v for vals, *_ in series for v in vals if v is not None]
    if not values:
        text(p, r.left() + 8, r.top() + 18, "NO DATA YET", font(11, 600, 2), C["muted"])
        return
    lo = min(values) if lo is None else lo
    hi = max(values) if hi is None else hi
    if hi <= lo:
        hi = lo + 1
    for vals, col, fill, dashed in series:
        pts = [(i, v) for i, v in enumerate(vals) if v is not None]
        if len(pts) < 2:
            continue
        n = max(len(vals) - 1, 1)

        def at(i, v):
            return QPointF(r.left() + r.width() * i / n, r.bottom() - (v - lo) / (hi - lo) * (r.height() - 4) - 2)
        path = QPainterPath(at(*pts[0]))
        for i, v in pts[1:]:
            path.lineTo(at(i, v))
        if fill:
            area = QPainterPath(path)
            area.lineTo(at(pts[-1][0], lo))
            area.lineTo(at(pts[0][0], lo))
            area.closeSubpath()
            p.fillPath(area, color(col, fill))
        pen = QPen(QColor(col), 1.6)
        if dashed:
            pen.setDashPattern([3, 3])
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.strokePath(path, pen)
    if labels:
        f = font(10, 400, mono=True)
        text(p, r.right() - 4, r.top() + 12, f"{hi:.0f}{unit}", f, C["muted"], "right")
        text(p, r.right() - 4, r.bottom() - 4, f"{lo:.0f}{unit}", f, C["muted"], "right")


class View(QWidget):
    """What a detail view shows. `shared`: the HUD's data (weather, vitals, drives...), send() to Umay, log."""

    NAME, INDEX, TITLE = "", "", ""
    SIZE = MAX_SIZE   # at most; a small view (hud_audio) asks for less

    def __init__(self, shared):
        super().__init__()
        self.shared = shared
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    def sub(self):
        return ""

    def opened(self):
        """Shown, at its full size: start reading what it needs."""

    def closing(self):
        """About to go: stop reading."""

    def changed(self, what):
        """The HUD has new data of a kind ("weather", "vitals", "balances", "disk"...)."""

    def message(self, msg):
        """A message from Umay meant for this view (a delete's result...)."""

    def escape(self):
        """Esc: True if the view handled it (a confirmation closed), else the detail view closes."""
        return False


class Painted(View):
    """A view drawn with QPainter, like the panels."""

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        try:
            self.draw(p, self.width(), self.height())
        except Exception as e:  # a surprising answer from a source: the view says so, the HUD lives on
            self.shared.log(f"{self.TITLE} detail paint failed: {type(e).__name__}: {e}")
            text(p, 0, 30, "NO DATA", font(14, 600, 2.6), C["muted"])
        p.end()

    def draw(self, p, w, h):
        raise NotImplementedError


class Detail(QWidget):
    """The overlay on one monitor: the dimmed screen and the view's frame, grown from the panel's rect."""

    def __init__(self, view, source, screen, on_closed):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
                         | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setStyleSheet(QSS)
        self.view, self.on_closed = view, on_closed
        g = screen.availableGeometry()
        self.setGeometry(g)
        self.source = QRectF(source.translated(-g.topLeft()))
        w = min(view.SIZE[0], MAX_SIZE[0], g.width() - 2 * EDGE[0])
        h = min(view.SIZE[1], MAX_SIZE[1], g.height() - 2 * EDGE[1])
        self.target = QRectF((g.width() - w) / 2, (g.height() - h) / 2, w, h)
        self.t, self.closing = 0.0, False
        view.setParent(self)
        view.hide()
        self.anim = QVariantAnimation(self)
        self.anim.valueChanged.connect(self._step)
        self.anim.finished.connect(lambda: self.after and self.after())
        self.after = None

    def name(self):
        return self.view.NAME

    def open(self):
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus()
        self._run(0.0, 1.0, OPEN_MS, self._opened)

    def _run(self, a, b, ms, done):
        self.anim.stop()
        self.after = done
        self.anim.setStartValue(a)
        self.anim.setEndValue(b)
        self.anim.setDuration(ms)
        self.anim.setEasingCurve(QEasingCurve.Type.Linear)
        self.anim.start()

    def _step(self, value):
        self.t = float(value)
        self.update()

    def rect_now(self):
        e = ease(self.t)
        s, d = self.source, self.target
        return QRectF(lerp(s.x(), d.x(), e), lerp(s.y(), d.y(), e), lerp(s.width(), d.width(), e),
                      lerp(s.height(), d.height(), e))

    def inner(self):
        r = self.target
        return QRectF(r.x() + PAD[0], r.y() + PAD[1], r.width() - PAD[0] - PAD[2], r.height() - PAD[1] - PAD[3])

    def close_box(self):
        r = self.target
        return QRectF(r.right() - PAD[2] - CLOSE_BOX[0], r.top() + 18, CLOSE_BOX[0], CLOSE_BOX[1])

    def _opened(self):
        self.view.setGeometry(self.inner().toRect())
        self.view.show()
        self.view.opened()
        self.activateWindow()
        self.setFocus()

    def close_detail(self):
        if self.closing:
            return
        self.closing = True
        self.view.closing()
        self.view.hide()
        self._run(self.t, 0.0, CLOSE_MS, self._closed)

    def _closed(self):
        self.hide()
        self.on_closed(self)
        self.deleteLater()

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key.Key_Escape and not self.view.escape():
            self.close_detail()
        else:
            super().keyPressEvent(ev)

    def mousePressEvent(self, ev):
        pos = ev.position()
        if self.close_box().contains(pos) or not self.rect_now().contains(pos):
            self.close_detail()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        e = ease(self.t)
        p.fillRect(QRectF(0, 0, self.width(), self.height()), QColor(0, 4, 8, int(150 * e)))
        r = self.rect_now()
        frame(p, r, 0.35 + 0.65 * e)
        if self.t >= 0.999 and not self.closing:
            v = self.view
            text(p, r.left() + PAD[0], r.top() + 44, f"{v.INDEX} // {v.TITLE}", font(17, 600, 3.6), C["cyan2"])
            b = self.close_box()
            text(p, b.left() - 18, r.top() + 41, v.sub(), font(13, 500, 2.2), C["muted"], "right")
            p.setPen(QPen(color(C["cyan"], 0.35), 1))
            p.setBrush(color(C["cyan"], 0.06))
            p.drawRect(b)
            text(p, b.center().x(), b.top() + 23, "ESC  ✕", font(13, 700, 2.4), C["cyan2"], "center")
            g = QLinearGradient(r.left() + PAD[0], 0, r.right() - PAD[2], 0)
            g.setColorAt(0, color(C["cyan"], 0.4))
            g.setColorAt(1, color(C["cyan"], 0))
            p.fillRect(QRectF(r.left() + PAD[0], r.top() + 58, r.width() - PAD[0] - PAD[2], 1), g)
        p.end()
