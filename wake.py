"""The wake phrase: saying "Hey Umay" (or "Umay, wake up") opens the chat window and starts listening.
"Umay, eyes on" / "Umay, eyes off" turns her eyes (eyes.py: the camera moves the mouse) on or off without that.

The microphone is read on the CPU (~4.5% of one core with Windows' audio engine, measured on the
Ryzen AI 7 350). A plain loudness detector cuts out short bits of speech (MIN_SPEECH to MAX_SPEECH
seconds; the phrase takes ~1-1.5 s), and only those go to the local Whisper that voice_guard keeps
loaded, ~0.3 s on the GPU each. Nothing is ever sent to OpenRouter from here.

On only while voice_guard is in "ready" (mains power, no game, the GPU calm): on battery or when
a game shows up the microphone is closed at once. Paused while Umay speaks or 🎤 records, so
she doesn't wake herself. UMAY_WAKE=off turns it off, and Ctrl+F3 (voice.muted) closes it until pressed again.

The owner says her name the Turkish way (she is the old Turkic goddess): English Whisper wrote it "you me",
"u mai", "o mais" (wake.log, and a recording of the owner 2026-11-02). So each bit is heard with WAKE_PROMPT
as Whisper's prompt: on that recording 9 of 9 calls came out "Hey Umay." with it (8 of 9 without, the first
"Hei o mais"), and none of the owner's other sentences turned into her name. The spellings it still makes
count as her name too (NAMES, _words), only after a greeting as before.
"""
import collections
import difflib
import io
import os
import queue
import re
import threading
import time
from datetime import datetime

import numpy as np
import soundfile as sf

import eyes
import paths
import voice

RATE = 16000
BLOCK = 1024                 # 64 ms
MIN_SPEECH = 0.4             # seconds of sound in a bit worth hearing
MAX_SPEECH = 3.5             # longer: someone is talking, not calling her
END_SILENCE = 0.5            # quiet this long ends a bit
PRE_ROLL = 0.25              # kept from before the sound started (the first syllable is quiet)
# Her name and how Whisper may write it in English, from voice\wake.log and the owner's recording.
# Two-word spellings ("you may", "you me", "u mai", "o mais") are joined into one word first (_words).
NAMES = ("umay", "umai", "umey", "omay", "oomay", "humay", "youmay", "youme", "omai", "yumei", "yume", "yumi")
WAKE_PROMPT = "Hey Umay."   # Whisper's prompt for the bits (UMAY_WAKE_PROMPT changes it)
SPLIT_NAME = re.compile(r"\b(you|yu|u|o|oo|ou)\s+(may|mai|mais|me|mei|mey|my)\b")
NAME_SIMILARITY = 0.75
GREETINGS = ("hey", "hi", "okay", "ok", "hei", "ey")  # right before her name: "Hey Umay" ("hei": the owner's "hey")
WAKE_AFTER = ("wake",)                     # within two words after it: "Umay, wake up"
# Ending the spoken conversation (heard_close)
FAREWELLS = ("bye", "goodbye", "goodnight", "seeya", "cya", "farewell", "görüşürüz", "hoşçakal")
FAREWELL_JOINS = {("good", "bye"): "goodbye", ("bye", "bye"): "bye", ("good", "night"): "goodnight",
                  ("see", "you"): "seeya", ("see", "ya"): "seeya", ("hoşça", "kal"): "hoşçakal"}
AFTER_FAREWELL = ("now", "then", "later", "soon", "tomorrow", "again", "for", "love", "dear", "babe", "my", "too",
                  "thanks", "lot", "a", "much", "so", "very", "everything")
FAREWELL_REQUESTS = ("say", "says", "said", "saying", "tell", "telling", "told", "send", "sending", "message",
                     "messages", "text", "write", "writing", "type", "typing", "reply", "wish", "wave", "whatsapp",
                     "post", "comment", "song", "play", "playing", "called", "named", "title")
FAREWELL_OBJECT = ("to", "a", "the", "his", "her", "their", "our", "your", "its", "no")
STOP_SOFT = ("okay", "ok", "alright", "thanks", "thank", "you", "please", "just", "now", "oh", "so", "well", "yeah",
             "yes", "no", "fine", "enough")
END_TALK = re.compile(r"\b(stop|end|close|finish|quit|exit)\b(?: \w+){0,2}? (conversation|chat)\b"
                      r"|\b(stop|quit) (talking|listening)$")
COOLDOWN_SECONDS = 3         # after waking, or after she spoke
# No blocks from the microphone this long: the stream died without telling (2026-10-20: Windows showed
# the session inactive, nothing logged, never reopened). voice_guard's next check reopens it.
STALE_SECONDS = 10


def _setting_on():
    return os.environ.get("UMAY_WAKE", "").strip().lower() != "off"


def _words(text):
    text = re.sub(r"[^\w\s]", " ", text.lower())
    return SPLIT_NAME.sub(lambda m: m.group(1) + m.group(2), text).split()


def wake_prompt():
    return os.environ.get("UMAY_WAKE_PROMPT", "").strip() or WAKE_PROMPT


def _log(text):
    """voice_home/wake.log: on/off, and each bit heard (its text only if it looks like the phrase:
    what is said in the room otherwise isn't kept)."""
    try:
        with open(paths.voice_home() / "wake.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}\n")
    except OSError:
        pass


def _is_name(word, similarity=NAME_SIMILARITY):
    return max(difflib.SequenceMatcher(None, word, name).ratio() for name in NAMES) >= similarity


def _looks_like_it(text):
    return any(w in GREETINGS or _is_name(w, 0.6) for w in _words(text))


def _name_with(text, before=(), after=()):
    """Her name (as Whisper may spell it) right after a word in `before`, or followed within two words by
    a word starting with one in `after`."""
    words = _words(text)
    for i, word in enumerate(words):
        if _is_name(word):
            if i > 0 and words[i - 1] in before:
                return True
            if any(w.startswith(after) for w in words[i + 1:i + 3]):
                return True
    return False


def heard_wake(text):
    """Whisper's text holds the wake phrase: "Hey Umay" (hi, okay) or "Umay, wake up"."""
    return _name_with(text, before=GREETINGS, after=WAKE_AFTER)


def heard_eyes(text):
    """"on" / "off" for "Umay, eyes on" / "Umay eyes are off" (her name first), else None. English Whisper
    hears the owner's "Umay eyes" as "My eyes" (wake.log 2026-11-02: "My eyes are off."): that counts too."""
    words = _words(text)
    if not any(_is_name(w) for w in words[:3]) and words[:1] != ["my"]:
        return None
    return eyes.command(text)


def _close_name(word):
    # "you" is 0.75 like "youme" but isn't her name: "can you stop the music" must not end the conversation
    return word != "you" and _is_name(word)


def _fragments(text):
    """The words of each piece of `text` between . ! ? ; and commas, with the farewells joined into one word."""
    pieces = []
    for piece in re.split(r"[.!?;,]", text):
        words = _words(piece)
        joined = []
        for word in words:
            if joined and (joined[-1], word) in FAREWELL_JOINS:
                joined[-1] = FAREWELL_JOINS[(joined[-1], word)]
            else:
                joined.append(word)
        if joined:
            pieces.append(joined)
    return pieces


def heard_close(text):
    """The owner ends the spoken conversation (voice_session.py closes it on this, sending nothing). Inside a
    conversation she is already being talked to, so her name isn't needed (2026-11-05: "Goodbye.", "Thanks. Bye.",
    "Thank you. You are the best. Bye", "Bye, Umai." all went to the model and she said bye back, again and again):
    - it ends with a farewell: bye, goodbye, good night, see you (later), whatever follows it being only her name,
      "thanks", "now", "my love"... But not when that last piece asks for something: "tell Ahmet bye", "send him a
      message saying goodbye";
    - "stop" alone at the end ("Stop.", "Okay, stop", "Umay stop", "Oh my stop": Whisper's "Umay stop", 2026-11-03);
    - stop / end the conversation (or chat), stop talking / listening."""
    pieces = _fragments(text)
    flat = " ".join(" ".join(p) for p in pieces)
    for m in END_TALK.finditer(flat):
        before = flat[:m.start()].split()[-2:]
        if not ("not" in before or before[-1:] in (["dont"], ["never"]) or before == ["don", "t"]):
            return True
    while pieces:  # what follows the farewell: her name, "thanks", "now"...
        last = pieces[-1]
        if last[-2:] == ["thank", "you"]:
            del last[-2:]
        elif last[-1] in AFTER_FAREWELL or _close_name(last[-1]):
            last.pop()
        else:
            break
        if not last:
            pieces.pop()
    if not pieces:
        return False
    last = pieces[-1]
    word, before = last[-1], last[:-1]
    if word in FAREWELLS:
        asks = any(w in FAREWELL_REQUESTS for w in last)
        return not asks and (not before or before[-1] not in FAREWELL_OBJECT)
    if word == "stop":
        return (all(w in STOP_SOFT for w in before)
                or bool(before) and (before[-1] == "my" or _close_name(before[-1])))
    return False


class Segmenter:
    """Cuts short bits of speech out of the microphone's blocks by loudness. Pure: no audio device."""

    BACKGROUND_SECONDS = 5.0
    LOUDER = 2.5     # than the background: a voice close to the microphone
    FLOOR = 0.0015   # this microphone is quiet: a quiet room 0.0005, speech from the speakers 0.001-0.008

    def __init__(self, rate=RATE, block=BLOCK):
        self.block_seconds = block / rate
        # The room's level: the 30th percentile of the last few seconds, so a video playing in the
        # background raises it and only speech clearly louder than that (the owner, close by) counts.
        self._levels = collections.deque(maxlen=round(self.BACKGROUND_SECONDS / self.block_seconds))
        self.noise = 1e-3
        self._pre = []          # the last PRE_ROLL seconds of quiet blocks
        self._bit = None        # blocks of the bit being heard
        self._too_long = False
        self._quiet = 0
        self._loud = 0

    def threshold(self):
        return max(self.LOUDER * self.noise, self.FLOOR)

    def feed(self, block):
        """Returns a finished bit (float32 array) worth hearing, or None."""
        rms = float(np.sqrt(np.mean(block ** 2))) + 1e-9
        loud = rms > self.threshold()
        # The level is frozen while a bit is heard (else a long sound turns into "background" and ends
        # the bit with a pause that isn't there), but follows again once it is too long to be the phrase.
        if self._bit is None or self._too_long:
            self._levels.append(rms)
            if len(self._levels) >= self._levels.maxlen // 2:
                self.noise = float(np.percentile(self._levels, 30))
        if len(self._levels) < self._levels.maxlen // 2:
            return None  # just started listening: learning the room first
        if self._bit is None:
            self._pre = (self._pre + [block])[-max(1, round(PRE_ROLL / self.block_seconds)):]
            self._loud = self._loud + 1 if loud else 0
            if self._loud >= 2:  # 128 ms: not a click
                self._bit, self._quiet = list(self._pre), 0
            return None
        self._quiet = 0 if loud else self._quiet + 1
        if not self._too_long:
            self._bit.append(block)
            if len(self._bit) * self.block_seconds > MAX_SPEECH + END_SILENCE + PRE_ROLL:
                self._bit, self._too_long = [], True  # someone talking: drop it all until a pause
        if self._quiet * self.block_seconds < END_SILENCE:
            return None
        bit, too_long = self._bit, self._too_long
        self._bit, self._too_long, self._loud, self._pre = None, False, 0, []
        seconds = (len(bit) - self._quiet) * self.block_seconds - PRE_ROLL
        return np.concatenate(bit) if not too_long and MIN_SPEECH <= seconds <= MAX_SPEECH else None


def _wav_bytes(audio):
    out = io.BytesIO()
    sf.write(out, audio, RATE, format="WAV", subtype="PCM_16")
    return out.getvalue()


class WakeListener:
    """Listens for the wake phrase while enabled; on_wake() is called (from a background thread)."""

    def __init__(self, on_wake, paused=lambda: False, transcribe=None, on_eyes=None):
        self.on_wake, self.on_eyes = on_wake, on_eyes
        self.paused = paused          # e.g. 🎤 is recording
        self._transcribe = transcribe or (lambda wav: voice.local_stt.transcribe(wav, prompt=wake_prompt()))
        self._stream = None
        self._wanted = False          # what voice_guard last asked for: where Ctrl+F3 goes back to
        self._lock = threading.Lock()
        self._bits = queue.Queue(maxsize=2)  # more than that: drop, Whisper is still busy
        self._segmenter = Segmenter()
        self._quiet_until = 0.0
        self._last_audio = 0.0
        self._callback_failed = False
        threading.Thread(target=self._hear, daemon=True).start()

    def enabled(self):
        return self._stream is not None

    def _dead(self):
        """The open stream stopped giving audio (a device hiccup, or PortAudio ended it)."""
        stream = self._stream
        return stream is not None and (not getattr(stream, "active", True)
                                       or time.monotonic() - self._last_audio > STALE_SECONDS)

    def set_enabled(self, on):
        """Opens or closes the microphone (safe to call every few seconds); reopens it if it died."""
        self._wanted = on
        on = on and _setting_on() and not voice.muted["on"]
        with self._lock:
            if on and self._dead():
                _log("the microphone stopped giving audio: reopening it")
                self._close()
            if on and self._stream is None:
                import sounddevice

                self._segmenter = Segmenter()
                self._last_audio = time.monotonic()
                stream = sounddevice.InputStream(samplerate=RATE, channels=1, dtype="float32", blocksize=BLOCK,
                                                 callback=self._on_audio)
                stream.start()
                self._stream = stream
                _log("listening")
            elif not on and self._stream is not None:
                self._close()
                _log("off (muted)" if voice.muted["on"] else "off (battery, a game, or not ready)")

    def _close(self):
        stream, self._stream = self._stream, None
        try:
            stream.abort()
            stream.close()
        except Exception as e:  # a dead device may refuse; the stream is dropped anyway
            _log(f"closing the microphone: {type(e).__name__}: {e}")

    def _busy(self):
        if voice.speaking() or self.paused():
            self._quiet_until = time.monotonic() + COOLDOWN_SECONDS
            return True
        return time.monotonic() < self._quiet_until

    def _on_audio(self, data, frames, when, status):
        self._last_audio = time.monotonic()
        try:  # an exception here would make PortAudio end the stream without a word
            bit = self._segmenter.feed(data[:, 0].copy())
            if bit is not None and not self._busy():
                self._bits.put_nowait(bit)
        except queue.Full:
            pass
        except Exception as e:
            if not self._callback_failed:
                self._callback_failed = True
                _log(f"microphone block failed: {type(e).__name__}: {e}")

    def _hear(self):
        while True:
            bit = self._bits.get()
            # only the model voice_guard keeps loaded: never load it (or anything paid) for this
            if not self.enabled() or self._busy():
                continue
            if not (voice.stt_on() and voice.local_stt.loaded()):
                _log(f"{len(bit) / RATE:.1f} s bit skipped: the local Whisper isn't loaded")
                continue
            started = time.monotonic()
            try:
                text = self._transcribe(_wav_bytes(bit)) or ""
            except Exception as e:
                _log(f"couldn't hear: {e}")
                continue
            woke = heard_wake(text) and self.enabled() and not self._busy()
            look = None if woke or self.on_eyes is None else heard_eyes(text)
            _log(f"{len(bit) / RATE:.1f} s bit, heard in {time.monotonic() - started:.2f} s"
                 + (f": {text!r}" if _looks_like_it(text) else "") + (" -> WAKE" if woke else "")
                 + (f" -> EYES {look.upper()}" if look else ""))
            if look:
                self._quiet_until = time.monotonic() + COOLDOWN_SECONDS
                threading.Thread(target=self.on_eyes, args=(look,), daemon=True).start()
            if woke:
                self._quiet_until = time.monotonic() + COOLDOWN_SECONDS
                try:
                    self.on_wake()
                except Exception as e:
                    print(f"Wake: couldn't open: {e}")


_listener = {"it": None}


def room_level():
    """The room's loudness the listener has learned (voice_session.py starts from it), or None."""
    it = _listener["it"]
    if it is None or not it.enabled() or len(it._segmenter._levels) < it._segmenter._levels.maxlen // 2:
        return None
    return it._segmenter.noise


def mute_changed():
    """Ctrl+F3 was pressed (voice.muted): closes the microphone at once, or opens it again if voice_guard wants it."""
    it = _listener["it"]
    if it is not None:
        it.set_enabled(it._wanted)


def start(on_wake, paused=lambda: False, on_eyes=None):
    """Creates the listener and hands it to voice_guard, which turns it on and off (main.py). on_eyes("on" /
    "off"): "Umay, eyes on / off" was said."""
    import voice_guard

    if _listener["it"] is None:
        _listener["it"] = WakeListener(on_wake, paused, on_eyes=on_eyes)
        voice_guard.listeners.append(lambda mode, suspect: _listener["it"].set_enabled(mode == "ready" and not suspect))
