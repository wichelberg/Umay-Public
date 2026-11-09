"""Umay's memory of past conversations: the logs in chat_logs (the text box, Telegram, spoken ones), which
search_memory reads. The owner's rules (2026-11-02):

- Once a conversation is over (its log unchanged for SETTLED_SECONDS), the local model calls it important or
  unimportant. One where the user said "remember this" is important without asking, and so is one the
  model isn't sure about: forgetting can't be undone. A log that grows again later is judged again.
- When the logs pass UMAY_MEMORY_LIMIT_MB (100), the unimportant ones are deleted, oldest first.
- Still over with only important ones left: the oldest important ones are summarized by the local model, one
  by one, until it fits. Dates are never forgotten: every date and time in a log that the summary leaves
  out is added under it (dates_in), and the file keeps its name, the conversation's own date.

It runs every CHECK_SECONDS in the background, a few logs at a time, and waits while it shouldn't use the
GPU (`can_run`: a game, or the local model answering). Labels are kept in chat_logs/memory_index.json.
Log: %LOCALAPPDATA%\\Umay\\memory.log. UMAY_MEMORY=off: none of it.
"""
import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

import requests

import local_llm
import paths

CHECK_SECONDS = 5 * 60
SETTLED_SECONDS = 15 * 60  # a log unchanged this long: that conversation is over
PER_CHECK = 5              # logs judged per check, so the GPU isn't held for long
JUDGE_CHARS = 8000         # of a long log, its start and end are judged
SUMMARY_CHUNK_CHARS = 10000
INDEX_NAME = "memory_index.json"
IMPORTANT, UNIMPORTANT = "important", "unimportant"

REMEMBER = re.compile(r"\b(remember (this|that)|don'?t forget|keep in mind|bunu (hatırla|unutma|aklında tut)|"
                      r"unutma|hatırla bunu|aklında tut)\b", re.IGNORECASE)
_MONTHS = ("january|february|march|april|may|june|july|august|september|october|november|december|"
           "ocak|şubat|mart|nisan|mayıs|haziran|temmuz|ağustos|eylül|ekim|kasım|aralık")
DATES = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b"                                      # 2026-11-02
    r"|\b\d{1,2}[./]\d{1,2}[./]\d{2,4}\b"                         # 02.11.2026, 2/11/26
    rf"|\b\d{{1,2}}\s+(?:{_MONTHS})(?:\s+\d{{4}})?\b"             # 12 Mart 2027
    rf"|\b(?:{_MONTHS})\s+\d{{1,2}}(?:,?\s+\d{{4}})?\b"           # March 12, 2027
    r"|\b\d{1,2}:\d{2}\b",                                        # 14:30
    re.IGNORECASE)

JUDGE_PROMPT = (
    "Below is a past conversation between the user and Umay, their assistant. Decide whether it is worth "
    "remembering long term.\n"
    "IMPORTANT: facts about the user or people in their life, their plans, appointments, dates, decisions, "
    "preferences, promises, problems they are working on, or anything they asked to remember.\n"
    "UNIMPORTANT: small talk, one-off commands (weather, open an app, play music, a screenshot), tests, "
    "greetings.\n"
    "If unsure, answer IMPORTANT. Reply with one word only: IMPORTANT or UNIMPORTANT.\n\n"
    "Conversation:\n{text}")
SUMMARY_PROMPT = (
    "Summarize this past conversation between the user and Umay, their assistant, in a few short sentences, "
    "keeping every fact about the user, every decision and plan, and EVERY date and time exactly as written. "
    "Write in the third person and call them \"the user\". Write only the summary.\n\nConversation:\n{text}")


def _limit_bytes():
    try:
        return float(os.environ.get("UMAY_MEMORY_LIMIT_MB", "").strip() or 100) * 2 ** 20
    except ValueError:
        return 100 * 2 ** 20


def enabled():
    return os.environ.get("UMAY_MEMORY", "").strip().lower() != "off"


def _log(text):
    try:
        path = paths.voice_home().parent / "memory.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}\n")
    except OSError:
        pass


# --- pure parts ------------------------------------------------------------------------------------------

def label_of(answer):
    """The model's answer as a label; anything unclear is important."""
    text = answer.strip().lower()
    return UNIMPORTANT if re.search(r"\bunimportant\b|önemsiz", text) else IMPORTANT


def for_judging(text, limit=JUDGE_CHARS):
    """A long log's start and end (where what it was about and how it ended are)."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + "\n[...]\n" + text[-half:]


def dates_in(text):
    """Every date and time written in a text, in order, once each."""
    seen = []
    for m in DATES.finditer(text):
        d = " ".join(m.group(0).split())
        if d.lower() not in (s.lower() for s in seen):
            seen.append(d)
    return seen


def keep_dates(original, summary):
    """The summary with the dates of the original it left out added under it: dates are never forgotten."""
    missing = [d for d in dates_in(original) if d.lower() not in summary.lower()]
    return summary.rstrip() + ("\nDates mentioned: " + ", ".join(missing) if missing else "")


def next_step(entries, total, limit):
    """What to do to get the logs under `limit` bytes: ("delete", name) for the oldest unimportant one,
    else ("summarize", name) for the oldest important one not summarized yet, else None. entries:
    [(name, label, summarized)] oldest first; logs not judged yet are left alone."""
    if total <= limit:
        return None
    for name, label, _ in entries:
        if label == UNIMPORTANT:
            return "delete", name
    for name, label, summarized in entries:
        if label == IMPORTANT and not summarized:
            return "summarize", name
    return None


# --- the folder ------------------------------------------------------------------------------------------

class Memory:
    def __init__(self, folder, ask, now=time.time):
        """folder: chat_logs; ask(prompt) -> the local model's answer; now() -> seconds (epoch)."""
        self.folder, self.ask, self.now = Path(folder), ask, now
        self.index_path = self.folder / INDEX_NAME
        try:
            self.index = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.index = {}

    def save(self):
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.index, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.index_path)

    def logs(self):
        """The conversation logs, oldest first (by the time in their names, which they keep)."""
        return sorted((p for p in self.folder.glob("*.txt")), key=lambda p: (p.stem.split("_", 1)[-1]
                      if p.stem.startswith("telegram_") else p.stem))

    def total(self):
        return sum(p.stat().st_size for p in self.logs())

    def to_judge(self):
        """Logs of conversations that are over and weren't judged since they last changed."""
        out = []
        for p in self.logs():
            mtime = p.stat().st_mtime
            seen = self.index.get(p.name)
            if self.now() - mtime >= SETTLED_SECONDS and (seen is None or seen.get("mtime") != mtime):
                out.append(p)
        return out

    def judge(self, path):
        text = path.read_text(encoding="utf-8", errors="replace")
        user_lines = "\n".join(line for line in text.splitlines() if not line.startswith("Umay:"))
        if REMEMBER.search(user_lines):
            label, why = IMPORTANT, "asked to remember"
        elif not text.strip():
            label, why = UNIMPORTANT, "empty"
        else:
            label, why = label_of(self.ask(JUDGE_PROMPT.format(text=for_judging(text)))), "model"
        old = self.index.get(path.name, {})
        self.index[path.name] = {"label": label, "mtime": path.stat().st_mtime,
                                 "summarized": old.get("summarized", False)}
        self.save()
        _log(f"{path.name}: {label} ({why}, {len(text)} chars)")
        return label

    def summarize(self, path):
        text = path.read_text(encoding="utf-8", errors="replace")
        chunks = [text[i:i + SUMMARY_CHUNK_CHARS] for i in range(0, len(text), SUMMARY_CHUNK_CHARS)] or [""]
        summary = "\n".join(self.ask(SUMMARY_PROMPT.format(text=c)).strip() for c in chunks)
        summary = keep_dates(text, summary)
        path.write_text(f"[Summary of this conversation, made {datetime.now():%Y-%m-%d} to save space]\n"
                        f"{summary}\n", encoding="utf-8")
        self.index[path.name] = {**self.index.get(path.name, {}), "label": IMPORTANT, "summarized": True,
                                 "mtime": path.stat().st_mtime}
        self.save()
        _log(f"{path.name}: summarized, {len(text)} -> {path.stat().st_size} bytes")

    def fit(self, limit=None):
        """Deletes / summarizes until the logs are under the limit (or nothing more can be done)."""
        limit = _limit_bytes() if limit is None else limit
        while True:
            entries = [(p.name, self.index.get(p.name, {}).get("label"),
                        self.index.get(p.name, {}).get("summarized", False)) for p in self.logs()]
            step = next_step(entries, self.total(), limit)
            if step is None:
                return
            what, name = step
            if what == "delete":
                (self.folder / name).unlink(missing_ok=True)
                self.index.pop(name, None)
                self.save()
                _log(f"{name}: deleted (unimportant, logs over {limit / 2 ** 20:.0f} MB)")
            else:
                self.summarize(self.folder / name)

    def run_once(self, can_run=lambda: True):
        """One check: judge up to PER_CHECK finished conversations, then fit under the limit."""
        for path in self.to_judge()[:PER_CHECK]:
            if not can_run():
                return
            self.judge(path)
        if can_run():
            self.fit()


# --- the local model and the background loop ------------------------------------------------------------

def ask_local(prompt):
    """One question to the local model (Ollama), no tools; counts as Umay's own GPU work meanwhile."""
    local_llm.ensure_model()
    with local_llm.working(background=True):  # memory work: it doesn't hold up its own next check
        r = requests.post(local_llm.CHAT_URL, json={
            "model": local_llm.model(), "reasoning_effort": "none", "stream": False,
            "messages": [{"role": "user", "content": prompt}]}, timeout=300)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"] or ""


_started = {"on": False}


def start(folder, can_run):
    """Starts the background loop (main.py). can_run() -> False while the GPU isn't ours (a game) or the
    local model is answering."""
    if _started["on"] or not enabled():
        return
    _started["on"] = True

    def loop():
        while True:
            time.sleep(CHECK_SECONDS)
            try:
                if can_run():
                    Memory(folder, ask_local).run_once(can_run)
            except Exception as e:  # Ollama not running, a file in use: the next check tries again
                _log(f"error: {type(e).__name__}: {e}")
    threading.Thread(target=loop, name="memory", daemon=True).start()
