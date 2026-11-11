"""06 // AUDIO: the panel Ctrl+F3 opens (and closes), a detail view (hud_detail.py) with no panel of its own.

- STOP LISTENING / START LISTENING: what Ctrl+F3 alone did before: the microphone closes to her (the wake phrase,
  a spoken conversation, a spoken yes/no), or opens again (voice_session.set_mute).
- MICROPHONE and SPEAKERS / HEADPHONES: the devices Windows has right now. Umay reads them again at every open
  ({"event": "audio_state", "scan": true}), so a headset plugged in a moment ago is there. A click uses it at
  once ({"event": "audio_set", "input" | "output": name, "" = Windows' default}) and it is kept across restarts
  (audio_devices.py). A chosen device that isn't plugged in now shows as "not connected"; Windows' default is
  used meanwhile.

Umay answers with {"audio": {inputs, outputs, default_input, default_output, input, output, muted, listening}}.
"""
from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QLabel, QPushButton, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout)

from hud_detail import View
from hud_paint import C

DEFAULT = ""          # the item that means Windows' default
WAITING = {"muted": "Not listening: stopped here.",
           "off": "Not listening: the wake phrase is off (UMAY_WAKE=off).",
           "waiting": "Not listening right now: on battery, in a game, or the voice isn't ready yet."}


def label(text_, role=None, size=None, wrap=False):
    w = QLabel(text_)
    if role:
        w.setProperty("role", role)
    if size:
        w.setStyleSheet(f"font-size: {size}px;")
    w.setWordWrap(wrap)
    return w


def rows(names, default, chosen):
    """[(the line shown, the name it stands for, is it the one in use, can it be used)] for one list: Windows'
    default first, then each device; a chosen device that isn't connected now is shown too, unusable."""
    out = [(f"Windows default{f'  ({default})' if default else ''}", DEFAULT, not chosen, True)]
    out += [(name, name, name == chosen, True) for name in names]
    if chosen and chosen not in names:
        out.append((f"{chosen}  (not connected now)", chosen, True, False))
    return out


def listening_text(state):
    """One line: on which microphone the wake phrase is listened for, or why not."""
    listening = (state or {}).get("listening") or {}
    if listening.get("on"):
        return f"Listening for “Hey Umay” on {listening.get('device') or 'the default microphone'}."
    return WAITING.get(listening.get("why"), "Not listening right now.")


class Item(QTreeWidgetItem):
    def __init__(self, text_, name, usable):
        super().__init__([text_])
        self.name, self.usable = name, usable


class AudioView(View):
    NAME, INDEX, TITLE = "audio", "06", "AUDIO"
    SIZE = (980, 540)

    def __init__(self, shared):
        super().__init__(shared)
        self.state = None

        top = QHBoxLayout()
        col = QVBoxLayout()
        self.big = label("SCANNING…", size=30)
        self.line = label("", "muted", wrap=True)
        col.addWidget(self.big)
        col.addWidget(self.line)
        top.addLayout(col, 1)
        self.mute = QPushButton("STOP LISTENING")
        self.mute.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mute.setMinimumWidth(230)
        self.mute.clicked.connect(self._mute)
        top.addWidget(self.mute, 0, Qt.AlignmentFlag.AlignVCenter)

        lists = QHBoxLayout()
        self.inputs, self.outputs = self._list("input"), self._list("output")
        for title, tree in (("MICROPHONE", self.inputs), ("SPEAKERS / HEADPHONES", self.outputs)):
            box = QVBoxLayout()
            box.addWidget(label(title, "head"))
            box.addWidget(tree, 1)
            lists.addLayout(box, 1)
            lists.addSpacing(16)

        self.note = label("", "muted", wrap=True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        lay.addLayout(top)
        lay.addSpacing(6)
        lay.addLayout(lists, 1)
        lay.addWidget(self.note)
        lay.addWidget(label("Ctrl+F3 opens and closes this panel and reads the devices again. A click uses a device "
                            "at once and keeps it; one that isn't connected now leaves Windows' default in use. The "
                            "virtual cables of the WhatsApp call aren't listed.", "muted", wrap=True))

    def _list(self, kind):
        tree = QTreeWidget()
        tree.setColumnCount(1)
        tree.setHeaderHidden(True)
        tree.setRootIsDecorated(False)
        tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        tree.setCursor(Qt.CursorShape.PointingHandCursor)
        tree.setStyleSheet("QTreeWidget { font-family: 'Rajdhani'; font-size: 16px; font-weight: 600; }"
                           "QTreeWidget::item { padding: 7px 6px; }")
        tree.itemClicked.connect(lambda item, _col: self._chose(kind, item))
        return tree

    def sub(self):
        return "MICROPHONE · SPEAKERS"

    def opened(self):
        self.shared.send({"event": "audio_state", "scan": True})

    def changed(self, what):
        if what == "audio":
            self.show_state(self.shared.data.get("audio"))

    def show_state(self, state):
        self.state = state or {}
        muted = bool(self.state.get("muted"))
        self.big.setText("MUTED" if muted else "LISTENING")
        self.big.setProperty("role", "bad" if muted else "ok")
        self.big.style().unpolish(self.big)
        self.big.style().polish(self.big)
        self.line.setText(listening_text(self.state))
        self.mute.setText("START LISTENING" if muted else "STOP LISTENING")
        self.mute.setProperty("role", "" if muted else "danger")
        self.mute.style().unpolish(self.mute)
        self.mute.style().polish(self.mute)
        self.mute.setEnabled(True)
        self._fill(self.inputs, rows(self.state.get("inputs") or [], self.state.get("default_input"),
                                     self.state.get("input")))
        self._fill(self.outputs, rows(self.state.get("outputs") or [], self.state.get("default_output"),
                                      self.state.get("output")))
        error = self.state.get("error")
        self.note.setText(f"The devices couldn't be read: {error}" if error else "")

    def _fill(self, tree, lines):
        tree.blockSignals(True)
        tree.clear()
        for text_, name, used, usable in lines:
            item = Item(("●  " if used else "    ") + text_, name, usable)
            if used:
                item.setForeground(0, QBrush(QColor(C["cyan"] if usable else C["hot"])))
            elif not usable:
                item.setForeground(0, QBrush(QColor(C["muted"])))
            tree.addTopLevelItem(item)
            if used:
                tree.setCurrentItem(item)
        tree.blockSignals(False)

    def _chose(self, kind, item):
        if not isinstance(item, Item) or not item.usable or self.state is None:
            return
        if (self.state.get(kind) or DEFAULT) == item.name:
            return
        self.note.setText("Switching…")
        self.shared.send({"event": "audio_set", kind: item.name})

    def _mute(self):
        if self.state is None:
            return
        self.mute.setEnabled(False)
        self.shared.send({"event": "audio_set", "muted": not self.state.get("muted")})
