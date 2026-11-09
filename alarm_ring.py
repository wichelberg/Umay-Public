"""One ringing alarm or reminder (the events in config.json): popup + looping sound on the PC, and a
Telegram message with Close / Snooze buttons so it can be stopped from the phone.

2026-10-25: an alarm rang for hours while the owner was away, because only the popup's OK stopped it.
Now Telegram gets it at once when the owner is away (no input for AWAY_IDLE_SECONDS), otherwise once
it has rung REMOTE_AFTER_SECONDS unanswered (at the PC it's usually closed before that). Past
SILENCE_AFTER_SECONDS the sound stops by itself; the popup and the Telegram buttons stay.
2026-11-03: away for the companion's UMAY_AWAY_MINUTES (5) while it rings, Umay also calls the owner on WhatsApp
about it, once (companion.reminder_rang).
"""
import itertools
import threading
import time
from datetime import datetime, timedelta

import alert
import i18n

LANGUAGE = "en"
AWAY_IDLE_SECONDS = 10 * 60
REMOTE_AFTER_SECONDS = 2 * 60
SILENCE_AFTER_SECONDS = 15 * 60
SNOOZE_MINUTES = 10
POLL_SECONDS = 1

_counter = itertools.count(1)


def _telegram():
    import telegram_listener  # lazy: it pulls in the whole assistant
    return telegram_listener


def _idle_seconds():
    import system_tools
    return system_tools.idle_seconds()


def _away_seconds():
    import companion
    return companion.away_seconds()


def _call_about(name):
    import companion
    try:
        companion.reminder_rang(name)
    except Exception as e:
        print(f"Alarm '{name}': the call failed: {e!r}")


def ring(name, sound_path):
    """Thread-safe and returns at once: the alarm runs on its own thread until it's closed."""
    threading.Thread(target=_ring, args=(name, sound_path), daemon=True).start()


def _ring(name, sound_path):
    try:
        _run(name, sound_path)
    except Exception as e:
        print(f"Alarm '{name}' failed: {e}")


def _run(name, sound_path):
    token = f"alarm-{next(_counter)}-{time.time()}"
    local = {}  # the popup's answer
    alert.show_choice(token, name, sound_path, ["OK"], lambda i: local.setdefault("closed", True),
                      title="Event Alert", close_choice=0)
    telegram = _telegram()
    started = time.monotonic()
    pending = None
    tried_remote = silenced = called = False
    while True:
        if "closed" in local:
            how = "pc"
            break
        if pending and pending["event"].is_set():
            how = "close" if pending["ok"] else "snooze"
            break
        elapsed = time.monotonic() - started
        if not tried_remote and (elapsed >= REMOTE_AFTER_SECONDS or _idle_seconds() >= AWAY_IDLE_SECONDS):
            tried_remote = True
            pending = telegram.send_alert(
                i18n.get(LANGUAGE, "alarm_ringing", name=name, time=datetime.now().strftime("%H:%M")),
                i18n.get(LANGUAGE, "alarm_button_close"),
                i18n.get(LANGUAGE, "alarm_button_snooze", minutes=SNOOZE_MINUTES))
        if not called and _idle_seconds() >= _away_seconds():
            called = True
            threading.Thread(target=_call_about, args=(name,), daemon=True).start()
        if not silenced and elapsed >= SILENCE_AFTER_SECONDS:
            silenced = True
            alert.silence(token)
            if pending:
                telegram.send(i18n.get(LANGUAGE, "alarm_silenced", name=name, minutes=SILENCE_AFTER_SECONDS // 60))
        time.sleep(POLL_SECONDS)

    now = datetime.now()
    if how == "pc":
        telegram.retire_alert(pending, i18n.get(LANGUAGE, "alarm_closed_pc", time=now.strftime("%H:%M")))
        return
    alert.close(token)
    telegram.forget_alert(pending)
    if how == "close":
        telegram.send(i18n.get(LANGUAGE, "alarm_closed_remote", name=name))
    else:
        timer = threading.Timer(SNOOZE_MINUTES * 60, ring, args=(name, sound_path))
        timer.daemon = True
        timer.start()
        at = (now + timedelta(minutes=SNOOZE_MINUTES)).strftime("%H:%M")
        telegram.send(i18n.get(LANGUAGE, "alarm_snoozed", name=name, minutes=SNOOZE_MINUTES, time=at))
