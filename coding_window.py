"""Coding mode's window (Qt, its own process: `Umay.exe --coding <umay pid>`, `python coding_window.py <pid>`),
started by coding_mode.open_window ("Activate coding mode", Ctrl+F7). Only a view: Umay sends one snapshot per change
on stdin ({"state": {...}}, {"log": line}, {"front": true}, {"close": true}) and this sends events on stdout
({"event": "task" | "folder" | "keep" | "revert" | "stop" | "voice" | "ready", ...}). English, like the HUD.

- Top: the folder (Pick folder...), and the voice button. Voice -> coding OFF is the normal Umay listening and this
  window for typing; ON is every spoken word here going to the 35B and never to the normal Umay.
- Middle: Progress (what the 35B did, one line an action) and Changes (what really changed, as a diff).
- Bottom: the task box. While a task waits for a decision the box is closed and a tick (keep) and a cross (undo)
  open: no new task until one is pressed.
- The window ends with Umay (her pid is checked), or with its X.
"""
import json
import sys
import threading

import paths

GREEN, PINK, AMBER, DIM = "#3DF5A8", "#FF4FA3", "#FFB23E", "#7f8a93"
STYLE = """
QWidget { background: #0d1117; color: #e6edf3; font-size: 13px; }
QPlainTextEdit, QTextEdit, QLineEdit { background: #010409; border: 1px solid #30363d; border-radius: 6px; padding: 6px; }
QPushButton { background: #21262d; border: 1px solid #30363d; border-radius: 6px; padding: 7px 14px; }
QPushButton:hover { background: #30363d; }
QPushButton:disabled { color: #484f58; }
QTabWidget::pane { border: 1px solid #30363d; }
QTabBar::tab { background: #161b22; padding: 6px 14px; }
QTabBar::tab:selected { background: #21262d; }
#keep { background: #12361f; border-color: #2ea043; color: #3DF5A8; font-size: 15px; font-weight: bold; }
#undo { background: #3a1420; border-color: #da3633; color: #FF4FA3; font-size: 15px; font-weight: bold; }
#voice:checked { background: #12361f; border-color: #2ea043; color: #3DF5A8; }
#notice { color: #FFB23E; }
"""


def diff_html(text):
    import html
    rows = []
    for line in text.splitlines():
        color = GREEN if line.startswith("+") and not line.startswith("+++") else \
            PINK if line.startswith("-") and not line.startswith("---") else \
            AMBER if line.startswith("@@") else DIM if line.startswith(("---", "+++")) else None
        body = html.escape(line) or "&nbsp;"
        rows.append(f'<span style="color:{color}">{body}</span>' if color else body)
    return '<pre style="font-family:Consolas,monospace">' + "<br>".join(rows) + "</pre>"


def main(parent_pid=0):
    import psutil
    from PySide6.QtCore import QObject, Qt, QTimer, Signal
    from PySide6.QtWidgets import (QApplication, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
                                   QPushButton, QTabWidget, QTextEdit, QVBoxLayout, QWidget)

    paths.load_env()
    app = QApplication(sys.argv[:1])
    app.setStyleSheet(STYLE)

    class Feed(QObject):
        got = Signal(dict)
        ended = Signal()

    feed = Feed()

    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    def read():
        for line in sys.stdin:
            try:
                feed.got.emit(json.loads(line))
            except ValueError:
                continue
        feed.ended.emit()

    win = QWidget()
    win.setWindowTitle("Umay · Coding mode")
    win.resize(900, 720)

    folder = QLabel("No folder")
    folder.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    pick = QPushButton("Pick folder...")
    voice = QPushButton("Voice → coding: OFF")
    voice.setObjectName("voice")
    voice.setCheckable(True)
    voice.setToolTip("OFF: voice goes to the normal Umay and this window is for typing. ON: everything you say goes here.")
    notice = QLabel("")
    notice.setObjectName("notice")
    notice.setWordWrap(True)
    log = QPlainTextEdit()
    log.setReadOnly(True)
    changes = QTextEdit()
    changes.setReadOnly(True)
    tabs = QTabWidget()
    tabs.addTab(log, "Progress")
    tabs.addTab(changes, "Changes")
    summary = QLabel("")
    summary.setWordWrap(True)
    keep = QPushButton("✔  Keep")
    keep.setObjectName("keep")
    undo = QPushButton("✖  Undo")
    undo.setObjectName("undo")
    task = QLineEdit()
    task.setPlaceholderText("Describe the task for the 35B...")
    go = QPushButton("Send")
    stop = QPushButton("Stop")

    top = QHBoxLayout()
    top.addWidget(folder, 1)
    top.addWidget(pick)
    top.addWidget(voice)
    decide = QHBoxLayout()
    decide.addWidget(keep, 1)
    decide.addWidget(undo, 1)
    box = QHBoxLayout()
    box.addWidget(task, 1)
    box.addWidget(go)
    box.addWidget(stop)
    lay = QVBoxLayout(win)
    lay.addLayout(top)
    lay.addWidget(notice)
    lay.addWidget(tabs, 1)
    lay.addWidget(summary)
    lay.addLayout(decide)
    lay.addLayout(box)

    current = {"state": {}}

    def render(state):
        current["state"] = state
        busy, pending = state.get("busy"), state.get("pending")
        folder.setText(state.get("folder") or "No folder")
        notice.setText(state.get("notice") or "")
        notice.setVisible(bool(state.get("notice")))
        voice.blockSignals(True)
        voice.setChecked(bool(state.get("voice_on")))
        voice.setText("Voice → coding: " + ("ON" if state.get("voice_on") else "OFF"))
        voice.blockSignals(False)
        log.setPlainText("\n".join(state.get("log") or []))
        log.verticalScrollBar().setValue(log.verticalScrollBar().maximum())
        if pending:
            rows = state.get("changes") or []
            head = "".join(f'<div><b>{r[0]}</b> — {r[1]} (<span style="color:{GREEN}">+{r[2]}</span> '
                           f'<span style="color:{PINK}">-{r[3]}</span>)</div>' for r in rows)
            changes.setHtml(head + diff_html(state.get("diff") or ""))
            tabs.setCurrentWidget(changes)
            text = "The 35B says: " + (state.get("summary") or "(no report)")
            if not rows:
                text += "\nNo file differs now."
            summary.setText(text + "\nTry it, then Keep (the change stays) or Undo (everything goes back). "
                                   "Files a test run changed may not come back.")
        else:
            changes.setPlainText("")
            summary.setText("")
            if busy:
                tabs.setCurrentWidget(log)
        summary.setVisible(bool(pending))
        keep.setVisible(bool(pending))
        undo.setVisible(bool(pending))
        task.setEnabled(not busy and not pending)
        go.setEnabled(not busy and not pending and bool(state.get("folder")))
        stop.setVisible(bool(busy))
        pick.setEnabled(not busy and not pending)
        if not state.get("llama", True):
            notice.setText("The 35B is not set up (UMAY_LLAMA_EXE / UMAY_LLAMA_MODEL).")
            notice.setVisible(True)

    def got(msg):
        if "state" in msg:
            render(msg["state"])
        elif "log" in msg:
            log.appendPlainText(msg["log"])
        elif msg.get("front"):
            win.showNormal()
            win.raise_()
            win.activateWindow()
        elif msg.get("close"):
            app.quit()

    def submit():
        text = task.text().strip()
        if text:
            task.clear()
            send({"event": "task", "text": text})

    def choose():
        start = current["state"].get("folder") or ""
        picked = QFileDialog.getExistingDirectory(win, "Pick the project folder", start)
        if picked:
            send({"event": "folder", "folder": picked})

    feed.got.connect(got)
    feed.ended.connect(app.quit)
    go.clicked.connect(submit)
    task.returnPressed.connect(submit)
    pick.clicked.connect(choose)
    keep.clicked.connect(lambda: send({"event": "keep"}))
    undo.clicked.connect(lambda: send({"event": "revert"}))
    stop.clicked.connect(lambda: send({"event": "stop"}))
    voice.toggled.connect(lambda on: send({"event": "voice", "on": bool(on)}))

    watch = QTimer()
    watch.timeout.connect(lambda: app.quit() if parent_pid and not psutil.pid_exists(parent_pid) else None)
    watch.start(3000)

    threading.Thread(target=read, daemon=True).start()
    win.show()
    send({"event": "ready"})
    return app.exec()


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[-1]) if sys.argv[-1].isdigit() else 0))
