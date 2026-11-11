"""Umay's side of the HUD (hud_app.py, its own process): starts it, tells it what she is doing, and hears
the orb's clicks. It took the desktop pet's place, with the same calls: start, activity, say, visible,
toggle, anchor.

activity(): what the orb shows. "listen", "think", "talk" (the pet's names), "research" (a web search or
page) and "work" (a tool acting on the PC); None = standby. While she speaks, her voice's loudness
(voice.level) goes to the orb LEVEL_HZ times a second.

The HUD's panels open detail views; for what only Umay's process can do (the credit balances, scanning and
deleting on the disk) the HUD sends {"event": name, ...}, which runs the handler registered with on(name)
off the reader thread; tell() sends the answer back. open_detail() opens a panel's detail view.

If the HUD's process ends on its own it is started again, up to RESTARTS times. UMAY_HUD=off: no HUD.
"""
import json
import os
import subprocess
import sys
import threading
import time

import paths

STATES = {None: "idle", "listen": "listening", "think": "thinking", "research": "researching", "work": "working",
          "talk": "speaking"}
LEVEL_HZ = 20
RESTARTS = 3
RESTART_SECONDS = 5

_state = {"proc": None, "on_click": None, "visible": True, "muted": False, "activity": None, "anchor": None,
          "says": {}, "next_id": 0, "restarts": 0, "stopping": False, "handlers": {}}
_lock = threading.Lock()


def enabled():
    return os.environ.get("UMAY_HUD", "").strip().lower() != "off"


def _command():
    if getattr(sys, "frozen", False):  # Umay.exe: main.py --hud runs hud_app in the same exe
        return [sys.executable, "--hud", str(os.getpid())]
    return [sys.executable, "-X", "utf8", str(paths.resource_dir() / "hud_app.py"), str(os.getpid())]


def _send(obj):
    with _lock:
        proc = _state["proc"]
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.stdin.write(json.dumps(obj) + "\n")
            proc.stdin.flush()
        except (OSError, ValueError):
            pass


def start(on_click=None, language=None):
    """Starts the HUD (does nothing twice). on_click() runs when the orb is clicked. `language` is the
    pet's old argument: the HUD is in English."""
    if on_click is not None:
        _state["on_click"] = on_click
    if not enabled() or (_state["proc"] is not None and _state["proc"].poll() is None):
        return
    log = paths.voice_home().parent / "hud_process.log"
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as err:
            proc = subprocess.Popen(_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err,
                                    text=True, encoding="utf-8", cwd=str(paths.resource_dir()),
                                    env=paths.own_child_env(), creationflags=subprocess.CREATE_NO_WINDOW)
    except OSError as e:
        print(f"HUD couldn't start: {e}")
        return
    _state["proc"] = proc
    threading.Thread(target=_read, args=(proc,), daemon=True).start()
    threading.Thread(target=_voice_level, args=(proc,), daemon=True).start()
    _send({"state": STATES[_state["activity"]]})
    if not _state["visible"]:
        _send({"visible": False})
    if _state["muted"]:  # a HUD that was restarted
        _send({"muted": True})


def _read(proc):
    for line in proc.stdout:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("event") == "click" and _state["on_click"] is not None:
            threading.Thread(target=_state["on_click"], daemon=True).start()
        elif event.get("event") == "say_click":
            callback = _state["says"].pop(event.get("id"), None)
            if callback is not None:
                threading.Thread(target=callback, daemon=True).start()
        elif "orb" in event:
            _state["anchor"] = tuple(event["orb"])
        elif event.get("event") == "ready":
            _state["restarts"] = 0
        elif event.get("event") in _state["handlers"]:
            threading.Thread(target=_handle, args=(event,), daemon=True).start()
    proc.stdout.close()
    code = proc.wait()
    _state["anchor"] = None
    if _state["stopping"] or _state["proc"] is not proc:
        return
    print(f"HUD stopped (exit {code}), see {paths.voice_home().parent / 'hud_process.log'}")
    if _state["restarts"] < RESTARTS:
        _state["restarts"] += 1
        time.sleep(RESTART_SECONDS)
        start()


def _handle(event):
    try:
        _state["handlers"][event["event"]](event)
    except Exception as e:  # a failed request never takes the reader down
        print(f"HUD event {event.get('event')} failed: {type(e).__name__}: {e}")


def on(name, handler):
    """handler(event) runs (on a thread of its own) when the HUD sends {"event": name, ...}."""
    _state["handlers"][name] = handler


def tell(obj):
    """Thread-safe: a message to the HUD (an answer to one of its events)."""
    _send(obj)


def open_detail(name):
    """Thread-safe: opens a panel's detail view ("storage" for the disk scan's findings), showing the HUD."""
    if not visible():
        toggle()
    _send({"open": name})


def open_audio():
    """Thread-safe: opens the audio panel (Ctrl+F3: listening, the microphone, the speakers), or closes it if it is
    open; even while the HUD is hidden. False when there is no HUD to show it."""
    if pid() is None:
        return False
    _send({"open": "audio", "toggle": True})
    return True


def _voice_level(proc):
    """Her voice's loudness to the orb while she speaks."""
    while proc.poll() is None:
        if _state["activity"] == "talk":
            import voice
            _send({"level": round(voice.level(), 3)})
            time.sleep(1 / LEVEL_HZ)
        else:
            time.sleep(0.2)


def activity(kind):
    """Thread-safe: what Umay is doing now (STATES' keys)."""
    if kind not in STATES:
        kind = None
    _state["activity"] = kind
    _send({"state": STATES[kind]})


def current():
    return _state["activity"]


def mic_muted(on):
    """Thread-safe: shows or hides the muted microphone above the panels button (Ctrl+F3)."""
    _state["muted"] = bool(on)
    _send({"muted": bool(on)})


def say(text, seconds=10, on_click=None):
    """Thread-safe: a line beside the orb; on_click() runs if it is clicked."""
    _state["next_id"] += 1
    if on_click is not None:
        _state["says"][_state["next_id"]] = on_click
    _send({"say": text, "id": _state["next_id"], "seconds": seconds})


def pid():
    """The HUD process's id (None when it isn't running)."""
    proc = _state["proc"]
    return proc.pid if proc is not None and proc.poll() is None else None


def visible():
    return _state["visible"] and _state["proc"] is not None and _state["proc"].poll() is None


def toggle():
    """Thread-safe: hides the HUD if it's shown, else shows it (starting it if needed). Returns whether it
    is shown afterwards."""
    if visible():
        _state["visible"] = False
        _send({"visible": False})
        return False
    _state["visible"] = True
    start()
    _send({"visible": True})
    return visible()


def anchor():
    """The orb's window (x, y, w, h) while the HUD is shown, else None (the text box's place)."""
    return _state["anchor"] if visible() else None


def stop():
    _state["stopping"] = True
    with _lock:
        proc = _state["proc"]
        if proc is not None and proc.poll() is None:
            try:
                proc.stdin.close()  # the HUD ends at the end of its input
            except OSError:
                pass
