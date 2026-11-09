"""Tools that manage Umay's alarms and reminders (the events in config.json).

The event system (check_event/alert) already shows a popup with a sound for
each event; these tools only edit config.json and ask the supervisor to reload
it. One-off reminders get a zero frequency, so they never re-fire after a
restart, and old ones Umay created are cleaned up when new ones are added.
"""
import json
import os
import threading
from datetime import datetime, timedelta

import config_loader
import pc_tools

DEFAULT_SOUND = "sounds/alert.mp3"
EXPIRED_REMINDER_DAYS = 1
REPEATS = {
    "none": {"year": 0, "month": 0, "day": 0, "hour": 0, "minute": 0},
    "hourly": {"year": 0, "month": 0, "day": 0, "hour": 1, "minute": 0},
    "daily": {"year": 0, "month": 0, "day": 1, "hour": 0, "minute": 0},
    "weekly": {"year": 0, "month": 0, "day": 7, "hour": 0, "minute": 0},
}
_EMPTY_PARTS = {"year": None, "month": None, "day": None, "hour": None, "minute": None}

_lock = threading.Lock()


class ToolError(Exception):
    pass


def _load():
    if not config_loader.CONFIG_PATH.exists():
        return {"events": []}
    with open(config_loader.CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _save(config):
    tmp = config_loader.CONFIG_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=4)
    os.replace(tmp, config_loader.CONFIG_PATH)
    config_loader.request_reload()


def _parts(dt):
    return {"year": dt.year, "month": dt.month, "day": dt.day, "hour": dt.hour, "minute": dt.minute}


def _start(event):
    """The event's start as a datetime (missing parts = now), like config_loader does."""
    return config_loader.DateParts(**event["start_date"]).to_datetime(datetime.now())


def _describe(event):
    start = event["start_date"]
    time = f"{start['hour']:02}:{start['minute']:02}" if start["hour"] is not None and start["minute"] is not None else "now"
    date = f"{start['year']}-{start['month']:02}-{start['day']:02} " if start["year"] is not None else ""
    if event["loop"]:
        freq = event["frequency"]
        every = ", ".join(f"{v} {k}" for k, v in freq.items() if v)
        when = f"every {every or '?'} starting {date}{time}"
    else:
        when = f"once at {date}{time}"
    state = "on" if event["active"] else "off"
    return f"- {event['name']} [{state}]: {when}"


def list_alarms() -> str:
    try:
        events = _load()["events"]
    except (OSError, ValueError, KeyError) as e:
        return f"Couldn't read the alarms: {e}"
    return "\n".join(_describe(e) for e in events) if events else "No alarms or reminders."


def _parse_time(at):
    now = datetime.now()
    text = at.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    try:
        clock = datetime.strptime(text, "%H:%M")
    except ValueError:
        raise ToolError("Give the time as HH:MM or YYYY-MM-DD HH:MM.")
    when = now.replace(hour=clock.hour, minute=clock.minute, second=0, microsecond=0)
    return when if when > now else when + timedelta(days=1)


def add_reminder(text: str, at: str = None, in_minutes: float = None, repeat: str = "none") -> str:
    try:
        if repeat not in REPEATS:
            raise ToolError(f"repeat must be one of: {', '.join(REPEATS)}.")
        if in_minutes:
            when = datetime.now() + timedelta(minutes=float(in_minutes))
        elif at:
            when = _parse_time(at)
        else:
            raise ToolError("Say when: `at` (HH:MM or YYYY-MM-DD HH:MM) or `in_minutes`.")
        # The event loop works in whole minutes and drops one-offs already in the past, so round up.
        if when.second or when.microsecond:
            when = when.replace(second=0, microsecond=0) + timedelta(minutes=1)

        with _lock:
            config = _load()
            events = config["events"]
            cutoff = datetime.now() - timedelta(days=EXPIRED_REMINDER_DAYS)
            events[:] = [e for e in events
                         if not (e.get("created_by") == "hannah" and not e["loop"] and _start(e) < cutoff)]
            names = {e["name"] for e in events}
            name, n = text.strip() or "Reminder", 2
            while name in names:
                name, n = f"{text.strip()} ({n})", n + 1
            events.append({
                "name": name,
                "active": True,
                "loop": repeat != "none",
                "start_date": _parts(when),
                "frequency": REPEATS[repeat],
                "end_date": dict(_EMPTY_PARTS),
                "sound": DEFAULT_SOUND,
                "created_by": "hannah",
            })
            _save(config)
        repeat_text = "" if repeat == "none" else f", repeating {repeat}"
        return f"Reminder '{name}' set for {when:%Y-%m-%d %H:%M}{repeat_text}."
    except (ToolError, OSError, ValueError, KeyError) as e:
        return f"Couldn't add the reminder: {e}"


def _find(events, name):
    query = pc_tools._normalize(name)
    matches = [e for e in events if query and query in pc_tools._normalize(e["name"])]
    exact = [e for e in matches if pc_tools._normalize(e["name"]) == query]
    matches = exact or matches
    if not matches:
        raise ToolError(f"No alarm matches '{name}'.\n{list_alarms()}")
    if len(matches) > 1:
        raise ToolError("Several alarms match; ask which one: " + ", ".join(e["name"] for e in matches))
    return matches[0]


def set_alarm_active(name: str, active: bool) -> str:
    try:
        with _lock:
            config = _load()
            event = _find(config["events"], name)
            event["active"] = bool(active)
            _save(config)
        return f"'{event['name']}' is now {'on' if active else 'off'}."
    except (ToolError, OSError, ValueError, KeyError) as e:
        return str(e)


def delete_alarm(name: str) -> str:
    try:
        with _lock:
            config = _load()
            event = _find(config["events"], name)
            config["events"].remove(event)
            _save(config)
        return f"Deleted '{event['name']}'."
    except (ToolError, OSError, ValueError, KeyError) as e:
        return str(e)


def _tool(name, description, properties, required):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


TOOLS = [
    _tool(
        "list_alarms",
        "Lists Umay's alarms and reminders (e.g. game event loops) with whether they're on and when they ring.",
        {}, [],
    ),
    _tool(
        "add_reminder",
        "Adds a reminder that rings on the PC (and on Telegram if the user is away). Give `at` or `in_minutes`, in "
        "the PC's local time. For other days, repeating reminders, or when they ask for an alarm. Something to "
        "do today (a to-do, an errand, 'I need to ... at 6'): use plan_day 'add' with `at` instead.",
        {
            "text": {"type": "string", "description": "What to remind about; shown as the reminder's name."},
            "at": {"type": "string", "description": "HH:MM (next occurrence) or YYYY-MM-DD HH:MM."},
            "in_minutes": {"type": "number", "description": "Minutes from now, instead of `at`."},
            "repeat": {"type": "string", "enum": list(REPEATS), "description": "Default none (one-off)."},
        },
        ["text"],
    ),
    _tool(
        "set_alarm_active",
        "Turns an existing alarm/reminder on or off (keeps it for later).",
        {"name": {"type": "string"}, "active": {"type": "boolean"}},
        ["name", "active"],
    ),
    _tool(
        "delete_alarm",
        "Deletes an alarm/reminder permanently.",
        {"name": {"type": "string"}},
        ["name"],
    ),
]

TOOL_FUNCTIONS = {
    "list_alarms": list_alarms,
    "add_reminder": add_reminder,
    "set_alarm_active": set_alarm_active,
    "delete_alarm": delete_alarm,
}
