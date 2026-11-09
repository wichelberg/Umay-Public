"""What the disk scanner remembers between scans. Everything is in %LOCALAPPDATA%\\Umay\\disk
(UMAY_DISK_HOME moves it):

- knowledge.json (edit it by hand): what an item is, kept by identity, never by path: a Steam appid,
  a program's name + publisher, a product + company from an exe, a model id. Each entry has a source:
  rule, exe (the exe's version info), gemini (a guess, shown as one) or owner. Write your own text
  with "source": "owner" and it is never overwritten; "redownloadable": true/false there also decides
  how the item is deleted. describe() looks here first; only what it still doesn't know goes to Gemini
  Flash (ai_assistant.ask_flash), with nothing but a folder's or program's name, the publisher and a
  few file names: no file contents and no personal paths.
- history.sqlite: each scan's used / free space and every item's size and last use (the panel's
  timeline, the daily scan's findings, which findings were already told).
- last_scan.json: the latest scan, so the panel shows it at once; cache.json: the scanner's cache.
"""
import json
import os
import re
import sqlite3
import threading
import time
from datetime import date
from pathlib import Path

import i18n
from disk_tools import Scan

ASKABLE = {"program", "folder"}   # the kinds rules can't explain
GEMINI_BATCH = 25
GEMINI_FILES = 8


def home():
    custom = os.environ.get("UMAY_DISK_HOME", "").strip()
    return Path(custom) if custom else Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Umay" / "disk"


# --- knowledge -------------------------------------------------------------------------------


class Knowledge:
    def __init__(self, path):
        self.path = Path(path)
        self.entries = {}
        if self.path.is_file():
            try:
                self.entries = json.loads(self.path.read_text(encoding="utf-8")).get("items", {})
            except (OSError, ValueError):
                self.entries = {}  # a broken hand edit: start over rather than stop scanning
        self.changed = False

    def get(self, key):
        return self.entries.get(key)

    def put(self, key, what, source, name="", redownloadable=None):
        entry = {"what": what, "source": source, "name": name, "updated": date.today().isoformat()}
        if redownloadable is not None:
            entry["redownloadable"] = bool(redownloadable)
        old = self.entries.get(key)
        if old and {k: v for k, v in old.items() if k != "updated"} == {k: v for k, v in entry.items() if k != "updated"}:
            return
        self.entries[key] = entry
        self.changed = True

    def save(self):
        if not self.changed:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": 1, "items": dict(sorted(self.entries.items()))}, ensure_ascii=False,
                                  indent=1), encoding="utf-8")
        os.replace(tmp, self.path)
        self.changed = False


def _apply(item, entry):
    item.what, item.what_source = entry.get("what", ""), entry.get("source", "")
    if "redownloadable" in entry:
        if entry["source"] == "owner":
            item.redownloadable = bool(entry["redownloadable"])
        else:
            item.info["redownloadable_guess"] = bool(entry["redownloadable"])


def _where(item):
    """Where a folder is, without the user's name: "AppData\\Local", "Program Files", "C:\\"."""
    name = item.name
    if name.startswith("~\\"):
        parent = os.path.dirname(name[2:])
        return parent or "user folder"
    return os.path.dirname(name.rstrip("\\")) or name


def _ask_payload(n, item):
    if item.kind == "program":
        entry = {"id": n, "program": item.name, "publisher": item.info.get("publisher", "")}
    else:
        entry = {"id": n, "folder": os.path.basename(item.path.rstrip("\\")), "in": _where(item)}
    try:
        names = sorted(e.name for e in os.scandir(item.path))[:GEMINI_FILES] if item.path else []
    except OSError:
        names = []
    if names:
        entry["files"] = names
    return entry


def _parse_answer(text):
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        rows = json.loads(text[start:end + 1])
    except ValueError:
        return []
    return [r for r in rows if isinstance(r, dict) and isinstance(r.get("what"), str)]


def describe(items, knowledge, ask=None, lang="tr"):
    """Fills what / what_source of every item: the owner's text first, then what the scan's rules or the
    exe said (saved to the knowledge too), then the knowledge, and only then Gemini, in batches.
    Returns how many requests went to Gemini."""
    pending = []
    for it in items:
        if not it.key:
            continue
        entry = knowledge.get(it.key)
        if entry and entry.get("source") == "owner":
            _apply(it, entry)
        elif it.what and it.what_source in ("rule", "exe"):
            if it.what_source == "exe" or it.kind in ASKABLE:  # the rest is the same text every scan
                knowledge.put(it.key, it.what, it.what_source, it.name)
        elif entry:
            _apply(it, entry)
        elif it.kind in ASKABLE and not it.container and not it.protected:
            pending.append(it)
    calls = 0
    if ask is None:
        return calls
    for start in range(0, len(pending), GEMINI_BATCH):
        batch = pending[start:start + GEMINI_BATCH]
        payload = [_ask_payload(n, it) for n, it in enumerate(batch)]
        calls += 1
        try:
            answer = ask(i18n.get(lang, "disk_gemini_prompt", items=json.dumps(payload, ensure_ascii=False)))
        except Exception as e:
            print(f"Disk: Gemini didn't answer: {e}")
            break
        for row in _parse_answer(answer):
            try:
                it = batch[int(row["id"])]
            except (KeyError, ValueError, IndexError, TypeError):
                continue
            redl = row.get("redownloadable")
            knowledge.put(it.key, row["what"].strip(), "gemini", it.name,
                          redl if isinstance(redl, bool) else None)
            _apply(it, knowledge.get(it.key))
    return calls


# --- history -----------------------------------------------------------------------------------


class History:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS scans (id INTEGER PRIMARY KEY, at REAL, total INTEGER, used INTEGER,
                                              free INTEGER, seconds REAL);
            CREATE TABLE IF NOT EXISTS sizes (scan INTEGER, item TEXT, size INTEGER, last_used REAL,
                                              PRIMARY KEY (scan, item));
            CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY, name TEXT, kind TEXT);
            CREATE TABLE IF NOT EXISTS told (key TEXT PRIMARY KEY, at REAL);
            CREATE INDEX IF NOT EXISTS sizes_item ON sizes (item);
        """)

    def add(self, scan):
        with self.lock, self.db:
            cur = self.db.execute("INSERT INTO scans (at, total, used, free, seconds) VALUES (?, ?, ?, ?, ?)",
                                  (scan.at, scan.total, scan.used, scan.free, scan.stats.get("seconds", 0)))
            sid = cur.lastrowid
            rows = [it for it in scan.items if it.kind != "link"]
            self.db.executemany("INSERT OR REPLACE INTO sizes VALUES (?, ?, ?, ?)",
                                [(sid, it.id, it.size, it.last_used) for it in rows])
            self.db.executemany("INSERT OR REPLACE INTO items VALUES (?, ?, ?)", [(it.id, it.name, it.kind) for it in rows])
            return sid

    def last_at(self):
        with self.lock:
            row = self.db.execute("SELECT MAX(at) FROM scans").fetchone()
        return row[0] or 0

    def totals(self):
        """[(at, used, free)] of every scan, oldest first."""
        with self.lock:
            return self.db.execute("SELECT at, used, free FROM scans ORDER BY at").fetchall()

    def item_sizes(self, item_id):
        with self.lock:
            return self.db.execute("SELECT s.at, z.size FROM sizes z JOIN scans s ON s.id = z.scan WHERE z.item = ? "
                                   "ORDER BY s.at", (item_id,)).fetchall()

    def sizes_before(self, scan_at):
        """{item: size} of the last scan before scan_at ({} if none)."""
        with self.lock:
            row = self.db.execute("SELECT id FROM scans WHERE at < ? ORDER BY at DESC LIMIT 1", (scan_at,)).fetchone()
            if row is None:
                return {}
            return dict(self.db.execute("SELECT item, size FROM sizes WHERE scan = ?", (row[0],)).fetchall())

    def growers(self, days=7, limit=10, now=None):
        """[(item id, name, kind, bytes grown)] over the last `days`: the newest scan against the last scan
        from before then (or the oldest scan in the window when there's none), biggest growth first."""
        now = now or time.time()
        with self.lock:
            scans = self.db.execute("SELECT id, at FROM scans ORDER BY at").fetchall()
            if len(scans) < 2:
                return []
            newest = scans[-1][0]
            before = [s for s in scans if s[1] <= now - days * 86400]
            base = before[-1][0] if before else scans[0][0]
            if base == newest:
                return []
            rows = self.db.execute(
                "SELECT n.item, i.name, i.kind, n.size - COALESCE(o.size, 0) FROM sizes n "
                "LEFT JOIN sizes o ON o.item = n.item AND o.scan = ? JOIN items i ON i.id = n.item "
                "WHERE n.scan = ? ORDER BY 4 DESC LIMIT ?", (base, newest, limit)).fetchall()
        return [r for r in rows if r[3] > 0]

    def told(self, key):
        with self.lock:
            return self.db.execute("SELECT 1 FROM told WHERE key = ?", (key,)).fetchone() is not None

    def tell(self, keys):
        with self.lock, self.db:
            self.db.executemany("INSERT OR REPLACE INTO told VALUES (?, ?)", [(k, time.time()) for k in keys])

    def close(self):
        with self.lock:
            self.db.close()


# --- the store: all of it in one folder ----------------------------------------------------------


class Store:
    def __init__(self, folder=None):
        self.folder = Path(folder or home())
        self.folder.mkdir(parents=True, exist_ok=True)
        self.knowledge = Knowledge(self.folder / "knowledge.json")
        self.history = History(self.folder / "history.sqlite")

    def load_cache(self):
        try:
            return json.loads((self.folder / "cache.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save_cache(self, cache):
        self._write("cache.json", cache)

    def last_scan(self):
        try:
            return Scan.from_dict(json.loads((self.folder / "last_scan.json").read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def save_scan(self, scan):
        self._write("last_scan.json", scan.to_dict())

    def _write(self, name, data):
        tmp = self.folder / (name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.folder / name)


def safe_name(text):
    return re.sub(r"[^\w.-]+", "_", text)[:80]
