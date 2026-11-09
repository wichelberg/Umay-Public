"""Umay's eyes: "Umay, eyes on" turns the laptop camera on and the hands move the mouse (an open hand moves the
cursor like a trackpad, a fist holds the left button, a drag over both monitors; two hands zoom, show every
window, stretch one: hand_gesture.py); "eyes off" turns it off. hands_worker.py does
it in hands_env (setup_voice_env.py --hands); this module starts and stops it and says how it went beside the
HUD's orb.

The phrase works three ways: said any time (the wake listener hears "Umay, eyes on" without "Hey Umay" first,
wake.heard_eyes), said in a spoken conversation, or typed (in the text box or on Telegram): command() catches
"eyes on / off" before any model sees it, so the small local model can't get it wrong.

A ✌ held for a second in front of the camera turns it off too. "Hey Umay" turns it on as well
(UMAY_EYES_ON_WAKE=off: not), and a window grabbed with a fist and thrown goes to the other monitor, is closed
(no monitor that way), maximized (up) or minimized (down). It turns itself
off after 5 minutes without a hand in front of the camera. Its log: voice_home/hands_worker.log.

photo(): one picture from the laptop's camera (never the owner's phone: hands_worker picks it by name), for
/kamera on Telegram and the send_camera_photo tool; taken by the running worker while the eyes are on.
"""
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import paths

ON_WORDS = ("on", "open", "aç", "açık", "açıl")
OFF_WORDS = ("off", "close", "closed", "kapa", "kapat", "kapalı")
# "eyes on", "eyes are on", "eyes, off", "gözler açık", "gözlerini kapat"
PHRASE = re.compile(r"\b(?:eyes?|göz\w*)\b[\s,.:!-]*(?:(?:are|is)\s+)?(\w+)", re.IGNORECASE)
TEXTS = {
    "on": "Eyes on: move an open hand like a trackpad, a fist grabs and drags, thumb on index clicks (twice: a "
          "double click). Throw a grabbed window to send it to the other monitor (up: maximize, down: minimize). "
          "Click a window, then push it aside with your hand side-on to close it. Two open hands zoom, two side-on "
          "hands pushed outwards show every window, two fists stretch a window. Say \"eyes off\" or show ✌ for a "
          "second to stop.",
    "already": "My eyes are already on.",
    "off": "Eyes off: the camera is closed.",
    "not_on": "My eyes weren't on.",
    "missing": "I can't see yet: the camera's environment isn't set up (python setup_voice_env.py --hands).",
    "idle": "Eyes off: no hand for 5 minutes, so I closed the camera.",
    "peace": "Eyes off: you showed ✌, so I closed the camera.",
    "closed": "Closed {title}.",
    "error": "Eyes off: {text}.",
}

_state = {"proc": None, "notify": print, "lock": threading.Lock(), "skip": lambda: []}


def command(text, max_words=None):
    """"on" / "off" if the text asks for the eyes (anything else: None). max_words: only a short message counts
    (a typed or spoken one: "keep your eyes open for the courier" isn't a command)."""
    if max_words is not None and len((text or "").split()) > max_words:
        return None
    for m in PHRASE.finditer(text or ""):
        word = m.group(1).lower()
        if word.startswith(ON_WORDS):
            return "on"
        if word.startswith(OFF_WORDS):
            return "off"
    return None


def python():
    custom = os.environ.get("UMAY_EYES_PYTHON", "").strip()
    return Path(custom) if custom else paths.voice_home() / "hands_env" / "Scripts" / "python.exe"


def available():
    return python().is_file()


def active():
    proc = _state["proc"]
    return proc is not None and proc.poll() is None


def configure(notify, skip=None):
    """notify(text): how a start or stop went, said beside the HUD's orb. skip() -> more process ids whose
    windows a throw never closes (the HUD's)."""
    _state["notify"] = notify
    if skip is not None:
        _state["skip"] = skip


def on_wake():
    """"Hey Umay" turns the eyes on too (UMAY_EYES_ON_WAKE=off: not). What to say, or None."""
    if os.environ.get("UMAY_EYES_ON_WAKE", "").strip().lower() == "off" or active() or not available():
        return None
    return start()


def start():
    """Turns the eyes on; returns what to tell the user."""
    with _state["lock"]:
        if active():
            return TEXTS["already"]
        if not available():
            return TEXTS["missing"]
        log = paths.voice_home() / "hands_worker.log"
        skip = [str(os.getpid())] + [str(pid) for pid in _state["skip"]()]
        with open(log, "a", encoding="utf-8") as err:
            proc = subprocess.Popen([str(python()), "-X", "utf8", str(paths.resource_dir() / "hands_worker.py"), *skip],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, text=True,
                                    encoding="utf-8", cwd=str(paths.resource_dir()),
                                    creationflags=subprocess.CREATE_NO_WINDOW)
        _state["proc"] = proc
    threading.Thread(target=_read, args=(proc,), daemon=True).start()
    return TEXTS["on"]


def _read(proc):
    for line in proc.stdout:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("event") in ("idle", "peace"):
            _state["notify"](TEXTS[event["event"]])
        elif event.get("event") == "closed":
            _state["notify"](TEXTS["closed"].format(title=event.get("title", "it")))
        elif event.get("event") == "error":
            _state["notify"](TEXTS["error"].format(text=event.get("text", "the camera failed")))
    proc.wait()


def photo(timeout=25):
    """A JPEG from the laptop's camera (bytes). Raises RuntimeError with what to tell the user."""
    if not available():
        raise RuntimeError(TEXTS["missing"])
    path = Path(tempfile.gettempdir()) / f"umay_camera_{os.getpid()}_{int(time.time() * 1000)}.jpg"
    try:
        proc = _state["proc"]
        if active():
            proc.stdin.write(f"snap {path}\n")
            proc.stdin.flush()
            end = time.monotonic() + 5
            while time.monotonic() < end and not path.is_file():
                time.sleep(0.1)
            time.sleep(0.2)  # written to the end
        else:
            out = subprocess.run([str(python()), "-X", "utf8", str(paths.resource_dir() / "hands_worker.py"), "--snap",
                                  str(path)], capture_output=True, text=True, encoding="utf-8", timeout=timeout,
                                 cwd=str(paths.resource_dir()), creationflags=subprocess.CREATE_NO_WINDOW).stdout
            for line in out.splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("event") == "error":
                    raise RuntimeError(TEXTS["error"].format(text=event.get("text", "the camera failed")))
        if not path.is_file():
            raise RuntimeError("The camera gave no picture.")
        return path.read_bytes()
    except subprocess.TimeoutExpired:
        raise RuntimeError("The camera took too long to answer.")
    finally:
        path.unlink(missing_ok=True)


def send_camera_photo(caption: str = "") -> str:
    """The tool: a photo from the laptop's camera to the owner on Telegram."""
    import pc_tools

    send = pc_tools.current_photo_sender()
    if send is None:
        return "The user is at the PC (Umay's window): there's nobody to send a camera photo to."
    try:
        send(photo(), caption or "📷")
    except RuntimeError as e:
        return str(e)
    return "Sent a photo from the laptop's camera to the user on Telegram."


TOOLS = [{
    "type": "function",
    "function": {
        "name": "send_camera_photo",
        "description": "Takes a photo with the laptop's camera (the room at home; never the user's phone) and sends "
                       "it to the user on Telegram. Only when they ask to see home, the room, or the camera.",
        "parameters": {"type": "object", "properties": {"caption": {"type": "string",
                                                                     "description": "Optional short caption."}},
                       "required": []},
    },
}]
TOOL_FUNCTIONS = {"send_camera_photo": send_camera_photo}


def stop():
    """Turns the eyes off (a held button is let go first); returns what to tell the user."""
    with _state["lock"]:
        proc = _state["proc"]
        if proc is None or proc.poll() is not None:
            return TEXTS["not_on"]
        try:
            proc.stdin.write("stop\n")
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
        _state["proc"] = None
    return TEXTS["off"]


def switch(what):
    """"on" / "off" -> start() / stop()."""
    return start() if what == "on" else stop()
