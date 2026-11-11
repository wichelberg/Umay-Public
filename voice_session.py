"""A spoken conversation: "Hey Umay" (wake.py) starts it, she listens, answers aloud, and listens
again, until "Umay, goodbye" or IDLE_SECONDS with nothing said. The HUD's orb listens, thinks while
the answer comes and speaks with her; the light around the screen (glow.py) only with UMAY_GLOW=on.

Credits are only spent on what the owner said: "Umay, goodbye" is recognized here and ends it at
once, sending nothing; a turn with no speech in it (SilenceCut), or sound that never pauses for
LONGEST seconds (a video, not someone talking to her), is never sent; speech is transcribed only
by the local Whisper (no paid fallback here). If her voice can't say the answer, it goes to
Telegram as text. voice_guard ends the conversation on battery or when a game shows up.

While she listens (from the microphone, not in a call) the PC's volume on speakers drops to 5%
(listen_volume.py) and comes back once the owner has said their task, or when the conversation ends.

Ctrl+F3 (toggle_mute) ends a conversation going on the microphone and keeps her from listening until pressed again.
"""
import os
import threading
import time
from datetime import datetime

import voice

IDLE_SECONDS = 30      # with nothing said (the owner's choice), counted from the end of her last answer
LONGEST = 15.0         # one turn: longer without a pause is not someone talking to her
AFTER_SPEECH = 0.3     # before listening again: the speakers' tail


class Session:
    def __init__(self, answer, new_chat, transcribe, notify, activity, glow, log_dir, ended=None,
                 recorder=None, idle_seconds=None, log_suffix="voice", duck=None):
        """recorder: what records a turn (voice.Recorder: the microphone; a WhatsApp call's sound, calls.py).
        duck: listen_volume.Ducker (the PC's volume drops while she listens; not in a call)."""
        self._duck = duck
        self._answer, self._new_chat, self._transcribe = answer, new_chat, transcribe
        self._recorder_class = recorder  # None: voice.Recorder, looked up when used
        self._idle_seconds, self._log_suffix = idle_seconds, log_suffix
        self._notify, self._activity, self._glow, self._log_dir = notify, activity, glow, log_dir
        self._ended = ended or (lambda chat: None)  # the finished conversation
        self._chat = None
        self._lock = threading.Lock()
        self._on = False
        self._turn = 0
        self._recorder = None
        self.state = "off"
        self._floor = None
        self._last_speech = 0.0
        self._log_path = None
        self._hint = ""  # for the model with the owner's next answer only (say(hint=...))

    def active(self):
        return self._on

    def _current(self, turn):
        return self._on and self._turn == turn

    def level(self):
        """For the light: her voice while she speaks, the microphone while listening."""
        if self.state == "speaking":
            return voice.level()
        recorder = self._recorder
        return recorder.level() if self.state == "listening" and recorder is not None else 0.0

    def start(self, floor=None):
        """floor: the room's level the wake listener learned (so a video in the background isn't speech)."""
        with self._lock:
            if self._on or (self._recorder_class is None and voice.muted["on"]):  # a call has its own sound
                return
            self._on = True
            self._turn += 1
            turn = self._turn
        self._chat = self._new_chat()
        self._floor = floor
        self._last_speech = time.monotonic()
        self._log_path = self._log_dir / f"{datetime.now():%Y-%m-%d_%H-%M-%S}_{self._log_suffix}.txt"
        voice.warm_up()  # the voice pack loads (~3 s) while the owner says the first thing
        self._glow.show(self.level, "listen")
        self._listen(turn)

    def close(self, why=""):
        with self._lock:
            if not self._on:
                return
            self._on = False
            self._turn += 1
            recorder, self._recorder = self._recorder, None
        self.state = "off"
        if self._duck is not None:
            self._duck.up()
        if recorder is not None:
            try:
                recorder.stop()
            except Exception:
                pass
        voice.stop_playing()
        self._glow.hide()
        self._activity(None)
        self._log("—", f"(closed: {why})")
        try:
            if isinstance(self._chat, dict):
                self._chat["log_path"] = self._log_path  # for what learns from it (memory_cortex)
            self._ended(self._chat)
        except Exception as e:
            print(f"Voice: after the conversation: {e!r}")

    def _log(self, who, text):
        if self._log_path is None:
            return
        try:
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(f"{who}: {text}\n")
        except OSError:
            pass

    def _set(self, state, glow_mode, activity):
        self.state = state
        if self._duck is not None:  # down only while she listens: back up once the owner has said it
            self._duck.down() if state == "listening" else self._duck.up()
        self._glow.set_mode(glow_mode)
        self._activity(activity)

    def _listen(self, turn):
        if not self._current(turn):
            return
        self._set("listening", "listen", "listen")
        try:
            recorder = (self._recorder_class or voice.Recorder)(on_silence=lambda: self._heard(turn), floor=self._floor,
                                                                longest=LONGEST)
        except Exception as e:
            self._log("—", f"(microphone failed: {e})")
            self.close("microphone")
            return
        with self._lock:
            if self._current(turn):
                self._recorder = recorder
                return
        recorder.stop()

    def _idle_or_listen(self, turn):
        idle = IDLE_SECONDS if self._idle_seconds is None else self._idle_seconds
        if time.monotonic() - self._last_speech >= idle:
            self.close("nothing said")
        else:
            self._listen(turn)

    def _heard(self, turn):
        with self._lock:
            recorder, self._recorder = self._recorder, None
        if recorder is None or not self._current(turn):
            return
        audio = recorder.stop()
        cut = recorder.cut
        self._floor = cut.floor()
        if not audio or not cut.spoke or cut.ran_out:
            return self._idle_or_listen(turn)  # nobody talked to her: nothing to send
        self._set("thinking", "think", "think")
        try:
            text = (self._transcribe(audio) or "").strip()
        except Exception as e:
            self._log("—", f"(couldn't hear: {e})")
            text = ""
        if not self._current(turn):
            return
        if not text:
            return self._idle_or_listen(turn)
        import wake  # (wake imports voice_guard; imported late to keep this module light)

        if wake.heard_close(text):
            self._log("🎤", text)
            return self.close("Umay, goodbye")
        self._last_speech = time.monotonic()
        self._log("🎤", text)
        hint, self._hint = self._hint, ""
        answer = self._answer(self._chat, text + hint + voice.SPOKEN_HINT)
        if not self._current(turn):
            return
        self._log("Umay", answer)
        self._set("speaking", "speak", "talk")
        voice.speak(answer, on_error=lambda e: self._not_said(answer, e), on_done=lambda: self._spoken(turn))

    def say(self, text, hint=""):
        """She speaks first (a call she made: why she called; something she brings up herself), then listens.
        Her line goes into the conversation, so the owner's answer is understood as an answer to it. `hint`
        goes to the model with that answer only (e.g. "write their plans down with plan_day")."""
        with self._lock:
            if not self._on:
                return
            self._turn += 1
            turn = self._turn
            recorder, self._recorder = self._recorder, None
        self._hint = hint
        if recorder is not None:
            recorder.stop()
        if self._chat is not None:
            self._chat["messages"].append({"role": "assistant", "content": text})
        self._log("Umay", text)
        self._set("speaking", "speak", "talk")
        voice.speak(text, on_error=lambda e: self._not_said(text, e), on_done=lambda: self._spoken(turn))

    def _spoken(self, turn):
        if not self._current(turn):
            return
        self._last_speech = time.monotonic()
        time.sleep(AFTER_SPEECH)
        self._listen(turn)

    def _not_said(self, answer, error):
        self._log("—", f"(not said aloud: {error}; sent to Telegram)")
        try:
            self._notify(answer)
        except Exception:
            pass


_session = {"it": None}


def _local_transcribe(wav_bytes):
    """Only the local Whisper: an empty answer rather than a paid one."""
    if not (voice.stt_on() and voice.hears()):
        return ""
    return voice.local_stt.transcribe(wav_bytes) or ""


class _NoLight:
    """The light around the screen, off (the owner's choice, 2026-11-02: the HUD's orb shows the state)."""

    def show(self, *_):
        pass

    def hide(self):
        pass

    def set_mode(self, _):
        pass


def setup(answer, new_chat, notify, activity, log_dir, ended=None):
    """Creates the session (main.py); voice_guard ends it on battery or in a game. UMAY_GLOW=on brings the
    light around the screen back."""
    import listen_volume
    import voice_guard

    if os.environ.get("UMAY_GLOW", "").strip().lower() == "on":
        import glow as light
    else:
        light = _NoLight()
    if _session["it"] is None:
        listen_volume.restore_left_over()
        _session["it"] = Session(answer, new_chat, _local_transcribe, notify, activity, light, log_dir, ended,
                                 duck=listen_volume.ducker())
        voice_guard.listeners.append(
            lambda mode, suspect: _session["it"].close("battery, a game, or the GPU busy")
            if (mode != "ready" or suspect) and _session["it"].active() else None)
    return _session["it"]


def start():
    """Thread-safe (wake.py heard "Hey Umay")."""
    import wake

    if _session["it"] is not None:
        threading.Thread(target=_session["it"].start, args=(wake.room_level(),), daemon=True).start()


def active():
    return _session["it"] is not None and _session["it"].active()


def open_with(text, hint=""):
    """She starts a spoken conversation herself (companion.py): says `text`, then listens as after "Hey Umay".
    False if there's no session or one is already going."""
    import wake

    session = _session["it"]
    if session is None or session.active() or voice.muted["on"]:
        return False
    session.start(wake.room_level())
    session.say(text, hint)
    return True


_mute_lock = threading.Lock()


def toggle_mute():
    """She stops listening (the wake phrase and a conversation going on the microphone, which ends), or listens
    again. Returns whether she is muted now. Not kept across a restart."""
    return set_mute(not voice.muted["on"])


def set_mute(on):
    """The Ctrl+F3 panel's STOP / START LISTENING (and toggle_mute): muted or not, at once. Returns whether she is
    muted now."""
    import wake

    with _mute_lock:
        voice.muted["on"] = bool(on)
        muted = voice.muted["on"]
        try:
            wake.mute_changed()
        except Exception as e:  # no microphone right now: voice_guard's next reading tries again
            print(f"Voice: the microphone after Ctrl+F3: {e!r}")
        if muted and _session["it"] is not None:
            _session["it"].close("microphone muted")
    return muted
