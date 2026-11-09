"""API credit log: what each paid request cost and what it was for (the Ctrl+F1 panel's Credits tab).

One entry is one thing the owner asked for: a text-box message with all its tool rounds, a Telegram
message, a spoken turn, a voice note transcribed on OpenRouter, the disk scan's question to Gemini.
Code that makes paid requests wraps them in `activity(source, what)`; every OpenRouter response then
adds a round with the cost OpenRouter reports for it (usage.cost) and its tokens (`record`), and the
tools the model called (`note_tool`).

Around each entry the OpenRouter balance is read before the first request and again after the last,
so the panel shows before - after next to the reported cost. OpenRouter lowers the balance about a
minute late, so "after" is read AFTER_DELAYS seconds later, again until it has moved. When entries
overlap in time (one starts before another's "after" was read) their balance differences overlap
too: the entry is marked. Tavily: its
remaining credit is read before an entry's first web search and after the entry ends.

Stored in %LOCALAPPDATA%\\Umay\\credit_log.sqlite (UMAY_CREDIT_LOG moves it).
"""
import contextlib
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

SOURCES = ("box", "telegram", "voice", "stt", "disk", "other")
# Seconds after an entry ends when its "after" balance is read, until it shows the cost. Measured
# 2026-10-21: OpenRouter's /credits still had the old balance 36 s after a request and the new one
# (lower by exactly the reported cost) about a minute later.
AFTER_DELAYS = (10, 60, 180)
WHAT_CHARS = 200

_local = threading.local()
_lock = threading.Lock()
_state = {"ledger": None, "balance": None, "tavily": None, "open": set(), "listeners": []}


def path():
    custom = os.environ.get("UMAY_CREDIT_LOG", "").strip()
    return Path(custom) if custom else Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Umay" / "credit_log.sqlite"


def configure(balance=None, tavily=None, ledger=None):
    """balance() -> OpenRouter USD left or None; tavily() -> Tavily credits left or None."""
    if balance is not None:
        _state["balance"] = balance
    if tavily is not None:
        _state["tavily"] = tavily
    if ledger is not None:
        _state["ledger"] = ledger


def ledger():
    with _lock:
        if _state["ledger"] is None:
            _state["ledger"] = Ledger(path())
        return _state["ledger"]


def listen(fn):
    """fn() runs (on a worker thread) when an entry is added or settled; returns a function that stops it."""
    _state["listeners"].append(fn)
    return lambda: fn in _state["listeners"] and _state["listeners"].remove(fn)


def _changed():
    for fn in list(_state["listeners"]):
        try:
            fn()
        except Exception as e:
            print(f"Credit log: listener failed: {e}")


def _read(name):
    fn = _state[name]
    if fn is None:
        return None
    try:
        value = fn()
        return float(value) if value is not None else None
    except Exception:
        return None


@dataclass(eq=False)  # compared (and kept in the open set) by identity
class Entry:
    started: float
    source: str
    what: str
    id: int = 0
    finished: float = 0.0
    cost: float = 0.0             # what OpenRouter reported, all rounds
    requests: int = 0
    before: float = None          # OpenRouter balance before / after (USD)
    after: float = None
    tavily_before: float = None   # Tavily credits left before the first search / after the entry
    tavily_after: float = None
    searches: int = 0
    overlap: bool = False         # another entry ran at the same time: the balance difference mixes them
    rounds: list = field(default_factory=list)

    @property
    def difference(self):
        """before - after, or None until both are known."""
        if self.before is None or self.after is None:
            return None
        return self.before - self.after

    def detail(self):
        return json.dumps({"rounds": self.rounds, "searches": self.searches}, ensure_ascii=False)


@contextlib.contextmanager
def activity(source, what):
    """Everything paid inside this block is one entry. Nested blocks (a spoken turn calling
    answer_message) join the outer one."""
    current = getattr(_local, "entry", None)
    if current is not None:
        yield current
        return
    entry = Entry(time.time(), source if source in SOURCES else "other", " ".join(str(what).split())[:WHAT_CHARS])
    entry.before = _read("balance")
    with _lock:
        if _state["open"]:
            entry.overlap = True
            for other in _state["open"]:
                other.overlap = True
        _state["open"].add(entry)
    _local.entry = entry
    try:
        yield entry
    finally:
        _local.entry = None
        entry.finished = time.time()
        if entry.requests or entry.searches:
            entry.id = ledger().insert(entry)
            threading.Thread(target=_settle, args=(entry,), daemon=True).start()
            _changed()
        else:  # nothing paid happened (no key): no entry
            with _lock:
                _state["open"].discard(entry)


def _settle(entry, sleep=time.sleep):
    """Reads the balances after the entry, once OpenRouter has caught up, and saves them."""
    waited = 0
    try:
        for delay in AFTER_DELAYS:
            sleep(max(0, delay - waited))
            waited = delay
            entry.after = _read("balance")
            if entry.difference is not None and entry.difference >= entry.cost * 0.9:
                break
        if entry.tavily_before is not None:
            entry.tavily_after = _read("tavily")
    finally:
        with _lock:
            _state["open"].discard(entry)
        ledger().update(entry)
        _changed()


def record(model, usage, label=""):
    """One OpenRouter response: its model and `usage` (cost, prompt / completion tokens). Outside an
    activity it becomes an entry of its own (source "other")."""
    usage = usage or {}
    round_ = {"model": model, "cost": float(usage.get("cost") or 0),
              "prompt_tokens": int(usage.get("prompt_tokens") or 0),
              "completion_tokens": int(usage.get("completion_tokens") or 0), "tools": []}
    entry = getattr(_local, "entry", None)
    if entry is None:
        with activity("other", label or model) as entry:
            entry.before = None  # read after the request already happened: no honest "before"
            _add(entry, round_)
        return
    _add(entry, round_)


def _add(entry, round_):
    entry.rounds.append(round_)
    entry.requests += 1
    entry.cost += round_["cost"]


def note_tool(name):
    """A tool the model called in the latest round (web_search, look_at_screen, ...)."""
    entry = getattr(_local, "entry", None)
    if entry is not None and entry.rounds:
        entry.rounds[-1]["tools"].append(name)


def before_search():
    """Call right before a Tavily search: the first one in an entry reads Tavily's credit."""
    entry = getattr(_local, "entry", None)
    if entry is None:
        return
    if entry.tavily_before is None:
        entry.tavily_before = _read("tavily")
    entry.searches += 1


# --- storage -----------------------------------------------------------------------------------------


class Ledger:
    COLUMNS = ("id", "started", "finished", "source", "what", "cost", "requests", "before", "after",
               "tavily_before", "tavily_after", "overlap", "detail")

    def __init__(self, file):
        Path(file).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(file), check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY, started REAL, finished REAL, source TEXT, what TEXT, cost REAL,
            requests INTEGER, before REAL, after REAL, tavily_before REAL, tavily_after REAL,
            overlap INTEGER, detail TEXT)""")
        self.db.execute("CREATE INDEX IF NOT EXISTS entries_started ON entries (started)")
        self.db.commit()

    def _values(self, e):
        return (e.started, e.finished, e.source, e.what, e.cost, e.requests, e.before, e.after, e.tavily_before,
                e.tavily_after, int(e.overlap), e.detail())

    def insert(self, e):
        with self.lock, self.db:
            return self.db.execute("INSERT INTO entries (started, finished, source, what, cost, requests, before, "
                                   "after, tavily_before, tavily_after, overlap, detail) VALUES "
                                   "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", self._values(e)).lastrowid

    def update(self, e):
        with self.lock, self.db:
            self.db.execute("UPDATE entries SET started=?, finished=?, source=?, what=?, cost=?, requests=?, "
                            "before=?, after=?, tavily_before=?, tavily_after=?, overlap=?, detail=? WHERE id=?",
                            self._values(e) + (e.id,))

    def entries(self, since=0, source=None, limit=1000):
        """Newest first, as Entry objects."""
        sql = f"SELECT {', '.join(self.COLUMNS)} FROM entries WHERE started >= ?"
        args = [since]
        if source:
            sql += " AND source = ?"
            args.append(source)
        with self.lock:
            rows = self.db.execute(sql + " ORDER BY started DESC LIMIT ?", args + [limit]).fetchall()
        out = []
        for row in rows:
            data = dict(zip(self.COLUMNS, row))
            detail = json.loads(data.pop("detail") or "{}")
            data["overlap"] = bool(data["overlap"])
            out.append(Entry(rounds=detail.get("rounds", []), searches=detail.get("searches", 0), **data))
        return out

    def totals(self, since=0):
        """{source: (entries, reported cost)} since a time."""
        with self.lock:
            rows = self.db.execute("SELECT source, COUNT(*), SUM(cost) FROM entries WHERE started >= ? "
                                   "GROUP BY source", (since,)).fetchall()
        return {s: (n, c or 0.0) for s, n, c in rows}

    def close(self):
        with self.lock:
            self.db.close()
