"""The memory editor: a window to see and fix what Umay remembers (memory_cortex), in its own process
(`Umay.exe --memory`, `python memory_editor.py`, "open the memory editor", or a click on the HUD's MEMORY panel).
"Research file" opens self_research.md, the list of Umay's own research (memory_cortex.research_file).

- The tree shows User and Self with their facts; facts still waiting to be filed sit under "Inbox".
- Rename: double-click a fact (or F2). The new text gets a new embedding (Ollama; if it's not running the old one
  stays until the next rename).
- Move (re-parent): drag a fact onto its new parent.
- Hide: Delete. The fact is superseded (valid_to), kept as history, and "Show hidden" brings it back with Restore.
- Delete for good: Shift+Delete, after a yes. Its children move up to its parent.

Roots and the seeded categories can't be renamed, moved or deleted.
"""
import os
import re
import subprocess
import sys
from datetime import datetime

import paths

INBOX = "__inbox__"
FIXED_TYPES = ("root",)
ASKED = re.compile(r"\b(open|show)\b.{0,20}\bmemory (editor|window)\b", re.IGNORECASE)  # English only (the owner)


def asked(text):
    """"open the memory editor": a shortcut, no model needed. The HUD's MEMORY panel opens it too."""
    return bool(ASKED.search(text or ""))


def open_editor():
    """Starts the editor as its own process (from Umay: a voice / Telegram / text request)."""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--memory"]
    else:
        cmd = [sys.executable, "-X", "utf8", str(paths.resource_dir() / "memory_editor.py")]
    subprocess.Popen(cmd, env=paths.own_child_env(), close_fds=True)


def is_fixed(node):
    """Roots and the seeded categories stay as they are."""
    return node["type"] in FIXED_TYPES or (node["type"] == "category" and node["source"] is None)


def main():
    paths.load_env()
    import memory_cortex  # numpy before Qt: imported after it, numpy hung the HUD's process (2026-11-07)
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeySequence, QShortcut
    from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QHBoxLayout, QLabel, QMessageBox,
                                   QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

    store = memory_cortex.Store()
    app = QApplication(sys.argv[:1])

    class Tree(QTreeWidget):
        def dropEvent(self, event):
            item = self.currentItem()
            target = self.itemAt(event.position().toPoint())
            nid = item.data(0, Qt.ItemDataRole.UserRole) if item else None
            parent = target.data(0, Qt.ItemDataRole.UserRole) if target else None
            node = store.node(nid) if nid else None
            new_parent = store.node(parent) if parent and parent != INBOX else None
            event.ignore()
            if not node or not new_parent or is_fixed(node) or new_parent["id"] == node["id"]:
                return
            if new_parent["branch"] != node["branch"] or _is_under(new_parent["id"], node["id"]):
                status.setText("Can't move it there (another branch, or into its own children).")
                return
            if node["status"] == "inbox":
                store.place(node["id"], new_parent["id"])
            else:
                store.update(node["id"], parent=new_parent["id"])
            status.setText(f"Moved \"{node['label']}\" under \"{new_parent['label']}\".")
            fill()

    def _is_under(nid, ancestor):
        seen = set()
        while nid and nid not in seen:
            if nid == ancestor:
                return True
            seen.add(nid)
            n = store.node(nid)
            nid = n["parent"] if n else None
        return False

    win = QWidget()
    win.setWindowTitle("Umay · Memory")
    win.resize(760, 680)
    tree = Tree()
    tree.setHeaderLabels(["Memory", "Sure", "Said", "From"])
    tree.setColumnWidth(0, 420)
    tree.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
    tree.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed)
    show_hidden = QCheckBox("Show hidden")
    status = QLabel("Double-click to rename · drag to move · Delete hides · Shift+Delete deletes for good")
    status.setWordWrap(True)
    buttons = {name: QPushButton(name) for name in ("Hide", "Restore", "Delete for good", "Refresh", "Research file")}
    row = QHBoxLayout()
    row.addWidget(show_hidden)
    row.addStretch(1)
    for b in buttons.values():
        row.addWidget(b)
    lay = QVBoxLayout(win)
    lay.addWidget(tree)
    lay.addLayout(row)
    lay.addWidget(status)
    filling = {"on": False}

    def fill():
        filling["on"] = True
        open_ids = set()
        it = tree.invisibleRootItem()
        stack = [it.child(i) for i in range(it.childCount())]
        while stack:
            item = stack.pop()
            if item.isExpanded():
                open_ids.add(item.data(0, Qt.ItemDataRole.UserRole))
            stack.extend(item.child(i) for i in range(item.childCount()))
        tree.clear()
        nodes = store.nodes("1=1" if show_hidden.isChecked() else "valid_to IS NULL")
        items = {}
        for n in nodes:
            when = datetime.fromtimestamp((n["said"] or n["created"] or 0) / 1000).strftime("%Y-%m-%d") \
                if (n["said"] or n["created"]) else ""
            label = n["label"] + (" (hidden)" if n["valid_to"] else "")
            item = QTreeWidgetItem([label, f"{n['confidence'] or 0:.1f}", when, (n["source"] or "")[:40]])
            item.setData(0, Qt.ItemDataRole.UserRole, n["id"])
            flags = item.flags() | Qt.ItemFlag.ItemIsDropEnabled
            if not is_fixed(n):
                flags |= Qt.ItemFlag.ItemIsEditable | Qt.ItemFlag.ItemIsDragEnabled
            else:
                flags &= ~Qt.ItemFlag.ItemIsDragEnabled
            item.setFlags(flags)
            if n["valid_to"]:
                item.setForeground(0, Qt.GlobalColor.gray)
            if n["type"] == "episode" and n["detail"]:
                item.setToolTip(0, n["detail"])
            items[n["id"]] = item
        inbox = QTreeWidgetItem(["Inbox (not filed yet)", "", "", ""])
        inbox.setData(0, Qt.ItemDataRole.UserRole, INBOX)
        inbox.setFlags(inbox.flags() & ~Qt.ItemFlag.ItemIsDragEnabled & ~Qt.ItemFlag.ItemIsDropEnabled)
        for n in nodes:
            item = items[n["id"]]
            if n["status"] == "inbox":
                inbox.addChild(item)
            elif n["parent"] in items:
                items[n["parent"]].addChild(item)
            else:
                tree.addTopLevelItem(item)
        if inbox.childCount():
            tree.addTopLevelItem(inbox)
        for nid, item in items.items():
            item.setExpanded(nid in open_ids or nid in ("user", "self"))
        inbox.setExpanded(True)
        filling["on"] = False

    def selected():
        item = tree.currentItem()
        nid = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        return store.node(nid) if nid and nid != INBOX else None

    def renamed(item, column):
        if filling["on"] or column != 0:
            return
        node = store.node(item.data(0, Qt.ItemDataRole.UserRole))
        text = item.text(0).replace(" (hidden)", "").strip()
        if not node or is_fixed(node) or not text or text == node["label"]:
            return
        vec = memory_cortex.embed_one(text, timeout=10)
        store.update(node["id"], label=text[:200], **({"embedding": vec} if vec is not None else {}))
        status.setText(f"Renamed to \"{text}\"." + ("" if vec is not None else " (Ollama is off: search uses the old wording.)"))

    def hide():
        node = selected()
        if node and not is_fixed(node) and not node["valid_to"]:
            store.supersede(node["id"])
            status.setText(f"Hidden: \"{node['label']}\" (kept as history; Show hidden + Restore brings it back).")
            fill()

    def restore():
        node = selected()
        if node and node["valid_to"]:
            store.update(node["id"], valid_to=None)
            status.setText(f"Restored: \"{node['label']}\".")
            fill()

    def delete():
        node = selected()
        if not node or is_fixed(node):
            return
        kids = len(store.nodes("parent=?", (node["id"],)))
        sure = QMessageBox.question(win, "Delete for good",
                                    f"Delete \"{node['label']}\" for good? This can't be undone."
                                    + (f" Its {kids} children move up a level." if kids else ""))
        if sure == QMessageBox.StandardButton.Yes:
            store.remove(node["id"])
            status.setText(f"Deleted: \"{node['label']}\".")
            fill()

    tree.itemChanged.connect(renamed)
    show_hidden.toggled.connect(lambda _: fill())
    buttons["Hide"].clicked.connect(hide)
    buttons["Restore"].clicked.connect(restore)
    buttons["Delete for good"].clicked.connect(delete)
    buttons["Refresh"].clicked.connect(fill)

    def research_file():
        path = memory_cortex.research_file()
        if not path.exists():
            status.setText("No research yet: Umay researches only while you are away.")
            return
        os.startfile(str(path))  # noqa: the default app for .md (Windows only, like the rest of Umay)
    buttons["Research file"].clicked.connect(research_file)
    QShortcut(QKeySequence(Qt.Key.Key_Delete), tree, hide)
    QShortcut(QKeySequence(Qt.Modifier.SHIFT | Qt.Key.Key_Delete), tree, delete)
    QShortcut(QKeySequence(Qt.Key.Key_F5), win, fill)
    fill()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
