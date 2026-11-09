"""The day's plan and its tally: what the owner said they'd do today, and whether they did it (the owner,
2026-11-04: "bugün ne yapacağız" every day, a tally of what I said I must do, a reminder if it isn't done).

- The plan is made in a conversation: companion.py asks about the day the first time the owner is at the PC
  (with the morning greeting), and the model writes each to-do down with the plan_day tool ("add"; a time
  makes it a reminder at that time). The same tool ticks one off ("done"), undoes it, drops it, or lists the day.
- What wasn't done yesterday moves to today (carry_over), told in the day's first conversation.
- companion.py asks about the open ones during the day (due_reminders, check_due): at the PC aloud, away on
  Telegram with Done / Not yet buttons, then a WhatsApp call if there's no answer in an hour.

State: %LOCALAPPDATA%\\Umay\\day_plan.json.
"""
import difflib
import json
import os
import re
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

LATE_REMINDER_MINUTES = 90   # a time passed longer ago than this (Umay was off) isn't reminded any more
_lock = threading.Lock()


def _path():
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Umay" / "day_plan.json"


def load(path=None):
    try:
        data = json.loads((path or _path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data.setdefault("items", [])
    data.setdefault("next_id", 1)
    return data


def save(data, path=None):
    path = path or _path()
    cutoff = (date.today() - timedelta(days=30)).isoformat()
    data["items"] = [i for i in data["items"] if i["day"] >= cutoff]  # a month of tallies is kept
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


# --- pure rules (tests/test_day_plan.py) ---------------------------------------------------------------------

def parse_at(text):
    """"15:00", "15.30", "3pm", "3:30 pm", "15" -> "HH:MM"; "" -> None; ValueError if it isn't a time."""
    text = (text or "").strip().lower().replace(".", ":")
    if not text:
        return None
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text)
    if not m:
        raise ValueError(f"'{text}' isn't a time: give HH:MM")
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    if m.group(3) == "pm" and hour < 12:
        hour += 12
    if not m.group(3) and 1 <= hour <= 6 and not text.startswith("0"):
        hour += 12  # "call mom at 5": the afternoon, not 05:00 (nobody plans a to-do at 5 in the morning)
    if m.group(3) == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        raise ValueError(f"'{text}' isn't a time: give HH:MM")
    return f"{hour:02d}:{minute:02d}"


def split_tasks(text):
    """Several to-dos in one go: separated by ";" or new lines."""
    return [t.strip(" -•\t") for t in re.split(r"[;\n]", text or "") if t.strip(" -•\t")]


TRAILING_TIME = re.compile(r"\s+(?:at|@)\s+(\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm)?)\s*$", re.IGNORECASE)


def time_in_text(task):
    """"Call mom at 5" -> ("Call mom", "17:00"): the model put the time in the words (several to-dos in one call,
    seen 2026-11-04). (task, None) if there's none."""
    m = TRAILING_TIME.search(task)
    if not m:
        return task, None
    try:
        return task[:m.start()].strip(), parse_at(m.group(1))
    except ValueError:
        return task, None


def day_items(data, day):
    return sorted((i for i in data["items"] if i["day"] == day),
                  key=lambda i: (i["at"] is None, i["at"] or "", i["id"]))


def carry_over(data, today):
    """Yesterday's (and older) open to-dos move to today; returns them."""
    moved = [i for i in data["items"] if i["day"] < today and not i["done"] and not i.get("dropped")]
    for i in moved:
        i["day"], i["carried"], i["at"], i["reminded"], i["asked"] = today, True, None, False, 0
    return moved


COMMON = {"the", "and", "for", "with", "that", "this", "from", "your", "my", "bir", "ile", "için", "işi"}


def _words(text):
    return set(re.findall(r"\w{3,}", (text or "").lower())) - COMMON


def find(items, ref):
    """The item `ref` means: its number in the list (1-based), or the best match of its words. None if unclear."""
    ref = (ref or "").strip()
    if ref.isdigit():
        n = int(ref)
        return items[n - 1] if 1 <= n <= len(items) else None
    want = _words(ref)
    scored = []
    for i in items:
        have = _words(i["text"])
        overlap = len(want & have) / max(1, len(want))
        # letters alike only when nearly the same (a typo, "gym" for "the gym"): "walk the dog" isn't "call the bank"
        ratio = difflib.SequenceMatcher(None, ref.lower(), i["text"].lower()).ratio()
        scored.append((max(overlap, ratio if ratio >= 0.8 else 0.0), i))
    scored.sort(key=lambda s: -s[0])
    if not scored or scored[0][0] < 0.5 or (len(scored) > 1 and scored[1][0] == scored[0][0]):
        return None
    return scored[0][1]


def describe(items):
    """The day's tally, one line each: "1. [x] 15:00 Call the bank"."""
    if not items:
        return "Nothing planned for today yet."
    lines = []
    for n, i in enumerate(items, 1):
        mark = "x" if i["done"] else "-" if i.get("dropped") else " "
        when = f"{i['at']} " if i["at"] else ""
        extra = " (from an earlier day)" if i.get("carried") else ""
        lines.append(f"{n}. [{mark}] {when}{i['text']}{extra}")
    done = sum(1 for i in items if i["done"])
    open_ = sum(1 for i in items if not i["done"] and not i.get("dropped"))
    return "\n".join(lines) + f"\nDone {done}, open {open_}."


def mark_done(items, item_id):
    """A Telegram "Done" button (companion.py): the to-do with this id is done."""
    for i in items:
        if i["id"] == item_id:
            i["done"], i["done_at"] = True, datetime.now().isoformat(timespec="minutes")
            i.pop("dropped", None)
            return i
    return None


def open_items(items):
    return [i for i in items if not i["done"] and not i.get("dropped")]


def due_reminders(items, now):
    """Open to-dos whose time has come (and not reminded yet); a time long passed is only marked reminded."""
    due = []
    for i in open_items(items):
        if not i["at"] or i["reminded"]:
            continue
        at = datetime.combine(now.date(), datetime.strptime(i["at"], "%H:%M").time())
        if at <= now:
            i["reminded"] = True
            if now - at <= timedelta(minutes=LATE_REMINDER_MINUTES):
                due.append(i)
    return due


def check_due(items, now, last_check, every_hours):
    """Open to-dos to ask about now ("did you do it?"): those without a time, or whose time passed an hour ago;
    only every `every_hours` since the last check (last_check: a timestamp, 0 = never)."""
    if now.timestamp() - last_check < every_hours * 3600:
        return []
    out = []
    for i in open_items(items):
        if i["at"]:
            at = datetime.combine(now.date(), datetime.strptime(i["at"], "%H:%M").time())
            if now - at < timedelta(hours=1):
                continue
        out.append(i)
    return out


# --- the tool --------------------------------------------------------------------------------------------------

def plan_day(action: str, task: str = "", at: str = "") -> str:
    """The model's tool: add / done / not_done / remove / list."""
    action = (action or "list").strip().lower().replace(" ", "_")
    today = date.today().isoformat()
    with _lock:
        data = load()
        carry_over(data, today)
        items = day_items(data, today)
        try:
            if action == "add":
                tasks = split_tasks(task)
                if not tasks:
                    return "Say what to add: `task`."
                given = parse_at(at)
                timed = [time_in_text(t) for t in tasks]
                # "finish the report; call mom at 5; buy bread" with at 17:00 (the local model, 2026-11-04): the
                # time is the one to-do's that says it, not the others'
                if len(timed) > 1 and any(said for _, said in timed):
                    given = None
                added = []
                for text, said in timed:
                    when = said or given
                    same = next((i for i in items if i["text"].lower() == text.lower()), None)
                    if same:
                        same["at"] = when or same["at"]
                        continue
                    data["items"].append({"id": data["next_id"], "text": text, "day": today, "at": when,
                                          "done": False, "done_at": None, "reminded": False, "asked": 0})
                    data["next_id"] += 1
                    added.append(text + (f" (at {when})" if when else ""))
                save(data)
                items = day_items(data, today)
                head = f"Added: {', '.join(added)}" if added else "Already on the list"
                return f"{head}.\n{describe(items)}"
            if action == "list":
                save(data)
                return describe(items)
            item = find(items, task)
            if item is None:
                return f"Which one? Give its number or its words.\n{describe(items)}"
            if action in ("done", "did", "complete", "completed"):
                item["done"], item["done_at"] = True, datetime.now().isoformat(timespec="minutes")
                item.pop("dropped", None)
            elif action in ("not_done", "undo", "open"):
                item["done"], item["done_at"] = False, None
                item.pop("dropped", None)
            elif action in ("remove", "drop", "cancel", "delete"):
                item["dropped"] = True
            else:
                return "action must be add, done, not_done, remove or list."
            save(data)
            return f"'{item['text']}': {action.replace('_', ' ')}.\n{describe(day_items(data, today))}"
        except ValueError as e:
            return f"Couldn't do that: {e}"


def today_text():
    """The day's tally for a prompt (companion.py), after carrying yesterday's open ones over."""
    today = date.today().isoformat()
    with _lock:
        data = load()
        moved = carry_over(data, today)
        save(data)
        return describe(day_items(data, today)), [i["text"] for i in moved]


def update(fn):
    """fn(data, today_items) under the lock, then saved (companion.py's bookkeeping); returns fn's result."""
    today = date.today().isoformat()
    with _lock:
        data = load()
        carry_over(data, today)
        result = fn(data, day_items(data, today))
        save(data)
        return result


TOOLS = [{
    "type": "function",
    "function": {
        "name": "plan_day",
        "description": (
            "The user's plan and to-do tally for today. Whenever the user tells you what they will do or must do "
            "today (their plans, tasks, errands), call action 'add' for them (one call per to-do, or several in "
            "`task` separated by ';'), with `at` if they gave a time: you then remind them at that time yourself "
            "(no add_reminder needed). When they say they did one, call 'done'; 'not_done' undoes that; "
            "'remove' when it's no longer needed; 'list' shows the day's tally."),
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["add", "done", "not_done", "remove", "list"]},
            "task": {"type": "string", "description": "add: the to-do, short (e.g. 'Call the bank'). done / "
                                                      "not_done / remove: its words or its number in the list."},
            "at": {"type": "string", "description": "add only: HH:MM, if it has a time today."},
        }, "required": ["action"]},
    },
}]
TOOL_FUNCTIONS = {"plan_day": plan_day}
