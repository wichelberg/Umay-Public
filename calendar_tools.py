"""Puts the owner's plans into their phone's calendar (an item of the old Zyraena development plan).

The owner's Microsoft account is in the new Outlook (no COM) and the phone's Outlook app; this PC's
classic Outlook has no account (2026-10-25), so nothing can be written into the calendar directly. Instead Umay makes one .ics file with all the
events (each with its own phone alarm) and sends it on Telegram: one tap on the phone adds them to
Outlook. Re-sending the same event (same title and start) updates it instead of adding a copy.
Each event also gets a PC reminder (reminder_tools), which rings and reaches Telegram (alarm_ring).

Only for when the owner asks to put things into their plans / calendar; a plain "set an alarm" or
"remind me" stays on the PC (add_reminder).
"""
import hashlib
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import paths
import reminder_tools

DEFAULT_MINUTES = 15
REPEATS = ("none", "daily", "weekly")
# Development plan whose items the owner refers to by number ("6. maddedeki alarmları planlarıma ekle").
DEV_PLAN_PATH = Path(os.environ.get("UMAY_DEV_PLAN", "").strip() or (
    Path.home() / "Documents/Projects/Umay/development/planned.txt"))
DONE_PLAN_NAME = "done.txt"  # finished items move here, next to the plan, keeping their numbers
ITEM_RULE = re.compile(r"^-{20,}[ \t]*$", re.MULTILINE)  # the line above and below each item's header


class ToolError(Exception):
    pass


def _telegram():
    import telegram_listener  # lazy: it pulls in the whole assistant
    return telegram_listener


def calendar_dir() -> Path:
    return paths.app_dir() / "calendar"


# --- .ics ------------------------------------------------------------------------------------

def _utc(dt):
    """A naive local time -> iCalendar UTC text (the phone shows it in its own time zone)."""
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _escape(text):
    return (str(text).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n"))


def _fold(line):
    """iCalendar lines are at most 75 octets; longer ones go on with a leading space."""
    out, current = [], b""
    for ch in line:
        encoded = ch.encode("utf-8")
        if len(current) + len(encoded) > (75 if not out else 74):
            out.append(current)
            current = b""
        current += encoded
    out.append(current)
    return "\r\n ".join(part.decode("utf-8") for part in out)


def _uid(title, start):
    digest = hashlib.sha1(f"{title.strip().lower()}|{start:%Y-%m-%d %H:%M}".encode("utf-8")).hexdigest()
    return f"{digest[:16]}@umay"


def build_ics(events, now=None):
    """events: dicts from _parse_event. Returns the .ics text."""
    stamp = _utc(now or datetime.now())
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Umay//Plans//TR", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH"]
    for e in events:
        lines += ["BEGIN:VEVENT", f"UID:{_uid(e['title'], e['start'])}", f"DTSTAMP:{stamp}",
                  f"DTSTART:{_utc(e['start'])}", f"DTEND:{_utc(e['start'] + timedelta(minutes=e['minutes']))}",
                  f"SUMMARY:{_escape(e['title'])}"]
        if e["notes"]:
            lines.append(f"DESCRIPTION:{_escape(e['notes'])}")
        if e["location"]:
            lines.append(f"LOCATION:{_escape(e['location'])}")
        if e["repeat"] != "none":
            rule = f"RRULE:FREQ={e['repeat'].upper()}"
            if e["until"]:
                rule += f";UNTIL={_utc(e['until'])}"
            lines.append(rule)
        lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_escape(e['title'])}",
                  f"TRIGGER:-PT{e['alert_before']}M", "END:VALARM", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


# --- the tool --------------------------------------------------------------------------------

def _parse_time(text, what):
    try:
        return datetime.strptime(str(text).strip().replace("T", " "), "%Y-%m-%d %H:%M")
    except ValueError:
        raise ToolError(f"{what} must be a full date and time, YYYY-MM-DD HH:MM (got '{text}'). "
                        "If the owner didn't say it, ask; don't guess.")


def _parse_event(raw):
    title = str(raw.get("title") or "").strip()
    if not title:
        raise ToolError("Every event needs a title.")
    repeat = raw.get("repeat") or "none"
    if repeat not in REPEATS:
        raise ToolError(f"repeat must be one of: {', '.join(REPEATS)}.")
    until = raw.get("repeat_until")
    until = _parse_time(f"{until} 23:59" if len(str(until).strip()) == 10 else until, "repeat_until") if until else None
    minutes = int(raw.get("minutes") or DEFAULT_MINUTES)
    alert_before = int(raw.get("alert_minutes_before") or 0)
    if minutes <= 0 or alert_before < 0:
        raise ToolError("minutes must be > 0 and alert_minutes_before >= 0.")
    return {"title": title, "start": _parse_time(raw.get("start"), f"start of '{title}'"), "minutes": minutes,
            "alert_before": alert_before, "notes": str(raw.get("notes") or "").strip(),
            "location": str(raw.get("location") or "").strip(), "repeat": repeat, "until": until,
            "pc": raw.get("pc_reminder", True) is not False}


def _pc_repeat(event):
    """The PC reminder's repeat: add_reminder repeats forever, so one with an end date stays one-off."""
    return event["repeat"] if event["repeat"] != "none" and event["until"] is None else "none"


def add_to_calendar(events: list) -> str:
    try:
        if not events:
            raise ToolError("Give at least one event.")
        parsed = [_parse_event(e) for e in events]
        now = datetime.now()
        past = [e for e in parsed if e["repeat"] == "none" and e["start"] - timedelta(minutes=e["alert_before"]) <= now]
        parsed = [e for e in parsed if e not in past]
        if not parsed:
            raise ToolError("All of these are already in the past: " + ", ".join(f"{e['title']} ({e['start']:%Y-%m-%d %H:%M})" for e in past))

        ics = build_ics(parsed, now)
        calendar_dir().mkdir(parents=True, exist_ok=True)
        path = calendar_dir() / f"plan_{now:%Y%m%d_%H%M%S}.ics"
        path.write_text(ics, encoding="utf-8", newline="")

        lines = []
        for e in parsed:
            ring_at = e["start"] - timedelta(minutes=e["alert_before"])
            line = f"- {e['title']}: {e['start']:%Y-%m-%d %H:%M}" + (f", {e['minutes']} min" if e["minutes"] != DEFAULT_MINUTES else "")
            if e["repeat"] != "none":
                line += f", {e['repeat']}" + (f" until {e['until']:%Y-%m-%d}" if e["until"] else "")
            line += f"; phone alarm {e['alert_before']} min before" if e["alert_before"] else "; phone alarm at the start"
            if e["pc"]:
                repeat = _pc_repeat(e)
                if ring_at > now or repeat != "none":  # a repeating one starts at its next time
                    line += "; PC: " + reminder_tools.add_reminder(e["title"], at=f"{ring_at:%Y-%m-%d %H:%M}", repeat=repeat)
                else:
                    line += "; PC: none (its first time is past; the phone repeats it)"
            lines.append(line)

        caption = (f"📅 {len(parsed)} etkinlik. Dosyaya dokun, Outlook ile aç ve takvime ekle. "
                   "Aynı etkinliği tekrar eklersen kopya olmaz, güncellenir.")
        sent = _telegram().send_document(ics.encode("utf-8"), path.name, caption)
        result = [f"Calendar file: {path}",
                  "Sent to Telegram: the owner taps it on the phone and adds it to Outlook (it's not in the calendar until then)."
                  if sent else "Telegram isn't connected, so it wasn't sent; the owner can open the file and import it in Outlook."]
        result += lines
        if past:
            result.append("Skipped (already past): " + ", ".join(f"{e['title']} ({e['start']:%Y-%m-%d %H:%M})" for e in past))
        return "\n".join(result)
    except (ToolError, OSError, ValueError, TypeError) as e:
        return f"Couldn't add to the calendar: {e}"


def read_plan_item(number: int) -> str:
    """The text of one numbered item of the development plan (open items first, then finished ones)."""
    try:
        texts = [DEV_PLAN_PATH.read_text(encoding="utf-8")]
    except OSError as e:
        return f"Couldn't read the development plan ({DEV_PLAN_PATH}): {e}"
    done = DEV_PLAN_PATH.with_name(DONE_PLAN_NAME)
    if done.exists():
        try:
            texts.append(done.read_text(encoding="utf-8"))
        except OSError:
            pass
    for text in texts:
        blocks = ITEM_RULE.split(text)
        for i in range(1, len(blocks) - 1, 2):  # rule, header, rule, body, rule, header, ...
            header = blocks[i].strip()
            if header.startswith(f"{int(number)})"):
                return f"{header}\n{blocks[i + 1].strip()}"
    return f"There's no item {number} in the development plan."


def _tool(name, description, properties, required):
    return {"type": "function",
            "function": {"name": name, "description": description,
                         "parameters": {"type": "object", "properties": properties, "required": required}}}


_EVENT = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "start": {"type": "string", "description": "YYYY-MM-DD HH:MM, the PC's local time. Never guessed."},
        "minutes": {"type": "integer", "description": f"Length; default {DEFAULT_MINUTES} (an alarm-like point in time)."},
        "alert_minutes_before": {"type": "integer",
                                 "description": "Phone alarm and PC reminder this many minutes before the start; "
                                                "default 0 (at the start). E.g. 30 for a meeting."},
        "notes": {"type": "string"},
        "location": {"type": "string"},
        "repeat": {"type": "string", "enum": list(REPEATS), "description": "Default none."},
        "repeat_until": {"type": "string", "description": "YYYY-MM-DD, last day of a repeating event (optional)."},
        "pc_reminder": {"type": "boolean", "description": "Also ring on the PC; default true."},
    },
    "required": ["title", "start"],
}

TOOLS = [
    _tool(
        "add_to_calendar",
        "Puts events into the owner's phone calendar (Outlook): sends a .ics file on Telegram for the owner to tap, "
        "and sets a PC reminder for each. ONLY when the owner asks to add things to their plans / calendar / "
        "Outlook / phone (e.g. '6. maddedeki alarmları planlarıma ekle'). A plain 'alarm kur' or 'hatırlatıcı "
        "kur' is add_reminder, never this. Every start needs a date and time the owner gave or that's written in "
        "the plan; if one is missing or unclear, ask instead of guessing. Afterwards say what was added and that "
        "it's in the calendar once they tap the file. It can't remove events: tell the owner to delete them on "
        "the phone.",
        {"events": {"type": "array", "items": _EVENT}},
        ["events"],
    ),
    _tool(
        "read_plan_item",
        "Reads one numbered item of Umay's development plan (development/planned.txt, or done.txt "
        "once the item is done). Use it when the owner "
        "refers to an item by number, e.g. '6. maddedeki alarmları planlarıma ekle' -> read item 6, then "
        "add_to_calendar with the events written there.",
        {"number": {"type": "integer"}},
        ["number"],
    ),
]

TOOL_FUNCTIONS = {
    "add_to_calendar": add_to_calendar,
    "read_plan_item": read_plan_item,
}
