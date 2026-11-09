"""Umay on her own initiative: she greets the owner in the morning, brings up news she thinks matters, notices
what changes on the PC's gadgets (the markets, the weather, the battery, the holidays) and starts the talk
herself. How she reaches the owner depends on the room and on them (route()):

- At the PC, the room quiet: aloud, as the start of a spoken conversation (voice_session.open_with), so the owner
  can simply answer; the line shows beside the orb too. At night as well (the owner, 2026-11-04: at the PC,
  everything she shows is said aloud).
- At the PC but she shouldn't talk: the room is loud (wake.room_level over UMAY_NOISY_LEVEL), another program
  uses the microphone (a meeting: Meet in the browser, Teams, Zoom; not Discord, UMAY_MIC_OK), the text box is
  open, or her voice can't run: in writing, in the text box (ai_assistant.reach_out), which opens without taking
  the keyboard. companion.log says why each time ("writing, not speaking: ..."). Right after Umay starts her
  voice and the room's level aren't ready yet: it waits for them (STARTUP_WAIT_SECONDS) instead of writing.
- During a game, a spoken conversation or a call: it waits (an urgent one is written at once).
- Away (no input for UMAY_AWAY_MINUTES, 5): an urgent one is a WhatsApp call (calls.call_owner): a ringing
  reminder (alarm_ring.py), a story about whom the owner follows (not a section's headline) she rates UMAY_NEWS_CALL_SCORE (10), a market's crash
  (CRASH_STEPS); news and markets not in quiet hours, at most CALLS_A_DAY each. The other important ones go to
  Telegram only, the rest is dropped: nothing shows on the PC while they are away, nor is it told again when they are
  back (the owner, 2026-11-04).

The day's opening: the first time the owner is at the PC between MORNING_FROM and MORNING_UNTIL, the camera
looks (face.py: the owner's face once it is learned, any face before); seen, she greets them (good morning /
afternoon / evening), says what today holds (the weather, the reminders, a holiday, a headline, the to-dos left
from yesterday) and asks what they'll do today: what they answer is written down as the day's plan (day_plan.py,
the plan_day tool). Not seen (nobody, or someone else): she looks again every LOOK_AGAIN_MINUTES. A camera that
can't look doesn't stop it. Only a loud room makes it written. It counts as done once said (not when started).

The day's plan (_plan): a to-do with a time is reminded at that time; every UMAY_PLAN_CHECK_HOURS (3) outside the
quiet hours she asks about the open ones ("did you do it?"; the model ticks them off). Away, that question goes
to Telegram with Done / Not yet buttons; no answer in PLAN_CALL_AFTER_MINUTES (60) and still away: a WhatsApp
call about it (not in the quiet hours, at most CALLS_A_DAY["plan"]).

What she says is her model's own words (ai_assistant.ask_flash with her persona; the local model is free), made
from plain facts; if the model can't answer, the facts themselves. News is rated by the same model, in batches.
Nothing here starts while a game runs except what is urgent.

Logs: %LOCALAPPDATA%\\Umay\\companion.log (every decision); state (what was told, the day's greeting) in
companion.json beside it. .env: UMAY_COMPANION (on; off = none of this), UMAY_AWAY_MINUTES, UMAY_QUIET_HOURS,
UMAY_NOISY_LEVEL, UMAY_NEWS_SCORE, UMAY_NEWS_CALL_SCORE, UMAY_BREAK_MINUTES, UMAY_MIC_OK, UMAY_PLAN_CHECK_HOURS.
"""
import json
import os
import re
import threading
import time
import traceback
import winreg
from datetime import date, datetime, timedelta
from pathlib import Path

import paths

TICK_SECONDS = 15
NEWS_MINUTES = 10
MARKETS_MINUTES = 5
WEATHER_MINUTES = 20
GAP_MINUTES = 4            # between two things she brings up herself (urgent ones don't wait)
NEWS_GAP_MINUTES = 20      # between two news stories
NEWS_BATCH = 15            # new headlines rated at once (the newest)
CHAT_WAIT_MINUTES = 20     # small talk that couldn't be said in this long is dropped
MORNING_FROM, MORNING_UNTIL = 5, 22   # the day's opening (greeting + "what will we do today?")
PLAN_CALL_AFTER_MINUTES = 60          # away: a "did you do it?" on Telegram unanswered this long -> a call
PLAN_PHONE_ITEMS = 3                  # at most this many "did you do it?" messages at once
# With the owner's answer only, for the model (the local one forgets the tool otherwise).
PLAN_HINT = ("\n\n(If they name things they will do or must do today, write ALL of them down now with plan_day "
             "action 'add': every task they named, short, separated by ';' in `task`. Give `at` only for a clock "
             "time they actually said; never make one up. If they name no concrete task (\"not sure yet\", "
             "\"later\", \"nothing\"), add nothing: just answer.)")
DONE_HINT = ("\n\n(If they say they did a to-do from today's plan, mark it with plan_day action 'done'; if it's "
             "no longer needed, 'remove'. New to-dos: 'add'.)")
LOOK_AGAIN_MINUTES = 3
VOICE_WAIT_SECONDS = 120   # the morning greeting waits this long for her voice (it loads after a start)
STARTUP_WAIT_SECONDS = 180 # after a start, what she brings up waits this long for her voice rather than being written
BACK_SECONDS = 10          # input this recent after being away: the owner is back
RAIN_CHANCE = 70           # % in the next two hours, while it's dry now
LOW_BATTERY, CRITICAL_BATTERY = 20, 10
# A market's move today worth a word, by kind, in %; twice that again, and so on. A fall of CRASH_STEPS steps
# (a stock or crypto -15%, an index -6%) is urgent: away, she calls (the owner asked, 2026-11-04), at most
# MARKET_CALLS_A_DAY times and not in the quiet hours (then it goes to Telegram).
MOVES = {"index": 2.0, "fx": 1.5, "stock": 5.0, "crypto": 5.0, "commodity": 2.5}
CRASH_STEPS = 3
CALLS_A_DAY = {"news": 2, "markets": 2, "plan": 2}
DEFAULTS = {"UMAY_AWAY_MINUTES": "5", "UMAY_QUIET_HOURS": "23-08", "UMAY_NOISY_LEVEL": "0.004",
            "UMAY_NEWS_SCORE": "9", "UMAY_NEWS_CALL_SCORE": "10", "UMAY_BREAK_MINUTES": "120",
            "UMAY_MIC_OK": "discord", "UMAY_PLAN_CHECK_HOURS": "3"}
LEVELS = ("chat", "notable", "important", "urgent")
OURS_ON_MIC = ("umay",)  # the wake listener is her own microphone use (her calls make her busy anyway)
MIC_KEY = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"


def _setting(name, kind=float):
    raw = os.environ.get(name, "").strip() or DEFAULTS[name]
    try:
        return kind(raw)
    except ValueError:
        return kind(DEFAULTS[name])


def enabled():
    return os.environ.get("UMAY_COMPANION", "").strip().lower() != "off"


def _home():
    return paths.voice_home().parent  # %LOCALAPPDATA%\Umay


def _log(text):
    try:
        with open(_home() / "companion.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}\n")
    except OSError:
        pass


# --- pure rules (tests/test_companion.py) -------------------------------------------------------------------

def quiet_hours(hour, spec):
    """True in the quiet hours "23-08" (wrapping past midnight) or "13-14"; a bad spec: never."""
    m = re.fullmatch(r"\s*(\d{1,2})\s*-\s*(\d{1,2})\s*", spec or "")
    if not m:
        return False
    start, end = int(m.group(1)) % 24, int(m.group(2)) % 24
    return start <= hour < end if start <= end else hour >= start or hour < end


def route(level, kind, s):
    """How something reaches the owner now: "call", "hold" (away: Telegram only), "wait" (try again soon),
    "speak" or "write". s: away, gaming, busy (a conversation or call going), quiet (hours), noisy (or the
    microphone in use elsewhere), can_speak, box (the text box is open: she writes there), recent (she brought
    something up a moment ago), starting (Umay just started and her voice / the room's level isn't ready yet:
    it waits for them rather than writing, 2026-11-04)."""
    urgent = level == "urgent"
    if s["away"]:
        return "call" if urgent and not (s["quiet"] and kind in ("news", "markets")) else "hold"
    if s["busy"]:
        return "wait"
    if s["gaming"]:
        return "write" if urgent else "wait"
    if s.get("starting") and not s["can_speak"]:
        return "wait"
    if s["recent"] and not urgent:
        return "wait"
    # Not the quiet hours: at the PC she speaks at night too (the owner, 2026-11-04); they only keep her from
    # calling about news or markets while the owner is away.
    if s["noisy"] or s.get("box") or not s["can_speak"]:
        return "write"
    return "speak"


def why_written(s):
    """Why route() writes instead of speaking, for the log."""
    reasons = [name for name, on in (("noisy room", s.get("noisy") and not s.get("mic")),
                                     ("text box open", s.get("box")), ("voice not ready", not s.get("can_speak")))
               if on]
    if s.get("mic"):
        reasons.append("mic used by " + ", ".join(s["mic"]))
    return ", ".join(reasons) or "?"


def symbol_kind(symbol):
    s = symbol.upper()
    if s.endswith("=X"):
        return "fx"
    if s.endswith("=F"):
        return "commodity"
    if s.endswith("-USD") or s.endswith("-EUR"):
        return "crypto"
    if s.startswith("^") or (s.endswith(".IS") and s.startswith("XU")):
        return "index"
    return "stock"


def market_moves(quotes, told, today):
    """[(fact, level)] for markets that moved past a new step today (MOVES by kind, then twice, three times...);
    `told` ({symbol: [day, steps]}) is updated. Only live markets: a closed one's move is old news."""
    out = []
    for q in quotes:
        if not q.get("live") or q.get("pct") is None:
            continue
        step = MOVES[symbol_kind(q["symbol"])]
        steps = int(abs(q["pct"]) // step) * (1 if q["pct"] > 0 else -1)
        day, before = told.get(q["symbol"], [None, 0])
        if day != today:
            before = 0
        if steps == 0 or (abs(steps) <= abs(before) and (steps > 0) == (before > 0)):
            continue
        told[q["symbol"]] = [today, steps]
        way = "up" if q["pct"] > 0 else "down"
        level = "urgent" if steps <= -CRASH_STEPS else "important" if abs(steps) >= 2 else "notable"
        out.append((f"{q['name']} is {way} {abs(q['pct']):.1f}% today (now {q['price']:,.2f})", level))
    return out


def parse_scores(text, n):
    """{index: score} from the model's answer ([{"i": 1, "score": 8}, ...], or "1: 8" lines)."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    scores = {}
    for i, score in re.findall(r'"i"\s*:\s*(\d+)\s*,\s*"score"\s*:\s*(\d+)', text) or \
            re.findall(r"^\s*(\d+)\s*[:.)-]\s*(\d+)\b", text, flags=re.M):
        i, score = int(i), int(score)
        if 1 <= i <= n and 0 <= score <= 10:
            scores[i] = score
    return scores


def rain_soon(weather):
    """"Rain is likely around 15:00 (80%)" when it is dry now and rain is likely within two hours, else None."""
    hours = (weather or {}).get("hourly") or []
    if len(hours) < 3 or (hours[0].get("rain") or 0) >= 40:
        return None
    for h in hours[1:3]:
        if (h.get("rain") or 0) >= RAIN_CHANCE:
            return f"Rain is likely around {h['hour']}:00 ({h['rain']}% chance) where you are"
    return None


def clean(text):
    """The model's line without its thinking, quotes or a "Umay:" in front."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    text = re.sub(r"^(umay|assistant)\s*:\s*", "", text, flags=re.I).strip()
    return text.strip('"“”').strip()


def mic_ok():
    """Programs whose microphone use doesn't keep her quiet (UMAY_MIC_OK, comma-separated parts of their path):
    Discord by default (the owner, 2026-11-04: she may talk while Discord is open, not during a Meet)."""
    return tuple(p.strip().lower() for p in _setting("UMAY_MIC_OK", str).split(",") if p.strip())


def mic_users(ours=None):
    """Programs Windows says are using the microphone now (its privacy records), hers and mic_ok()'s left out.
    A browser on the microphone (Meet, Teams on the web) counts: it's most likely a meeting."""
    ours = OURS_ON_MIC + mic_ok() if ours is None else ours
    found = []

    def walk(path):
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path)
        except OSError:
            return
        with key:
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(key, i)
                except OSError:
                    break
                i += 1
                if name == "NonPackaged":
                    walk(path + "\\" + name)
                    continue
                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path + "\\" + name) as sub:
                        start = winreg.QueryValueEx(sub, "LastUsedTimeStart")[0]
                        stop = winreg.QueryValueEx(sub, "LastUsedTimeStop")[0]
                except OSError:
                    continue
                if start and not stop and not any(o in name.lower() for o in ours):
                    found.append(name.replace("#", "\\"))

    walk(MIC_KEY)
    return found


# --- the companion ------------------------------------------------------------------------------------------

class Companion:
    def __init__(self, deps):
        """deps: ask(prompt) -> text (her model, no tools), persona() -> text, speak(text, hint) -> bool (a spoken
        conversation started), write(text, hint), call(text[, hint]) -> str, notify(text) (Telegram),
        toast(text, on_click), owner() -> name or "", state() -> dict (busy, gaming, can_speak, room, idle),
        news(), markets(), weather(), calendar(day) -> [(datetime, name)], look() -> face.look()'s answer,
        ask_phone(text, yes, no, hint) -> pending (Telegram buttons), forget_phone(pending),
        retire_phone(pending, note)."""
        self.d = deps
        self.lock = threading.Lock()
        self.queue = []    # [{"fact", "level", "kind", "at"}]: to say once the owner can hear it
        self.last_said = 0.0
        self.last_news_said = 0.0
        self.away = False
        self.active_since = time.time()
        self.started = time.time()
        self.next_look = 0.0
        self.looking = False
        self.due = {}      # what runs when: news, markets, weather
        self.plan_phone = []  # away: "did you do it?" on Telegram, waiting [{"id", "text", "pending", "at"}]
        self.store = self._load()

    # state ---------------------------------------------------------------------------------------------------

    def _path(self):
        return _home() / "companion.json"

    def _load(self):
        try:
            data = json.loads(self._path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data.setdefault("seen_news", [])
        data.setdefault("moves", {})
        data.setdefault("greeted", "")
        data.setdefault("calls", [])  # the days of news calls
        data.setdefault("market_calls", [])
        data.setdefault("flags", {})
        return data

    def _save(self):
        self.store["seen_news"] = self.store["seen_news"][-600:]
        try:
            self._path().write_text(json.dumps(self.store, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def _situation(self):
        st = self.d["state"]()
        room = st.get("room")
        mic = mic_users() if st["can_speak"] else []
        noisy = (room is not None and room > _setting("UMAY_NOISY_LEVEL")) or bool(mic)
        quiet = quiet_hours(datetime.now().hour, _setting("UMAY_QUIET_HOURS", str))
        return {"away": st["idle"] >= _setting("UMAY_AWAY_MINUTES") * 60, "gaming": st["gaming"],
                "busy": st["busy"], "quiet": quiet, "noisy": noisy, "box": st.get("box", False),
                "can_speak": st["can_speak"] and room is not None,
                "starting": time.time() - self.started < STARTUP_WAIT_SECONDS,
                "recent": time.time() - self.last_said < GAP_MINUTES * 60, "mic": mic, "room": room,
                "idle": st["idle"]}

    # words ---------------------------------------------------------------------------------------------------

    def _words(self, facts, how, ending=""):
        """Her own words for the facts: aloud or written, opening a conversation."""
        owner = self.d["owner"]()
        prompt = (
            f"{self.d['persona']()}\n\n"
            f"Right now you start a conversation with {owner or 'the person you look after'} yourself, without "
            f"being asked ({how}). Tell the facts below in English, in one or two short sentences, the way you "
            "talk (crisp, Jarvis-like, \"dear\" at most once): the point, not commentary or feelings about it. "
            "Names exactly as written. Only the facts given: invent nothing (no guesses about the room, their mood or their day), no emoji, no lists. "
            f"{ending}\n\nFacts:\n{facts}\n\nAnswer with only the words you say.")
        try:
            text = clean(self.d["ask"](prompt))
            if text:
                return text
        except Exception as e:
            _log(f"words: the model failed ({e!r}); the facts as they are")
        return facts

    # telling -------------------------------------------------------------------------------------------------

    def tell(self, fact, level="notable", kind="other", hint="", on_click=None, items=None):
        """Thread-safe: something to bring up; it goes out at the next tick the way route() says. hint: for the
        model with the owner's answer; on_click: what a click on the line beside the orb opens; items: the
        day_plan to-dos it asks about (kind "plan_check")."""
        with self.lock:
            self.queue.append({"fact": fact, "level": level, "kind": kind, "at": time.time(), "hint": hint,
                               "on_click": on_click, "items": items or []})
        _log(f"to tell ({level}, {kind}): {fact}")

    def _deliver(self, s):
        with self.lock:
            if not self.queue:
                return
            top = max(LEVELS.index(m["level"]) for m in self.queue)
            batch = [m for m in self.queue if LEVELS.index(m["level"]) == top]
            # "did you do it?" goes on its own: away it's a question with buttons, not a line among news
            plan = batch[0]["kind"] == "plan_check"
            batch = [m for m in batch if (m["kind"] == "plan_check") == plan]
            level, kind = LEVELS[top], batch[0]["kind"]
        way = route(level, kind, s)
        if way == "wait":
            with self.lock:  # small talk that waited too long isn't worth it any more
                self.queue = [m for m in self.queue
                              if m["level"] != "chat" or time.time() - m["at"] < CHAT_WAIT_MINUTES * 60]
            return
        with self.lock:
            for m in batch:
                if m in self.queue:
                    self.queue.remove(m)
        facts = "\n".join(f"- {m['fact']}" for m in batch)
        hint = "".join(dict.fromkeys(m.get("hint", "") for m in batch))
        on_click = next((m["on_click"] for m in batch if m.get("on_click")), None)
        if way in ("hold", "call") and kind == "plan_check":  # away: asked on Telegram, a call if unanswered
            self._ask_phone([i for m in batch for i in m.get("items", [])])
            return
        if way == "hold":  # away: Telegram only, nothing on the PC's screen, now or when they are back
            if level in ("important", "urgent"):  # the rest is dropped (the owner, 2026-11-04)
                self.d["notify"]("Umay: " + "; ".join(m["fact"] for m in batch))
                _log(f"away, to Telegram only ({level}): {facts}")
            else:
                _log(f"away, dropped ({level}): {facts}")
            return
        if way == "call":
            if not self._may_call(kind):
                self.d["notify"]("Umay: " + "; ".join(m["fact"] for m in batch))
                _log(f"no more {kind} calls today; to Telegram: {facts}")
                return
            text = self._words(facts, "you are calling them on the phone because it can't wait; say why",
                               "Then ask if they want to talk about it.")
            self._call(text, facts)
            return
        if way == "write":
            _log(f"writing, not speaking: {why_written(s)}")
        ending = ("Ask in a few words whether they did them." if kind == "plan_check"
                  else "You may end with a question of a few words, if one fits.")
        self._say(facts, way, level, ending, hint=hint, on_click=on_click)

    def _may_call(self, kind):
        """Counts a call of this kind today; False once CALLS_A_DAY of them were made."""
        if kind not in CALLS_A_DAY:
            return True
        key = {"news": "calls", "markets": "market_calls"}.get(kind, f"{kind}_calls")
        today = date.today().isoformat()
        calls = [c for c in self.store.get(key, []) if c == today]
        if len(calls) >= CALLS_A_DAY[kind]:
            return False
        self.store[key] = calls + [today]
        self._save()
        return True

    def _call(self, text, facts, hint=""):
        answer = self.d["call"](text, hint) if hint else self.d["call"](text)
        _log(f"call: {text!r} -> {answer}")
        if not answer.startswith("Calling"):  # no number, a game, already in a call: Telegram at least
            self.d["notify"](f"Umay: {facts}\n({answer})")
        return answer.startswith("Calling")

    def _say(self, facts, way, level, ending="You may end with a question of a few words, if one fits.", hint="",
             on_click=None):
        if way == "speak":
            text = self._words(facts, "spoken aloud at the PC", ending)
            if self.d["speak"](text, hint):
                self.d["toast"](text, on_click)
                _log(f"said aloud ({level}): {text}")
            else:
                way = "write"
        if way == "write":
            text = self._words(facts, "written in a chat window on their screen", ending)
            self.d["write"](text, hint)
            self.d["toast"](text, on_click)
            _log(f"written ({level}): {text}")
        self.last_said = time.time()

    # the day's plan --------------------------------------------------------------------------------------------

    def _plan(self, s):
        """A to-do's time came: a reminder. Every UMAY_PLAN_CHECK_HOURS (not at night): "did you do it?" about the
        open ones. Away, the questions on Telegram are followed up (_phone_follow_up)."""
        import day_plan

        now = datetime.now()
        for item in day_plan.update(lambda data, items: day_plan.due_reminders(items, now)):
            self.tell(f"It is {item['at']}: time for '{item['text']}' (on today's plan)", "important", "plan",
                      hint=DONE_HINT)
        self._phone_follow_up(s)
        if s["quiet"] or s["gaming"] or self.store["greeted"] != now.date().isoformat() or self.plan_phone:
            return  # the day's plan is asked first (the opening); nothing new while phone questions are open
        last = self.store.get("plan_checked", 0.0)
        if not last or datetime.fromtimestamp(last).date() != now.date():
            last = self.store.get("plan_opened", 0.0)  # the first check: hours after the plan was asked
        hours = _setting("UMAY_PLAN_CHECK_HOURS")
        if hours <= 0:
            return
        items = day_plan.update(lambda data, items: day_plan.check_due(items, now, last, hours))
        if not items:
            return
        self.store["plan_checked"] = now.timestamp()
        self._save()
        names = "; ".join(i["text"] for i in items)
        self.tell(f"These to-dos from today's plan are still open: {names}. Ask whether they did them",
                  "important", "plan_check", hint=DONE_HINT, items=[i["id"] for i in items])

    def _ask_phone(self, ids):
        """Away: one Telegram question per open to-do, with Done / Not yet buttons."""
        import day_plan

        open_ = day_plan.update(lambda data, items: [i for i in day_plan.open_items(items) if i["id"] in ids])
        for item in open_[:PLAN_PHONE_ITEMS]:
            pending = self.d["ask_phone"](f"Umay: did you do it? \"{item['text']}\" (on today's plan)",
                                          "Done ✅", "Not yet", DONE_HINT)
            self.plan_phone.append({"id": item["id"], "text": item["text"], "pending": pending,
                                    "at": time.time()})
            _log(f"away, asked on Telegram: {item['text']}")

    def _phone_follow_up(self, s):
        """The Telegram questions: a button pressed (or the to-do ticked off in a typed answer) closes one; the
        owner back at the PC: asked there instead; unanswered PLAN_CALL_AFTER_MINUTES and still away: a call."""
        import day_plan

        if not self.plan_phone:
            return
        open_ids = {i["id"] for i in day_plan.update(lambda data, items: day_plan.open_items(items))}
        still = []
        for q in self.plan_phone:
            pending = q["pending"]
            if pending is not None and pending["event"].is_set():
                if pending["ok"]:
                    day_plan.update(lambda data, items: day_plan.mark_done(items, q["id"]))
                    self.d["notify"](f"Noted: \"{q['text']}\" is done.")
                else:
                    self.d["notify"](f"All right, I'll ask about \"{q['text']}\" again later.")
                self.d["forget_phone"](pending)
                _log(f"Telegram answer for {q['text']!r}: {'done' if pending['ok'] else 'not yet'}")
                continue
            if q["id"] not in open_ids:  # ticked off or dropped (a typed answer, the PC)
                self.d["retire_phone"](pending, "(answered)")
                continue
            still.append(q)
        self.plan_phone = still
        if not still:
            return
        if not s["away"]:  # back at the PC: asked here instead, the phone's questions are closed
            for q in still:
                self.d["retire_phone"](q["pending"], "(asked at the PC)")
            self.plan_phone = []
            self.tell("These to-dos from today's plan are still open: " + "; ".join(q["text"] for q in still)
                      + ". Ask whether they did them", "important", "plan_check", hint=DONE_HINT,
                      items=[q["id"] for q in still])
            return
        if time.time() - min(q["at"] for q in still) < PLAN_CALL_AFTER_MINUTES * 60 or s["quiet"] or s["busy"]:
            return
        names = "; ".join(q["text"] for q in still)
        self.plan_phone = []
        for q in still:
            self.d["retire_phone"](q["pending"], "(asked on a call)")
        if not self._may_call("plan"):
            _log(f"no answer on Telegram, no more plan calls today: {names}")
            return
        text = self._words(f"You asked on Telegram an hour ago whether they did these to-dos and got no answer: "
                           f"{names}", "you are calling them on the phone about today's plan",
                           "Ask in a few words whether they did them.")
        _log(f"no answer on Telegram for an hour: calling ({names})")
        self._call(text, f"Did you do it? {names}", DONE_HINT)

    # sources -------------------------------------------------------------------------------------------------

    def _due(self, name, minutes):
        now = time.time()
        if now < self.due.get(name, 0):
            return False
        self.due[name] = now + minutes * 60
        return True

    def _news(self, s):
        if s["gaming"]:
            return
        data = self.d["news"]()
        seen = set(self.store["seen_news"])
        first_run = not seen
        new = [i for i in data["items"] if i["link"] not in seen]
        self.store["seen_news"] += [i["link"] for i in new]
        self._save()
        if first_run or not new:
            return  # the first read only learns what is already out
        new = sorted(new, key=lambda i: -i["at"])[:NEWS_BATCH]
        listing = "\n".join(f"{n}. [{', '.join(i['tags'])}] {i['title']} ({i['source']})"
                            for n, i in enumerate(new, 1))
        prompt = (
            "Rate each headline: should the owner be interrupted right now to hear it? 1-10. Be strict: most "
            "headlines are 2-6, a 9 comes a few times a week, a 10 a few times a year.\n"
            f"They follow: {', '.join(data.get('watch') or [])}.\n"
            "10: historic breaking news (a war starts, a market crash, a deadly earthquake in Türkiye or the UK, "
            "the CEO of a company they follow leaves). 9: big news about what they follow (a major new AI model "
            "released, a huge deal or lawsuit, an election result). 7: solid news about them (results, a new "
            "feature, a hire). 5: interesting but routine. 2: opinion, analysis, predictions, rumor, listicle, ad, "
            "a minor local story, anything that only mentions them in passing.\n"
            'Answer only with JSON: [{"i": 1, "score": 5}, ...]\n\n' + listing)
        try:
            scores = parse_scores(self.d["ask"](prompt), len(new))
        except Exception as e:
            _log(f"news: rating failed ({e!r})")
            return
        _log("news rated: " + ", ".join(f"{scores.get(n, '?')} {i['title'][:60]}" for n, i in enumerate(new, 1)))
        good = sorted(((scores[n], i) for n, i in enumerate(new, 1)
                       if scores.get(n, 0) >= _setting("UMAY_NEWS_SCORE")), key=lambda x: -x[0])[:2]
        if not good or (time.time() - self.last_news_said < NEWS_GAP_MINUTES * 60
                        and good[0][0] < _setting("UMAY_NEWS_CALL_SCORE")):
            return
        self.last_news_said = time.time()
        for score, item in good:
            # Only news about whom the owner follows can be a call: the model gave a 10 to a day's war news in the
            # WORLD section and she called (2026-11-03).
            urgent = score >= _setting("UMAY_NEWS_CALL_SCORE") and item["kind"] == "watch"
            self.tell(f"News about {' and '.join(item['tags'][:2])}: {item['title']} ({item['source']} reports)",
                      "urgent" if urgent else "important", "news")

    def _markets(self):
        data = self.d["markets"]()
        for fact, level in market_moves(data["quotes"], self.store["moves"], date.today().isoformat()):
            self.tell(fact, level, "markets")
        self._save()

    def _weather(self):
        rain = rain_soon(self.d["weather"]())
        key = f"rain-{date.today().isoformat()}-{datetime.now().hour // 6}"
        if rain and not self.store["flags"].get(key):
            self.store["flags"] = {k: v for k, v in self.store["flags"].items() if not k.startswith("rain-")}
            self.store["flags"][key] = True
            self._save()
            self.tell(rain + ". Maybe take an umbrella if you go out", "notable", "weather")

    def _battery(self):
        import psutil

        b = psutil.sensors_battery()
        if b is None:
            return
        flags = self.store["flags"]
        if b.power_plugged:
            if flags.pop("battery", None) is not None:
                self._save()
            return
        for limit, level in ((CRITICAL_BATTERY, "important"), (LOW_BATTERY, "notable")):
            if b.percent <= limit and flags.get("battery", 101) > limit:
                flags["battery"] = limit
                self._save()
                self.tell(f"The laptop's battery is at {b.percent:.0f}% and not charging", level, "battery")
                break

    def _break(self, s):
        minutes = _setting("UMAY_BREAK_MINUTES")
        if minutes <= 0 or s["gaming"]:
            return
        if time.time() - self.active_since >= minutes * 60:
            self.active_since = time.time()
            hours = minutes / 60
            self.tell(f"The owner has been at the PC for about {hours:g} hours without a break; suggest a short "
                      "stretch or a glass of water, kindly", "chat", "break")

    # the morning ---------------------------------------------------------------------------------------------

    def _morning(self, s):
        now = datetime.now()
        today = now.date().isoformat()
        if (self.store["greeted"] == today or not MORNING_FROM <= now.hour < MORNING_UNTIL or s["away"]
                or s["idle"] > 60 or s["busy"] or s["gaming"] or self.looking or time.time() < self.next_look):
            return
        self.looking = True
        threading.Thread(target=self._look_and_greet, args=(today,), daemon=True).start()

    def _look_and_greet(self, today):
        try:
            try:
                seen = self.d["look"]()
                _log(f"morning: the camera saw {seen}")
                if not seen.get("faces") or seen.get("owner") is False:
                    self.next_look = time.time() + LOOK_AGAIN_MINUTES * 60
                    return
            except Exception as e:
                _log(f"morning: the camera couldn't look ({e}); greeting anyway")
            facts = self._morning_facts()
            # Not the quiet hours: the owner just sat down and was seen. Umay just started with Windows: her voice
            # is still loading, so it gets a moment.
            for _ in range(VOICE_WAIT_SECONDS // 5):
                s = self._situation()
                if s["can_speak"] or s["noisy"] or s["gaming"]:
                    break
                time.sleep(5)
            way = "speak" if not (s["noisy"] or s["box"] or not s["can_speak"]) else "write"
            hour = datetime.now().hour
            greeting = "good morning" if hour < 12 else "good afternoon" if hour < 18 else "good evening"
            self._say(facts, way, "important",
                      f"It is the day's first talk: say {greeting}, tell only what matters in this briefly (two or "
                      "three short sentences; to-dos left from earlier, if any, by name), then ask in a few words "
                      "what they will do today.", hint=PLAN_HINT)
            # Only once it was said: 2026-11-04 07:02 the owner shut the PC down while she was getting the facts
            # ready, the day counted as greeted, and at 12:09 she didn't say good morning.
            self.store["greeted"] = today
            self.store["plan_opened"] = time.time()
            self._save()
        except Exception:
            _log("morning failed:\n" + traceback.format_exc())
            self.next_look = time.time() + LOOK_AGAIN_MINUTES * 60  # not again every tick
        finally:
            self.looking = False

    def _morning_facts(self):
        import holiday_days

        now = datetime.now()
        facts = [f"Today is {now:%A, %d %B}."]
        try:
            today = holiday_days.between(now.date(), now.date()).get(now.date())
            tomorrow_day = now.date() + timedelta(days=1)
            tomorrow = holiday_days.between(tomorrow_day, tomorrow_day).get(tomorrow_day)
            if today:
                facts.append(f"Today is {holiday_days.describe(today)}.")
            if tomorrow:
                facts.append(f"Tomorrow is {holiday_days.describe(tomorrow)}.")
        except Exception as e:
            _log(f"morning: holidays failed ({e!r})")
        try:
            w = self.d["weather"]()
            facts.append(f"Weather: {w['text'].lower()}, {w['temp']:.0f}°C now, {w['low']:.0f} to {w['high']:.0f}"
                         f"°C today, {w.get('rain_chance') or 0}% chance of rain.")
        except Exception as e:
            _log(f"morning: weather failed ({e!r})")
        try:
            rings = [(at, name) for at, name in self.d["calendar"](now.date()) if at >= now]
            if rings:
                facts.append("Reminders today: " + ", ".join(f"{name} at {at:%H:%M}" for at, name in rings[:5]) + ".")
            else:
                facts.append("No reminders set for today.")
        except Exception as e:
            _log(f"morning: calendar failed ({e!r})")
        try:
            import hud_feeds

            items = [i for i in self.d["news"]()["items"] if i["kind"] == "watch" and time.time() - i["at"] < 12 * 3600]
            for item in hud_feeds.spread(items, 2):
                facts.append(f"A headline: {item['title']}.")
        except Exception as e:
            _log(f"morning: news failed ({e!r})")
        try:
            import day_plan

            tally, moved = day_plan.today_text()
            if moved:
                facts.append("To-dos left from earlier days, still open: " + "; ".join(moved) + ".")
            elif "Nothing planned" not in tally:
                facts.append("Today's plan so far:\n" + tally)
        except Exception as e:
            _log(f"morning: the plan failed ({e!r})")
        return "\n".join(facts)

    # the loop ------------------------------------------------------------------------------------------------

    def _presence(self, s):
        """Away and back. What came while they were away already went to Telegram: it isn't told again here."""
        if s["away"]:
            if not self.away:
                _log(f"away (idle {s['idle']:.0f} s)")
            self.away = True
            return
        if self.away and s["idle"] <= BACK_SECONDS:
            self.away = False
            self.active_since = time.time()
            _log("back")

    def reminder_away(self, name):
        """A reminder rings (alarm_ring.py): if the owner is away, she calls them about it."""
        s = self._situation()
        if s["away"] and not s["busy"]:
            text = self._words(f"Your reminder is ringing on the PC: {name}", "you are calling them on the phone",
                               "Keep it short.")
            _log(f"reminder while away: calling ({name})")
            self._call(text, f"Reminder: {name}")
            return True
        return False

    def tick(self):
        s = self._situation()
        self._presence(s)
        self._morning(s)
        jobs = [("news", NEWS_MINUTES, lambda: self._news(s)), ("markets", MARKETS_MINUTES, self._markets),
                ("weather", WEATHER_MINUTES, self._weather), ("battery", 1, self._battery)]
        for name, minutes, job in jobs:
            if self._due(name, minutes):
                try:
                    job()
                except Exception as e:
                    _log(f"{name} failed: {e!r}")
        self._break(s)
        try:
            self._plan(s)
        except Exception:
            _log("plan failed:\n" + traceback.format_exc())
        self._deliver(s)

    def run(self):
        _log("started")
        while True:
            try:
                self.tick()
            except Exception:
                _log("tick failed:\n" + traceback.format_exc())
            time.sleep(TICK_SECONDS)


_it = {"companion": None}


def start(deps):
    """Starts her initiative (main.py), unless UMAY_COMPANION=off."""
    if not enabled() or _it["companion"] is not None:
        return
    _it["companion"] = Companion(deps)
    threading.Thread(target=_it["companion"].run, daemon=True).start()


def tell(fact, level="notable", kind="other", on_click=None):
    """Thread-safe: something for her to bring up (other modules: the disk scan, ...); False if the companion is
    off (then the caller shows it itself)."""
    if _it["companion"] is None:
        return False
    _it["companion"].tell(fact, level, kind, on_click=on_click)
    return True


def away_seconds():
    return _setting("UMAY_AWAY_MINUTES") * 60


def reminder_rang(name):
    """alarm_ring.py: a reminder rings; True if she called the owner (away) about it."""
    c = _it["companion"]
    return bool(c and c.reminder_away(name))
