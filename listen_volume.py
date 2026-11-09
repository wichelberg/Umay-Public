"""The PC's volume while Umay listens: on speakers, the master volume drops to UMAY_LISTEN_VOLUME (5%) while a
spoken conversation listens, so music or a video doesn't drown the owner out, and comes back the moment the owner
has said their task (voice_session.py: "thinking"), or when the conversation ends (the owner, 2026-11-04).

- Only on speakers: headphones / a headset (the default device's form factor) aren't heard by the microphone, so
  the volume stays. A laptop whose headphone jack shares the "Speakers" device can't be told apart: it drops too.
- Never louder: a volume already at or below the listening level, or muted, is left alone.
- The owner's own change wins: if the volume isn't the one set here any more when it should come back, it stays.
- A crash while it's down: the volume before is kept in %LOCALAPPDATA%\\Umay\\listen_volume.json and put back when
  Umay starts again (restore_left_over).

The work runs on one thread of its own (Windows' audio objects want their thread set up; the recorder's callbacks
must not wait for it). UMAY_LISTEN_VOLUME=off turns it off. Log: %LOCALAPPDATA%\\Umay\\listen_volume.log.
"""
import json
import os
import queue
import threading
from datetime import datetime
from pathlib import Path

DEFAULT_LEVEL = 5
FORM_FACTOR_KEY = "{1DA5D803-D492-4EDD-8C23-E0C0FFEE7F0E} 0"   # PKEY_AudioEndpoint_FormFactor
NOT_HEARD = {3: "headphones", 5: "headset", 6: "handset"}     # the microphone doesn't hear these
SAME = 1.5   # % apart: still the volume set here (Windows rounds the scalar)


def _home():
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Umay"


def _log(text):
    try:
        with open(_home() / "listen_volume.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}\n")
    except OSError:
        pass


def level_setting():
    """The listening volume in % (0-100), or None when UMAY_LISTEN_VOLUME=off."""
    raw = os.environ.get("UMAY_LISTEN_VOLUME", "").strip().lower()
    if raw in ("off", "no", "false"):
        return None
    try:
        return max(0, min(100, int(float(raw)))) if raw else DEFAULT_LEVEL
    except ValueError:
        return DEFAULT_LEVEL


# --- pure rules (tests/test_listen_volume.py) ----------------------------------------------------------------

def lower_to(now, muted, form_factor, target):
    """The volume to set while listening, or None to leave it (with why, for the log)."""
    if target is None:
        return None, "off (UMAY_LISTEN_VOLUME)"
    if form_factor in NOT_HEARD:
        return None, f"{NOT_HEARD[form_factor]}, not speakers"
    if muted:
        return None, "muted"
    if now <= target + SAME:
        return None, f"already {now:.0f}%"
    return target, ""


def restore_to(now, saved):
    """The volume to put back, or None: the owner changed it meanwhile (it isn't the one set here any more)."""
    if saved is None:
        return None
    return saved["before"] if abs(now - saved["set"]) <= SAME else None


# --- the device ----------------------------------------------------------------------------------------------

class _Speakers:
    """The default output device (Windows' audio objects; this thread must be set up for them first)."""

    def __init__(self):
        from pycaw.pycaw import AudioUtilities

        self.device = AudioUtilities.GetSpeakers()
        self.id = getattr(self.device, "id", None)
        props = getattr(self.device, "properties", None) or {}
        self.form_factor = next((v for k, v in props.items() if k.upper() == FORM_FACTOR_KEY), None)
        self.volume = self.device.EndpointVolume

    def percent(self):
        return self.volume.GetMasterVolumeLevelScalar() * 100

    def muted(self):
        return bool(self.volume.GetMute())

    def set(self, percent):
        self.volume.SetMasterVolumeLevelScalar(max(0.0, min(100.0, percent)) / 100, None)


def _speakers_by_id(device_id):
    """The device the volume was lowered on (the default may have changed since), or None if it's gone."""
    from pycaw.pycaw import AudioUtilities

    for device in AudioUtilities.GetAllDevices():
        if getattr(device, "id", None) == device_id:
            try:
                return device.EndpointVolume
            except Exception:
                return None
    return None


class Ducker:
    """down() / up() from any thread, as often as wanted: only a change of wish does anything."""

    def __init__(self, speakers=_Speakers, path=None):
        self._speakers = speakers
        self._path = path or (_home() / "listen_volume.json")
        self._wishes = queue.Queue()
        self._thread = None
        self._lock = threading.Lock()
        self._down = False   # the last wish

    def down(self):
        self._wish(True)

    def up(self):
        self._wish(False)

    def _wish(self, down):
        with self._lock:
            if down == self._down:
                return
            self._down = down
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()
        self._wishes.put(down)

    def _run(self):
        try:
            import comtypes

            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except Exception:
            pass
        while True:
            down = self._wishes.get()
            while not self._wishes.empty():  # only the latest wish counts
                down = self._wishes.get()
            try:
                self.apply(down)
            except Exception as e:
                _log(f"{'lowering' if down else 'restoring'} failed: {type(e).__name__}: {e}")

    # what each wish does (also called directly by the tests and restore_left_over) ----------------------------

    def _saved(self):
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _save(self, data):
        try:
            if data is None:
                self._path.unlink(missing_ok=True)
            else:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._path.write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            pass

    def apply(self, down):
        if down:
            if self._saved() is not None:
                return  # already down (a crash's left-over is restored at start)
            sp = self._speakers()
            now = sp.percent()
            target, why = lower_to(now, sp.muted(), sp.form_factor, level_setting())
            if target is None:
                _log(f"listening, volume left at {now:.0f}%: {why}")
                return
            self._save({"device": sp.id, "before": round(now, 1), "set": float(target)})
            sp.set(target)
            _log(f"listening: volume {now:.0f}% -> {target}%")
            return
        saved = self._saved()
        if saved is None:
            return
        sp = self._speakers()
        volume = sp.volume if saved.get("device") in (None, sp.id) else _speakers_by_id(saved["device"])
        if volume is None:
            _log("the device the volume was lowered on is gone; nothing to restore")
            self._save(None)
            return
        now = volume.GetMasterVolumeLevelScalar() * 100
        back = restore_to(now, saved)
        if back is None:
            _log(f"volume changed by hand to {now:.0f}% meanwhile; left there")
        else:
            volume.SetMasterVolumeLevelScalar(max(0.0, min(100.0, back)) / 100, None)
            _log(f"heard: volume {now:.0f}% -> {back:.0f}%")
        self._save(None)


_it = {"ducker": None}


def ducker():
    if _it["ducker"] is None:
        _it["ducker"] = Ducker()
    return _it["ducker"]


def restore_left_over():
    """At start (main.py): a volume a crash left down comes back. On a thread of its own."""
    d = ducker()
    if d._saved() is None:
        return

    def run():
        try:
            import comtypes

            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except Exception:
            pass
        try:
            _log("a volume left down at the last exit: restoring")
            d.apply(False)
        except Exception as e:
            _log(f"restoring at start failed: {type(e).__name__}: {e}")
    threading.Thread(target=run, daemon=True).start()
