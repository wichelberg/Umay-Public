"""05 // CREDITS's detail view (hud_detail.py): the API credit log, which was the Ctrl+F1 panel's Credits tab.
The OpenRouter and Tavily credit left (Umay reads them: {"event": "balances"}, answered with {"balances"}),
totals for today / 7 / 30 days and by source, then every entry: when, from where (text box, Telegram, voice,
cloud transcription, disk scan), what it was for, the model, the cost OpenRouter reported, the balance before
and after and their difference. Selecting an entry shows each request in it (model, tokens, cost, the tools
it called) and Tavily's credit before / after.

The log is read from its file (credit_log.sqlite), again every few seconds while the view is open.
"""
import os
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QLabel, QPushButton,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout)

import credit_log
import i18n
import panel_text as T
from hud_detail import View
from hud_paint import C

POLL_MS = 3000
WIDTHS = {"time": 130, "source": 130, "what": 300, "model": 200, "requests": 50, "cost": 90, "before": 90,
          "after": 90, "difference": 110}
NUMBERS = ("requests", "cost", "before", "after", "difference")


def label(text_, role=None, wrap=False, size=None):
    w = QLabel(text_)
    if role:
        w.setProperty("role", role)
    if size:
        w.setStyleSheet(f"font-size: {size}px;")
    w.setWordWrap(wrap)
    w.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return w


def balances_text(lang, balances):
    """(OpenRouter, Tavily) lines from Umay's {"balances"} answer (None: not asked yet)."""
    if balances is None:
        return i18n.get(lang, "panel_loading"), i18n.get(lang, "panel_loading")
    tavily = balances.get("tavily") or ("error", None)
    return T.openrouter_text(lang, balances.get("openrouter")), T.tavily_text(lang, tuple(tavily))


class Row(QTreeWidgetItem):
    def __init__(self, values, entry):
        super().__init__(list(values))
        self.entry = entry


class CreditsView(View):
    NAME, INDEX, TITLE = "credits", "05", "CREDITS"

    def __init__(self, shared):
        super().__init__(shared)
        self.lang = shared.lang
        self.ledger, self.mtime = None, None
        t = self.t

        top = QHBoxLayout()
        self.openrouter = label("", size=30)
        self.tavily = label("", size=30)
        for name, value in (("OPENROUTER", self.openrouter), ("TAVILY", self.tavily)):
            col = QVBoxLayout()
            col.addWidget(label(name, "head"))
            col.addWidget(value)
            top.addLayout(col)
            top.addSpacing(40)
        refresh = QPushButton("REFRESH")
        refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh.clicked.connect(self.ask_balances)
        top.addWidget(refresh, 0, Qt.AlignmentFlag.AlignBottom)
        top.addStretch(1)
        self.local = label("", "muted", wrap=True)
        top.addWidget(self.local, 0, Qt.AlignmentFlag.AlignBottom)

        self.summary = label("", size=17)
        self.by_source = label("", "muted", wrap=True)

        filters = QHBoxLayout()
        self.periods = [(p, t(f"credits_period_{p}")) for p, _ in T.PERIODS]
        self.sources = [("", t("credits_all_sources"))] + [(s, t(f"credits_source_{s}")) for s in credit_log.SOURCES]
        self.period_box, self.source_box = QComboBox(), QComboBox()
        self.period_box.addItems([lab for _, lab in self.periods])
        self.period_box.setCurrentIndex(1)
        self.source_box.addItems([lab for _, lab in self.sources])
        for box in (self.period_box, self.source_box):
            box.currentIndexChanged.connect(self.refresh)
        filters.addWidget(label("PERIOD", "head"))
        filters.addWidget(self.period_box)
        filters.addSpacing(18)
        filters.addWidget(label("SOURCE", "head"))
        filters.addWidget(self.source_box)
        filters.addSpacing(18)
        filters.addWidget(label(t("credits_hint"), "muted"), 1)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(T.CREDIT_COLUMNS))
        self.tree.setHeaderLabels([t(f"credits_col_{c}").upper() for c in T.CREDIT_COLUMNS])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        for i, c in enumerate(T.CREDIT_COLUMNS):
            self.tree.setColumnWidth(i, WIDTHS[c])
        self.tree.header().setSectionResizeMode(T.CREDIT_COLUMNS.index("what"), QHeaderView.ResizeMode.Stretch)
        self.tree.header().setStretchLastSection(False)
        self.tree.itemSelectionChanged.connect(self._select)

        self.detail = label(t("credits_select"), "mono", wrap=True)
        self.detail.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.detail.setMinimumHeight(120)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addLayout(top)
        lay.addSpacing(6)
        lay.addWidget(self.summary)
        lay.addWidget(self.by_source)
        lay.addLayout(filters)
        lay.addWidget(self.tree, 1)
        lay.addWidget(self.detail)

        self.poll = QTimer(self)
        self.poll.timeout.connect(self._poll)

    def t(self, key, **kw):
        return i18n.get(self.lang, key, **kw)

    def sub(self):
        return "API CREDIT LOG"

    def opened(self):
        self.changed("balances")
        self.ask_balances()
        self.refresh()
        self.poll.start(POLL_MS)

    def closing(self):
        self.poll.stop()
        if self.ledger is not None:
            self.ledger.close()
            self.ledger = None

    def ask_balances(self):
        self.shared.send({"event": "balances"})

    def changed(self, what):
        if what != "balances":
            return
        o, t = balances_text(self.lang, self.shared.data.get("balances"))
        self.openrouter.setText(o)
        self.tavily.setText(t)
        local = os.environ.get("UMAY_LLM", "").strip().lower() != "openrouter"
        self.local.setText("REPLIES COME FROM THE LOCAL MODEL: FREE" if local else "")

    def _poll(self):
        try:
            mtime = os.path.getmtime(credit_log.path())
        except OSError:
            return
        if mtime != self.mtime:
            self.refresh()

    def refresh(self, *_):
        try:
            self.mtime = os.path.getmtime(credit_log.path())
        except OSError:
            self.mtime = None
        if self.ledger is None:
            self.ledger = credit_log.Ledger(credit_log.path())
        led, now = self.ledger, time.time()
        self.summary.setText(T.credit_summary(self.lang, led, now))
        period = self.periods[self.period_box.currentIndex()][0]
        source = self.sources[self.source_box.currentIndex()][0] or None
        self.by_source.setText(T.by_source_text(self.lang, led.totals(T.since(period, now))))
        selected = self._selected_id()
        self.tree.clear()
        keep, rows = None, []
        for e in led.entries(T.since(period, now), source):
            row = Row(T.credit_row(self.lang, e), e)
            state = T.credit_state(e)
            if state:
                brush = QBrush(QColor(C["cyan"] if state == "pending" else C["muted"]))
                for c in range(len(T.CREDIT_COLUMNS)):
                    row.setForeground(c, brush)
            for c in NUMBERS:
                row.setTextAlignment(T.CREDIT_COLUMNS.index(c), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            rows.append(row)
            if e.id == selected:
                keep = row
        self.tree.addTopLevelItems(rows)
        if keep is not None:
            self.tree.setCurrentItem(keep)
        else:
            self.detail.setText(self.t("credits_select"))

    def _selected_id(self):
        rows = self.tree.selectedItems()
        return rows[0].entry.id if rows else None

    def _select(self):
        rows = self.tree.selectedItems()
        self.detail.setText(T.credit_details(self.lang, rows[0].entry) if rows else self.t("credits_select"))
