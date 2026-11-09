import sys

if __name__ == "__main__" and "--hud" in sys.argv:  # the HUD's own process (hud.py starts it)
    import hud_app

    sys.exit(hud_app.main(int(sys.argv[-1]) if sys.argv[-1].isdigit() else 0))
if __name__ == "__main__" and "--memory" in sys.argv:  # the memory editor's own process (memory_editor.open_editor)
    import memory_editor

    sys.exit(memory_editor.main())

import threading
import tkinter as tk
from datetime import datetime

import ai_assistant
import calls
import alert
import chat_box
import check_event
import companion
import config_loader
import eyes
import face
import conversation_memory
import disk_actions
import disk_watch
import health_monitor
import hud
import hud_data
import hud_feeds
import i18n
import local_llm
import memory_cortex
import pc_tools
import price_alerts
import router
import system_tools
import telegram_listener
import voice
import voice_guard
import voice_session
import wake
import web_control

RELOAD_INTERVAL_SECONDS = 1 * 60
INSTANCE_MUTEX = "Local\\Umay.instance"
ERROR_ALREADY_EXISTS = 183
_instance = {}


def only_instance():
    """False when another Umay is already running (started at sign-in, then again by hand): two would share
    the Telegram bot, the microphone and the orb. A named mutex, held until this process ends."""
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    _instance["mutex"] = kernel32.CreateMutexW(None, False, INSTANCE_MUTEX)
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def sync_events(running, raw_events):
    now = datetime.now()
    seen_names = set()

    for raw_event in raw_events:
        name = raw_event["name"]
        seen_names.add(name)
        existing = running.get(name)

        if existing is not None and existing["raw"] == raw_event:
            continue

        if existing is not None:
            print(f"Config changed for '{name}', restarting.")
            existing["stop_event"].set()

        config = config_loader.parse_event(raw_event, now)
        stop_event = threading.Event()
        thread = threading.Thread(target=check_event.is_event, args=(config, stop_event), daemon=True)
        running[name] = {"raw": raw_event, "stop_event": stop_event, "thread": thread}
        thread.start()

    for name in list(running.keys()):
        if name not in seen_names:
            print(f"Event '{name}' removed from config, stopping.")
            running.pop(name)["stop_event"].set()


def supervisor_loop():
    running = {}

    while True:
        try:
            raw_events = config_loader.load_raw_events()
        except Exception as e:
            print(f"Failed to load config: {e}")
        else:
            sync_events(running, raw_events)

        config_loader.wait_for_reload(RELOAD_INTERVAL_SECONDS)


def _extension_problem(text):
    """The Web Control extension can't work (web_control.watch): said aloud at the PC (companion.py, the owner
    2026-11-04: what she shows is said), and sent on Telegram."""
    print(f"Web control: {text}")
    if not companion.tell(f"The browser extension Umay uses (Web Control) has a problem: {text}", "notable",
                          "extension"):
        hud.say("🧩 " + text, seconds=20)
    pc_tools.fallback_notify("🧩 " + text)


def _clean_mei():
    """Deletes the Temp\\_MEI folders force-closed Umay exes left behind (disk_actions.clean_mei)."""
    try:
        removed, kept = disk_actions.clean_mei()
    except Exception as e:
        print(f"_MEI clean-up failed: {e}")
        return
    if removed or kept:
        print(f"_MEI clean-up: {len(removed)} deleted, {len(kept)} still in use")


def _disk_findings(found):
    """The daily disk scan found something new: said aloud at the PC (companion.py), its line beside the orb opens
    the Disk tab on a click."""
    print("Disk: " + "; ".join(f.text for f in found))
    if not companion.tell("The daily disk scan found: " + "; ".join(f.text for f in found[:3])
                          + ". The details are in the Disk tab of the HUD", "notable", "disk",
                          on_click=ai_assistant.open_disk_panel):
        hud.say(i18n.get(ai_assistant.current_language(), "disk_findings"), seconds=30,
                on_click=ai_assistant.open_disk_panel)


_place = {}


def _weather():
    if "it" not in _place:
        _place["it"] = hud_data.find_place()
    return hud_data.read_weather(_place["it"])


def _rings_on(day):
    return [(r["at"], r["name"]) for r in hud_data.read_alarms(day.year, day.month)["by_day"].get(day.day, [])]


def _price_alarm(text, level="important"):
    """A price alarm fired (price_alerts.py): the companion tells it the way it tells things (aloud / written /
    Telegram when away; "urgent", a move far bigger than expected: a WhatsApp call when away); with the
    companion off, Telegram."""
    if not companion.tell(text, level=level, kind="markets"):
        pc_tools.fallback_notify(text)


def _start_companion():
    """Umay on her own initiative (companion.py): what she needs from the rest of her."""
    companion.start({
        "ask": lambda prompt: ai_assistant.ask_flash(prompt, what="companion", source="companion"),
        "persona": ai_assistant.persona,
        "speak": voice_session.open_with,
        "write": ai_assistant.reach_out,
        "call": calls.call_owner,
        "notify": lambda text: pc_tools.fallback_notify(text),  # Telegram, once it's connected
        "toast": lambda text, on_click=None: hud.say(text, seconds=min(40, max(12, len(text) // 12)),
                                                     on_click=on_click),
        "ask_phone": telegram_listener.ask_with_buttons,
        "forget_phone": telegram_listener.forget_alert,
        "retire_phone": telegram_listener.retire_alert,
        "owner": lambda: "" if ai_assistant.USER_NAME == "You" else ai_assistant.USER_NAME,
        "state": lambda: {"busy": voice_session.active() or calls.active(), "gaming": voice.gaming["on"],
                          "can_speak": voice.tts_on(), "room": wake.room_level(),
                          "idle": system_tools.idle_seconds(), "box": chat_box.is_open()},
        "news": hud_feeds.read_news,
        "markets": hud_feeds.read_markets,
        "weather": _weather,
        "calendar": _rings_on,
        "look": face.look,
    })


def main():
    if not only_instance():
        print("Umay is already running.")
        return
    threading.Thread(target=_clean_mei, daemon=True).start()
    threading.Thread(target=supervisor_loop, daemon=True).start()
    ai_assistant.start_ai_assistant()
    router.warm_up()  # the local names (apps, folders, alarms) for the shadow router, read in the background
    ai_assistant.wire_hud()
    hud.start(on_click=ai_assistant.toggle_chat_box)
    telegram_listener.start()
    health_monitor.start()
    voice_guard.start()
    voice_session.setup(answer=lambda chat, text: ai_assistant.answer_message(chat, text, source="voice"),
                        new_chat=ai_assistant.new_chat,
                        notify=lambda text: pc_tools.fallback_notify(text),  # Telegram, once it's connected
                        activity=hud.activity, log_dir=ai_assistant.CHAT_LOG_DIR,
                        ended=lambda chat: memory_cortex.conversation_ended((chat or {}).get("log_path")))
    # Umay on the phone (WhatsApp Beta, her own number): the same conversation, over a call.
    calls.configure(answer=lambda chat, text: ai_assistant.answer_message(chat, text, source="call"),
                    new_chat=ai_assistant.new_chat, notify=lambda text: pc_tools.fallback_notify(text),
                    activity=hud.activity, log_dir=ai_assistant.CHAT_LOG_DIR)
    calls.watch()
    eyes.configure(notify=lambda text: hud.say("👁 " + text, seconds=12), skip=lambda: [p for p in [hud.pid()] if p])

    def woke():
        voice_session.start()
        said = eyes.on_wake()  # "Hey Umay" opens her eyes too
        if said:
            hud.say("👁 " + said, seconds=12)
    wake.start(on_wake=woke, paused=lambda: voice_session.active() or calls.active(),
               on_eyes=lambda what: hud.say("👁 " + eyes.switch(what), seconds=12))
    web_control.start()
    web_control.watch(notify=_extension_problem, lang=ai_assistant.current_language)
    disk_watch.configure(ask=ai_assistant.ask_flash, notify=_disk_findings, lang=ai_assistant.current_language)
    disk_watch.start()
    # Judges finished conversations and keeps chat_logs under its limit, with the local model: not during a
    # game, nor while it is answering.
    conversation_memory.start(ai_assistant.CHAT_LOG_DIR,
                              can_run=lambda: not voice.gaming["on"] and not local_llm.answering())
    # The Memory Cortex reads each finished conversation once; while the owner is away (AFK, companion's
    # UMAY_AWAY_MINUTES) it reads all the logs again and SELF researches (a small Tavily budget, topics from the
    # memory and the newsfeed's follows). Never while she listens, answers or talks, nor during a game.
    memory_cortex.start(ai_assistant.CHAT_LOG_DIR,
                        can_run=lambda: not voice.gaming["on"] and not local_llm.answering()
                        and not voice_session.active() and not calls.active(),
                        search=lambda q: ai_assistant.tavily_search(q, max_results=3),
                        remaining=ai_assistant._tavily_remaining,
                        away=lambda: system_tools.idle_seconds() >= companion.away_seconds(),
                        news=hud_feeds.watch_list)
    _start_companion()
    price_alerts.start(_price_alarm)

    root = tk.Tk()
    root.withdraw()
    alert.init(root)
    root.mainloop()


if __name__ == "__main__":
    main()
