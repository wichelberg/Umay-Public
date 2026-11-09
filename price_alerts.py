"""Price alarms the owner sets (plan 11): "BTC 80 bini geçerse haber ver", "tell me if Tesla drops below 200",
"USD/TL %1 oynarsa söyle". The companion already mentions big daily moves by itself (companion.MOVES); these are
the owner's own thresholds, for any symbol Yahoo Finance knows, on the markets panel or not.

Three kinds of rule, kept in %LOCALAPPDATA%\\Umay\\price_alerts.json:
- "above" / "below" a price: fires when the price crosses it, then the rule is gone (one-shot). A rule is armed
  only while its condition is not met, so one set when the price is already past it waits for it to come back
  and cross again (the answer says so). "reaches 80000" picks above or below from the price when it is set.
- "move": the day's change (since the last close) reaches that many percent either way; once a day, only while
  the market is open (a closed market's change is old news).

Every CHECK_SECONDS a thread reads the rules' quotes (hud_feeds.read_quotes) and tells the owner the way the
companion tells things (companion.tell, kind "markets"): aloud or written at the PC, Telegram when away
("important"). A move far bigger than expected is "urgent" (level_of): away, the companion calls on WhatsApp
(the owner, 2026-11-05). With the companion off it goes to Telegram (pc_tools.fallback_notify).

asked() catches the plain ways of setting one (English and Turkish) before the model, like markets_follow.py:
the small model mixed up the numbers ("80 bin") and the direction. The price_alert tool covers the rest and
lists / removes them. Log: %LOCALAPPDATA%\\Umay\\price_alerts.log.
"""
import json
import re
import threading
import time
from datetime import date, datetime

import requests

import hud_feeds
import markets_follow
import paths
from companion import CRASH_STEPS, MOVES, symbol_kind
from news_follow import ADDRESS, SPOKEN_TAIL

CHECK_SECONDS = 60
URGENT_TIMES = 2  # a move alarm at twice its limit is urgent (a call when away)
FILE = "price_alerts.json"
MAX_RULES = 30

TRIGGER = re.compile(r"\b(?:haber\s+ver|söyle|uyar|bildir|alarm|tell\s+me|let\s+me\s+know|alert\s+me|notify\s+me|"
                     r"warn\s+me|ping\s+me|message\s+me)\w*", re.I)
NUMBER = (r"\d{1,3}(?:[.,]\d{3})+(?![.,]?\d)(?:[.,]\d+)?|\d+(?:[.,]\d+)?")
PRICE = re.compile(rf"(?<![\w%])(?P<num>{NUMBER})\s*(?P<mult>bin\w*|k\b|thousand|milyon\w*|million|m\b)?"
                   r"(?!\s*%)(?:\s*(?:\$|dolar\w*|lira\w*|tl\b|usd\b|£|€))?", re.I)
PERCENT = re.compile(rf"(?:%\s*(?P<a>{NUMBER})|(?P<b>{NUMBER})\s*(?:%|percent\b|per\s+cent\b)|yüzde\s*(?P<c>{NUMBER}))",
                     re.I)
ABOVE = re.compile(r"\b(?:above|over|exceeds?|passes|tops|rises\s+above|goes\s+(?:above|over)|climbs\s+(?:above|over)|"
                   r"geç\w*|aş\w*|üstüne|üzerine|yukarı)\b", re.I)
BELOW = re.compile(r"\b(?:below|under|drops?\s+(?:below|under|to)|falls?\s+(?:below|under|to)|goes\s+(?:below|under)|"
                   r"altına|altında|aşağı\w*|düşer\w*|düşün\w*|düşük|iner\w*|inin\w*)\b", re.I)
# A time, not a price: "saat 9'u geçince haber ver" is a reminder.
TIME_WORDS = re.compile(r"\b(?:saat\w*|o'?clock|dakika\w*|minutes?|hours?|seconds?|saniye\w*|[ap]\.?m\b)"
                        r"|\d{1,2}:\d{2}", re.I)
CROSS = re.compile(r"\b(?:hits?|reach\w*|gets?\s+to|gelir\w*|gelin\w*|ulaş\w*|olur\w*|olun\w*|vurur\w*)\b", re.I)
MOVE = re.compile(r"\b(?:moves?|swings?|changes?|jumps?|oyna\w*|değiş\w*|hareket\w*|kıpırda\w*)\b", re.I)
LEAD = re.compile(r"^\W*(?:please\s+)?(?:tell\s+me|let\s+me\s+know|alert\s+me|notify\s+me|warn\s+me|ping\s+me|"
                  r"message\s+me)?\s*(?:if|when|once|whenever)?\s+|^\W*(?:eğer|şayet)\s+", re.I)
NAME_NOISE = re.compile(r"(?<!\S)(?:the|price\s+of|price|fiyat\w*|değer\w*|kur\w*|goes|rises|drops|falls|gets|is|"
                        r"bir|eğer)(?!\S)", re.I)
SUFFIX = re.compile(r"['’]\w+")

_lock = threading.Lock()


def _home():
    return paths.voice_home().parent  # %LOCALAPPDATA%\Umay


def _log(text):
    try:
        with open(_home() / "price_alerts.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}\n")
    except OSError:
        pass


# --- reading what was said (pure, tests/test_price_alerts.py) ---------------------------------------------------

def number(text):
    """"80,000" / "80.000" -> 80000; "34,5" / "1.5" -> decimal; None if it isn't a number."""
    text = str(text).strip()
    if re.fullmatch(r"\d{1,3}(?:([.,])\d{3})(?:\1\d{3})*", text):
        return float(re.sub(r"[.,]", "", text))
    m = re.fullmatch(r"\d{1,3}(?:([.,])\d{3})+([.,])(\d+)", text)
    if m and m.group(1) != m.group(2):
        return float(re.sub(r"[.,]", "", text[:m.start(2)]) + "." + m.group(3))
    try:
        return float(text.replace(",", "."))
    except ValueError:
        return None


def _scale(mult):
    mult = (mult or "").lower()
    if mult.startswith(("bin", "k", "thousand")):
        return 1_000
    if mult.startswith(("milyon", "million")) or mult == "m":
        return 1_000_000
    return 1


def _name(raw):
    raw = SUFFIX.sub("", LEAD.sub("", raw or ""))
    for pattern in (ABOVE, BELOW, CROSS, MOVE, TRIGGER):
        raw = pattern.sub(" ", raw)
    raw = NAME_NOISE.sub(" ", raw)
    name = re.sub(r"\s+", " ", raw).strip(" ,.:;!?-\"'")
    return name if name and len(name.split()) <= 4 else None


def parse(text):
    """{"name", "kind": above|below|cross|move, "value"} when the owner plainly sets a price alarm, else None."""
    text = ADDRESS.sub("", SPOKEN_TAIL.sub("", text or "")).strip()
    if not TRIGGER.search(text) or TIME_WORDS.search(text):
        return None
    pct = PERCENT.search(text)
    if pct and MOVE.search(text):
        value = number(pct.group("a") or pct.group("b") or pct.group("c"))
        name = _name(text[:pct.start()])
        return {"name": name, "kind": "move", "value": value} if name and value else None
    m = PRICE.search(text)
    if not m:
        return None
    value = number(m.group("num"))
    if value is None:
        return None
    value *= _scale(m.group("mult"))
    rest = text[m.end():]
    before = text[:m.start()]
    if BELOW.search(rest) or BELOW.search(before):
        kind = "below"
    elif ABOVE.search(rest) or ABOVE.search(before):
        kind = "above"
    elif CROSS.search(rest) or CROSS.search(before):
        kind = "cross"
    else:
        return None
    name = _name(before)
    return {"name": name, "kind": kind, "value": value} if name else None


def asked(text):
    """The alarm the owner plainly sets, for ai_assistant's shortcut; None otherwise."""
    return parse(text)


def shortcut(alarm):
    """Sets what asked() found; None when the name isn't a market Yahoo knows (then the model has a go: the
    sentence may not have been about prices at all)."""
    try:
        if markets_follow.resolve(alarm["name"]) is None:
            return None
    except (requests.RequestException, ValueError):
        pass  # offline: add() says so
    return add(alarm["name"], alarm["kind"], alarm["value"])


# --- rules ------------------------------------------------------------------------------------------------------

def _path():
    return _home() / FILE


def load():
    try:
        rules = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [r for r in rules if isinstance(r, dict) and r.get("symbol") and r.get("kind") in ("above", "below", "move")]


def save(rules):
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rules, ensure_ascii=False, indent=1), encoding="utf-8")


def met(rule, quote):
    """Whether the rule's condition holds for this quote (pure)."""
    if rule["kind"] == "above":
        return quote["price"] >= rule["value"]
    if rule["kind"] == "below":
        return quote["price"] <= rule["value"]
    return quote.get("pct") is not None and abs(quote["pct"]) >= rule["value"]


def describe(rule):
    v = rule["value"]
    if rule["kind"] == "move":
        return f"{rule['name']} ({rule['symbol']}) moves {v:g}% in a day"
    return f"{rule['name']} ({rule['symbol']}) goes {rule['kind']} {hud_feeds.price_text(v)}"


def level_of(rule, quote):
    """"urgent" for a move far bigger than expected (the owner, 2026-11-05: "then she may even call me on
    WhatsApp; you pick how big"): a move alarm at URGENT_TIMES its limit, or any alarm's market moving
    CRASH_STEPS of companion's steps in a day (a stock or crypto 15%, an index 6%, a currency 4.5%, a commodity
    7.5%), either way. Away, the companion calls for "urgent" (not in the quiet hours, at most twice a day);
    otherwise "important" (Telegram when away)."""
    pct = quote.get("pct")
    if pct is None:
        return "important"
    if rule["kind"] == "move" and abs(pct) >= URGENT_TIMES * rule["value"]:
        return "urgent"
    if abs(pct) >= CRASH_STEPS * MOVES[symbol_kind(rule["symbol"])]:
        return "urgent"
    return "important"


def check(rules, quotes, today):
    """([(message, level)], rules left) for one round (pure): crossing rules fire once and go, move rules once a
    day."""
    by_symbol = {q["symbol"]: q for q in quotes}
    out, left = [], []
    for rule in rules:
        q = by_symbol.get(rule["symbol"])
        if q is None or q.get("price") is None:
            left.append(rule)
            continue
        now = met(rule, q)
        if rule["kind"] == "move":
            if now and q.get("live") and rule.get("fired") != today:
                rule = {**rule, "fired": today}
                way = "up" if q["pct"] > 0 else "down"
                out.append((f"Price alarm: {rule['name']} is {way} {abs(q['pct']):.1f}% today "
                            f"(now {hud_feeds.price_text(q['price'])}); your limit was {rule['value']:g}%.",
                            level_of(rule, q)))
            left.append(rule)
            continue
        armed = rule.get("armed", True)
        if now and armed:
            day = f", {q['pct']:+.1f}% today" if q.get("pct") is not None else ""
            out.append((f"Price alarm: {rule['name']} went {rule['kind']} {hud_feeds.price_text(rule['value'])} "
                        f"(now {hud_feeds.price_text(q['price'])}{day}). That alarm is done.", level_of(rule, q)))
            continue  # one-shot
        if not now and not armed:
            rule = {**rule, "armed": True}  # set while already past: back on this side, so a cross counts now
        left.append(rule)
    return out, left


def _quote(symbol):
    quotes = hud_feeds.read_quotes([symbol])
    return quotes[0] if quotes else None


def add(name, kind, value):
    """Sets an alarm; the answer for the owner (English, like markets_follow)."""
    name = str(name or "").strip(" ,.\"'")
    if not name:
        return "Which market should the alarm watch?"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "Give the alarm's price or percent as a number."
    if value <= 0:
        return "Give the alarm's price or percent as a positive number."
    if kind not in ("above", "below", "cross", "move"):
        return "Say whether it's above or below a price, or a move of some percent in a day."
    try:
        found = markets_follow.resolve(name)
        quote = _quote(found[0]) if found else None
    except (requests.RequestException, ValueError):
        return "I couldn't reach Yahoo Finance to look that up. Try again in a bit."
    if found is None or quote is None:
        return f"Yahoo Finance doesn't know \"{name}\". Give me its symbol (e.g. BTC-USD, USDTRY=X, THYAO.IS)."
    symbol, shown = found
    price = quote["price"]
    if kind == "cross":
        kind = "above" if value > price else "below"
    rule = {"id": 0, "symbol": symbol, "name": shown, "kind": kind, "value": value,
            "set": date.today().isoformat()}
    with _lock:
        rules = load()
        if len(rules) >= MAX_RULES:
            return f"There are already {MAX_RULES} price alarms; remove some first."
        rule["id"] = max((r.get("id", 0) for r in rules), default=0) + 1
        now = met(rule, quote)
        if kind != "move":
            rule["armed"] = not now
        save(rules + [rule])
    _log(f"set #{rule['id']}: {describe(rule)} (now {price})")
    answer = f"Alarm #{rule['id']} set: I'll tell you when {describe(rule)}. It's {hud_feeds.price_text(price)} now"
    if kind == "move":
        answer += f", {quote['pct']:+.1f}% today." if quote.get("pct") is not None else "."
    elif now:
        answer += f", already {kind} that: I'll tell you when it crosses it again."
    else:
        answer += "."
    return answer


def listing():
    rules = load()
    if not rules:
        return "No price alarms are set."
    return "Price alarms: " + "; ".join(f"#{r['id']} {describe(r)}" for r in rules) + "."


def remove(which):
    """By number ("2", "#2"), by name ("bitcoin": all of its alarms) or "all"."""
    which = str(which or "").strip().lstrip("#")
    with _lock:
        rules = load()
        if which.lower() in ("all", "hepsi", "tümü", "hepsini"):
            gone = rules
        elif which.isdigit():
            gone = [r for r in rules if r.get("id") == int(which)]
        else:
            symbol = markets_follow.known(which)
            key = markets_follow._key(which)
            gone = [r for r in rules if r["symbol"] == symbol or key and key in markets_follow._key(r["name"])
                    or which.upper() in (r["symbol"], r["symbol"].split(".")[0].split("-")[0])]
        if not gone:
            return f"No price alarm matches \"{which}\". {listing()}"
        save([r for r in rules if r not in gone])
    _log("removed: " + "; ".join(describe(r) for r in gone))
    return "Removed: " + "; ".join(f"#{r['id']} {describe(r)}" for r in gone) + "."


# --- the watch --------------------------------------------------------------------------------------------------

def check_now(tell):
    """One round: reads the quotes and tells what fired. tell(text, level)."""
    with _lock:
        rules = load()
        if not rules:
            return []
        quotes = hud_feeds.read_quotes(sorted({r["symbol"] for r in rules}))
        messages, left = check(rules, quotes, date.today().isoformat())
        if left != rules:
            save(left)
    for text, level in messages:
        _log(f"{level}: {text}")
        tell(text, level)
    return messages


def start(tell):
    """Checks every CHECK_SECONDS in the background (main.py)."""
    def run():
        while True:
            time.sleep(CHECK_SECONDS)
            try:
                check_now(tell)
            except Exception as e:  # offline, Yahoo busy: the next round tries again
                _log(f"check failed: {type(e).__name__}: {e}")
    threading.Thread(target=run, name="price-alerts", daemon=True).start()


# --- the tool ---------------------------------------------------------------------------------------------------

def price_alert(action: str, name: str = "", condition: str = "", value: float = None, which: str = "") -> str:
    action = (action or "").strip().lower()
    if action in ("add", "set"):
        return add(name, (condition or "").strip().lower(), value)
    if action == "list":
        return listing()
    if action in ("remove", "delete"):
        return remove(which or name)
    return "Unknown action. Use add, list or remove."


TOOLS = [{
    "type": "function",
    "function": {
        "name": "price_alert",
        "description": (
            "The user's price alarms for a stock, index, currency, commodity or crypto: 'add' one (condition "
            "'above' / 'below' a price, 'cross' when they say 'reaches', or 'move' = the day's change reaches "
            "`value` percent either way), 'list' them, 'remove' one by its number or name ('all' for every one). "
            "She tells the user herself when one fires (aloud, written, or on Telegram when away)."),
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["add", "list", "remove"]},
            "name": {"type": "string", "description": "A market name or symbol (\"Bitcoin\", \"USD/TRY\", \"AAPL\")."},
            "condition": {"type": "string", "enum": ["above", "below", "cross", "move"]},
            "value": {"type": "number", "description": "The price (80000, not '80k'), or the percent for 'move'."},
            "which": {"type": "string", "description": "For 'remove': the alarm's number, a name, or 'all'."},
        }, "required": ["action"]},
    },
}]
TOOL_FUNCTIONS = {"price_alert": price_alert}
