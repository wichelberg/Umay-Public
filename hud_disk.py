"""03 // STORAGE's detail view (hud_detail.py): the drives and the disk scan, which was the Ctrl+F1 panel's Disk
tab. Every fixed drive with its free space; the scanned items (type, size, last use and where that date comes
from, what it is, re-downloadable or not, dependencies), sorted by a click on any column and filtered by type
and size; the selected item's details; a timeline of the used space and of the selected item; what grew most
this week. There is no compress button, on purpose.

The scan is read from its files (disk_memory.Store); scanning and deleting happen in Umay's process
(disk_watch): Scan now and Delete send {"event": "disk_scan" / "disk_delete"}, and Umay answers with
{"disk": {"running", "progress"}} and {"disk_result": {"id", "text", "ok"}}. Delete always asks first, in the
view itself, listing what goes with the item and what breaks.
"""
import os
import time

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPen
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QLabel, QPlainTextEdit,
                               QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

import disk_actions
import disk_memory
import disk_tools
import i18n
import panel_text as T
from disk_watch import size_text
from hud_detail import View, frame
from hud_paint import C, color, font, text

RED = "#ff4d6d"
WIDTHS = {"kind": 90, "name": 250, "size": 90, "used": 230, "what": 260, "redl": 150, "deps": 200}


def label(text_, role=None, wrap=False):
    w = QLabel(text_)
    if role:
        w.setProperty("role", role)
    w.setWordWrap(wrap)
    w.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return w


def button(text_, role=None):
    b = QPushButton(text_)
    if role:
        b.setProperty("role", role)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


class Drives(QWidget):
    """Every fixed drive: free space and a bar; what Umay's own models take."""

    def __init__(self, shared):
        super().__init__()
        self.shared = shared
        self.setFixedHeight(86)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        data = self.shared.data.get("drives") or {}
        drives = data.get("drives", [])
        w = self.width()
        n = max(1, len(drives))
        cw = min(420, (w - 20 * (n - 1)) / n)
        for i, d in enumerate(drives):
            x = i * (cw + 20)
            frac = d["used"] / d["total"] if d["total"] else 0
            text(p, x, 30, d["name"], font(26, 600, 2), C["text"])
            text(p, x + 56, 22, f"{d['free']:.0f} GB FREE", font(15, 600, 2), C["cyan2"])
            text(p, x + 56, 40, f"{d['used']:.0f} OF {d['total']:.0f} GB USED · {frac * 100:.0f}%",
                 font(11, 400, mono=True), C["muted"])
            p.fillRect(QRectF(x, 54, cw, 6), color(C["cyan"], 0.12))
            p.fillRect(QRectF(x, 54, cw * frac, 6), QColor(C["hot"] if frac > 0.85 else C["cyan"]))
        umay = data.get("umay", [])
        if umay:
            line = "  ·  ".join(f"{u['name']} {u['size']:.1f} GB" for u in umay)
            text(p, 0, 80, line, font(11, 400, mono=True), C["muted"])
        p.end()


class Timeline(QWidget):
    """The used space scan by scan and, under it, the selected item's size."""

    def __init__(self):
        super().__init__()
        self.totals, self.mine, self.name = [], [], ""
        self.setMinimumHeight(190)

    def set(self, totals, mine, name):
        self.totals, self.mine, self.name = totals, mine, name
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        half = h / 2
        self._series(p, self.totals, QRectF(0, 0, w, half - 6), C["cyan"], "USED SPACE")
        if self.name:
            self._series(p, self.mine, QRectF(0, half + 6, w, half - 6), C["hot"], self.name.upper()[:40])
        else:
            p.fillRect(QRectF(0, half + 6, w, half - 6), color(C["cyan"], 0.04))
            text(p, 8, half + 24, "SELECT AN ITEM FOR ITS SIZE OVER TIME", font(11, 600, 1.6), C["muted"])
        p.end()

    def _series(self, p, points, r, col, title):
        p.fillRect(r, color(C["cyan"], 0.04))
        text(p, r.left() + 8, r.top() + 16, title, font(11, 600, 1.6), C["muted"])
        if not points:
            text(p, r.left() + 8, r.top() + 36, "ONE SCAN SO FAR" if title == "USED SPACE" else "—",
                 font(11, 400, mono=True), C["dim"])
            return
        text(p, r.right() - 8, r.top() + 16, size_text(points[-1][1]), font(12, 400, mono=True), col, "right")
        times = [t for t, _ in points]
        values = [v for _, v in points]
        lo, hi, t0, t1 = min(values), max(values), min(times), max(times)
        pts = [QPointF(r.left() + 12 + (r.width() - 24) * ((t - t0) / (t1 - t0) if t1 > t0 else 1),
                       r.bottom() - 10 - (r.height() - 36) * ((v - lo) / (hi - lo) if hi > lo else 0.5))
               for t, v in points]
        p.setPen(QPen(QColor(col), 2))
        for a, b in zip(pts, pts[1:]):
            p.drawLine(a, b)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(col))
        for q in pts:
            p.drawEllipse(q, 3, 3)
        f = font(10, 400, mono=True)
        text(p, r.left() + 12, r.bottom() - 2 + 0, time.strftime("%m-%d", time.localtime(t0)), f, C["dim"])
        if t1 > t0:
            text(p, r.right() - 12, r.bottom() - 2, time.strftime("%m-%d", time.localtime(t1)), f, C["dim"], "right")


class Confirm(QWidget):
    """"Delete X?" over the view: how it goes, what goes with it, what breaks. answer(bool) once."""

    def __init__(self, parent, title, body, yes, no, answer):
        super().__init__(parent)
        self.answer, self.done = answer, False
        self.setGeometry(parent.rect())
        box = QWidget(self)
        w, h = min(640, parent.width() - 40), min(460, parent.height() - 40)
        box.setGeometry((parent.width() - w) // 2, (parent.height() - h) // 2, w, h)
        self.box = box
        lay = QVBoxLayout(box)
        lay.setContentsMargins(26, 22, 26, 20)
        head = label(title, wrap=True)
        head.setStyleSheet(f"color: {C['hot']}; font-size: 19px; font-weight: 700;")
        lay.addWidget(head)
        self.text = body
        view = QPlainTextEdit(body)
        view.setReadOnly(True)
        lay.addWidget(view, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel, delete = button(no), button(yes, "danger")
        cancel.clicked.connect(lambda: self.pick(False))
        delete.clicked.connect(lambda: self.pick(True))
        row.addWidget(cancel)
        row.addWidget(delete)
        lay.addLayout(row)
        self.show()
        cancel.setFocus()

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 4, 8, 170))
        frame(p, QRectF(self.box.geometry()), 1.0, 16)
        p.end()

    def mousePressEvent(self, ev):
        ev.accept()  # clicks around the box don't reach the list under it

    def pick(self, value):
        if self.done:
            return
        self.done = True
        self.hide()
        self.deleteLater()
        self.answer(value)


class Row(QTreeWidgetItem):
    def __init__(self, values, item):
        super().__init__(list(values))
        self.it = item


class StorageView(View):
    NAME, INDEX, TITLE = "storage", "03", "STORAGE"

    def __init__(self, shared):
        super().__init__(shared)
        self.lang = shared.lang
        self.store, self.scan = None, None
        self.sort_column, self.sort_reverse = "size", True
        self.selected, self.confirm = None, None
        self.deleting = None
        t = self.t

        top = QHBoxLayout()
        self.drives = Drives(shared)
        top.addWidget(self.drives, 1)
        side = QVBoxLayout()
        self.scan_button = button("SCAN NOW")
        self.scan_button.clicked.connect(self._scan)
        side.addWidget(self.scan_button)
        side.addStretch(1)
        top.addLayout(side)

        self.status = label("", "muted")
        filters = QHBoxLayout()
        self.kinds = [("", t("disk_filter_all"))] + [(k, t(f"disk_kind_{k}")) for k in disk_tools.KINDS]
        self.kind_box = QComboBox()
        self.kind_box.addItems([lab for _, lab in self.kinds])
        self.min_box = QComboBox()
        self.min_box.addItems([lab for _, lab in T.MIN_SIZES])
        self.min_box.setCurrentIndex(1)
        for box in (self.kind_box, self.min_box):
            box.currentIndexChanged.connect(self.fill)
        filters.addWidget(label("TYPE", "head"))
        filters.addWidget(self.kind_box)
        filters.addSpacing(18)
        filters.addWidget(label("MIN SIZE", "head"))
        filters.addWidget(self.min_box)
        filters.addStretch(1)
        self.count = label("", "muted")
        filters.addWidget(self.count)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(T.DISK_COLUMNS))
        self.tree.setHeaderLabels([t(f"disk_col_{c}").upper() for c in T.DISK_COLUMNS])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        header = self.tree.header()
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        header.sectionClicked.connect(self._sort)
        for i, c in enumerate(T.DISK_COLUMNS):
            self.tree.setColumnWidth(i, WIDTHS[c])
        header.setSectionResizeMode(T.DISK_COLUMNS.index("what"), QHeaderView.ResizeMode.Stretch)
        header.setStretchLastSection(False)
        self.tree.itemSelectionChanged.connect(self._select)

        self.details = label(t("disk_select"), wrap=True)
        self.details.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.delete_button = button(t("disk_delete").upper(), "danger")
        self.delete_button.setEnabled(False)
        self.delete_button.clicked.connect(self._ask_delete)
        self.note = label("", "muted", wrap=True)
        self.result = label("", "ok", wrap=True)
        left = QVBoxLayout()
        left.addWidget(self.details)
        actions = QHBoxLayout()
        actions.addWidget(self.delete_button)
        actions.addWidget(self.note, 1)
        left.addLayout(actions)
        left.addWidget(self.result)
        left.addStretch(1)

        self.timeline = Timeline()
        self.growers = label("", "mono", wrap=True)
        self.growers.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        right = QVBoxLayout()
        right.addWidget(label("TIMELINE", "head"))
        right.addWidget(self.timeline)
        right.addWidget(label(t("disk_growers").upper(), "head"))
        right.addWidget(self.growers, 1)

        bottom = QHBoxLayout()
        bottom.addLayout(left, 3)
        bottom.addSpacing(24)
        bottom.addLayout(right, 2)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addLayout(top)
        lay.addWidget(self.status)
        lay.addLayout(filters)
        lay.addWidget(self.tree, 3)
        lay.addLayout(bottom, 2)

    def t(self, key, **kw):
        return i18n.get(self.lang, key, **kw)

    def sub(self):
        return f"LOCAL DISK {os.environ.get('SystemDrive', 'C:').upper()}"

    # --- data ---

    def opened(self):
        self.load()
        self.shared.send({"event": "disk_state"})

    def closing(self):
        if self.confirm is not None:
            self.confirm.pick(False)
        if self.store is not None:
            self.store.history.close()
            self.store = None

    def load(self):
        """The latest scan and its history from disk (small and fast: a few hundred items)."""
        if self.store is None:
            self.store = disk_memory.Store()
        try:
            self.scan = self.store.last_scan()
        except Exception as e:
            self.shared.log(f"disk scan read failed: {type(e).__name__}: {e}")
            self.scan = None
        self._status()
        self.fill()
        self._growers()
        self._timeline(self.selected)

    def changed(self, what):
        if what == "drives":
            self.drives.update()
        elif what == "disk":
            state = self.shared.data.get("disk_state") or {}
            was = getattr(self, "_running", False)
            self._running = bool(state.get("running"))
            if was and not self._running:
                self.load()  # a scan finished
            else:
                self._status()

    def message(self, msg):
        r = msg.get("disk_result")
        if r is None:
            return
        self.deleting = None
        self.result.setProperty("role", "ok" if r.get("ok") else "bad")
        self.result.setText(r.get("text", ""))
        self.result.style().polish(self.result)
        self.load()

    def _status(self):
        state = self.shared.data.get("disk_state") or {}
        running = bool(state.get("running"))
        self.scan_button.setEnabled(not running)
        self.status.setText(T.scan_status(self.lang, self.scan, running, state.get("progress", "")))

    def fill(self, *_):
        self.tree.clear()
        if self.scan is None:
            self.count.setText("")
            self._show(None)
            return
        kind = self.kinds[self.kind_box.currentIndex()][0]
        min_size = T.MIN_SIZES[self.min_box.currentIndex()][0]
        rows = T.disk_rows(self.scan, kind, min_size, self.sort_column, self.sort_reverse)
        by_id, users, same = T.graph(self.scan)
        keep = None
        items = []
        for it in rows:
            row = Row(T.disk_row(self.lang, it, by_id, users, same), it)
            state = T.disk_state(it)
            if state:
                brush = QBrush(QColor(RED if state == "broken" else C["muted"]))
                for c in range(len(T.DISK_COLUMNS)):
                    row.setForeground(c, brush)
            row.setTextAlignment(T.DISK_COLUMNS.index("size"), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            items.append(row)
            if self.selected is not None and it.id == self.selected.id:
                keep = row
        self.tree.addTopLevelItems(items)
        self.count.setText(f"{len(rows)} ITEMS · {size_text(sum(it.size for it in rows))}")
        self.tree.header().setSortIndicator(T.DISK_COLUMNS.index(self.sort_column),
                                            Qt.SortOrder.DescendingOrder if self.sort_reverse else Qt.SortOrder.AscendingOrder)
        if keep is not None:
            self.tree.setCurrentItem(keep)
            self.tree.scrollToItem(keep)
        else:
            self.selected = None
            self._show(None)

    def _sort(self, index):
        column = T.DISK_COLUMNS[index]
        if self.sort_column == column:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_column, self.sort_reverse = column, T.first_sort_reverse(column)
        self.fill()

    def _select(self):
        rows = self.tree.selectedItems()
        self.selected = rows[0].it if rows else None
        self._show(self.selected)

    def _show(self, it):
        if it is None:
            self.details.setText(self.t("disk_select"))
            self.delete_button.setEnabled(False)
            self.note.setText("")
            self._timeline(None)
            return
        self.details.setText(T.item_details(self.lang, it))
        why = T.no_delete_text(self.lang, it)
        self.delete_button.setEnabled(not why and self.deleting is None)
        self.note.setText(why)
        self._timeline(it)

    def _growers(self):
        try:
            rows = self.store.history.growers(days=7, limit=8)
        except Exception:
            rows = []
        self.growers.setText("\n".join(f"+{size_text(d)}  {name}" for _, name, _, d in rows) or self.t("disk_growers_none"))

    def _timeline(self, item):
        try:
            history = self.store.history
            totals = [(at, used) for at, used, _ in history.totals()]
            mine = history.item_sizes(item.id) if item is not None else []
        except Exception:
            totals, mine = [], []
        self.timeline.set(totals, mine, item.name if item is not None else "")

    # --- actions ---

    def _scan(self):
        self.shared.send({"event": "disk_scan"})
        self.scan_button.setEnabled(False)
        self.status.setText(self.t("disk_scanning"))

    def _ask_delete(self):
        it = self.selected
        p = disk_actions.plan(it) if it is not None else None
        if p is None or self.scan is None or self.confirm is not None:
            return
        going, breaking = disk_tools.dependents(it, self.scan.items)
        body = T.confirm_text(self.lang, it, p, going, breaking)
        self.confirm = Confirm(self, self.t("disk_confirm_title", name=it.name), body, self.t("disk_confirm_yes"),
                               self.t("disk_confirm_no"), lambda ok: self._answer(it, ok))

    def _answer(self, it, ok):
        self.confirm = None
        if not ok:
            return
        self.deleting = it.id
        self.delete_button.setEnabled(False)
        self.result.setProperty("role", "muted")
        self.result.setText(self.t("disk_deleting"))
        self.result.style().polish(self.result)
        self.shared.send({"event": "disk_delete", "id": it.id})

    def escape(self):
        if self.confirm is not None:
            self.confirm.pick(False)
            return True
        return False

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if self.confirm is not None:
            self.confirm.setGeometry(self.rect())
