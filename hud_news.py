"""06 // NEWSFEED's detail view (hud_detail.py): every article the HUD has read (hud_feeds.read_news), newest
first, filtered by a followed name or a section; a click on a headline opens the article in the browser (and
closes the view).
REFRESH reads the feeds again now (the HUD reads them every NEWS_SECONDS anyway).
"""
import time

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QBrush, QColor, QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QPushButton, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout)

import hud_feeds
from hud_detail import View
from hud_paint import C

COLUMNS = ("AGE", "TAGS", "HEADLINE", "SOURCE")
WIDTHS = (70, 230, 600, 190)
TICK_MS = 60_000  # the ages move on while it is open
WATCH_COL, TOPIC_COL = C["amber"], C["cyan2"]
QSS = f"""
QPushButton {{ padding: 4px 12px; font-size: 13px; letter-spacing: 1px; }}
QPushButton:checked {{ background: rgba(56, 214, 255, 75); color: {C['text']}; border-color: {C['cyan']}; }}
QPushButton[role="watch"] {{ color: {C['amber']}; border-color: rgba(255, 178, 62, 110);
    background: rgba(255, 178, 62, 18); }}
QPushButton[role="watch"]:checked {{ background: rgba(255, 178, 62, 70); color: {C['text']};
    border-color: {C['amber']}; }}
"""


def label(text_, role=None):
    w = QLabel(text_)
    if role:
        w.setProperty("role", role)
    return w


class Row(QTreeWidgetItem):
    def __init__(self, item, now):
        super().__init__([hud_feeds.age(item["at"], now), " · ".join(item["tags"]), item["title"], item["source"]])
        self.link = item["link"]
        self.setToolTip(2, item["title"])
        self.setForeground(1, QBrush(QColor(WATCH_COL if item["kind"] == "watch" else TOPIC_COL)))
        self.setForeground(0, QBrush(QColor(C["muted"])))
        self.setForeground(3, QBrush(QColor(C["soft"])))


class NewsView(View):
    NAME, INDEX, TITLE = "news", "06", "NEWSFEED"

    def __init__(self, shared):
        super().__init__(shared)
        self.setStyleSheet(QSS)
        self.filter, self.buttons, self.tags = "ALL", {}, None

        self.watch_row, self.topic_row = QHBoxLayout(), QHBoxLayout()
        top = QHBoxLayout()
        self.status = label("", "muted")
        refresh = QPushButton("REFRESH")
        refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh.clicked.connect(self.refresh)
        top.addWidget(label("FOLLOWING", "head"))
        top.addLayout(self.watch_row)
        top.addStretch(1)
        top.addWidget(self.status)
        top.addSpacing(12)
        top.addWidget(refresh)
        sections = QHBoxLayout()
        sections.addWidget(label("SECTIONS", "head"))
        sections.addLayout(self.topic_row)
        sections.addStretch(1)
        sections.addWidget(label("CLICK A HEADLINE TO OPEN IT IN THE BROWSER", "muted"))

        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(COLUMNS))
        self.tree.setHeaderLabels(list(COLUMNS))
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setCursor(Qt.CursorShape.PointingHandCursor)
        for i, width in enumerate(WIDTHS):
            self.tree.setColumnWidth(i, width)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setStretchLastSection(False)
        self.tree.setStyleSheet("QTreeWidget { font-family: 'Rajdhani'; font-size: 15px; font-weight: 600; }")
        self.tree.itemClicked.connect(self.open_item)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        lay.addLayout(top)
        lay.addLayout(sections)
        lay.addWidget(self.tree, 1)

        self.tick = QTimer(self)
        self.tick.timeout.connect(self.fill)

    def sub(self):
        d = self.shared.data.get("news")
        return f"GOOGLE NEWS · {d['region'].upper()}" if d else "GOOGLE NEWS"

    def opened(self):
        self.changed("news")
        self.tick.start(TICK_MS)

    def closing(self):
        self.tick.stop()

    def refresh(self):
        self.status.setText("READING…")
        wake = self.shared.wake.get("news")
        if wake is not None:
            wake.set()

    def changed(self, what):
        if what != "news":
            return
        d = self.shared.data.get("news")
        if not d:
            self.status.setText("ACQUIRING…")
            return
        tags = (tuple(d["watch"]), tuple(d["topics"]))
        if tags != self.tags:
            self.tags = tags
            self._buttons(d)
        failed = f" · {len(d['failed'])} FEED(S) FAILED" if d.get("failed") else ""
        self.status.setText(f"UPDATED {time.strftime('%H:%M', time.localtime(d['at']))}{failed}")
        self.fill()

    def _buttons(self, d):
        for row in (self.watch_row, self.topic_row):
            while row.count():
                row.takeAt(0).widget().deleteLater()
        self.buttons = {}
        for row, names, role in ((self.watch_row, ["ALL"] + d["watch"], "watch"), (self.topic_row, d["topics"], None)):
            for name in names:
                b = QPushButton(name)
                b.setCheckable(True)
                b.setCursor(Qt.CursorShape.PointingHandCursor)
                if role and name != "ALL":
                    b.setProperty("role", role)
                b.clicked.connect(lambda _=False, n=name: self.choose(n))
                row.addWidget(b)
                self.buttons[name] = b
        if self.filter not in self.buttons:
            self.filter = "ALL"
        self.buttons[self.filter].setChecked(True)

    def choose(self, name):
        self.filter = name
        for n, b in self.buttons.items():
            b.setChecked(n == name)
        self.fill()

    def fill(self):
        d = self.shared.data.get("news")
        if not d:
            return
        now = time.time()
        items = [i for i in d["items"] if self.filter == "ALL" or self.filter in i["tags"]]
        self.tree.clear()
        self.tree.addTopLevelItems([Row(i, now) for i in items])

    def open_item(self, item, _column=0):
        """The article in the browser; the detail view goes, or it would stay on top of it."""
        if getattr(item, "link", ""):
            QDesktopServices.openUrl(QUrl(item.link))
            self.parent().close_detail()
