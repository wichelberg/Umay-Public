"""Yes/no questions on the PC, asked aloud: before Umay changes something on the PC she says what she is
about to do and listens for the answer (the local Whisper only, nothing paid). The owner's choice
(2026-11-02): no text on the screen for these; a request that came from Telegram is asked on Telegram
(pc_tools.channel) and never here.

An answer that isn't clearly yes or no is asked once more; then, or with nothing said, it is a no.
ask() returns None when her voice can't ask now (a game, no TTS or Whisper, the microphone failing):
the caller decides what then.
"""
import re
import threading
import time
from pathlib import Path

import hud
import voice

YES = {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "alright", "affirmative", "confirm", "confirmed",
       "correct", "absolutely", "definitely", "please"}
YES_PHRASES = ("go ahead", "do it", "of course", "sounds good")
SAME_AS_YES = ("no problem", "why not")  # a yes despite the no in it
SAME_AS_NO = ("do not do it", "don't do it", "dont do it")  # a no despite the "do it" in it
NO = {"no", "nope", "nah", "don't", "dont", "cancel", "stop", "negative", "never", "wait", "not"}
NO_PHRASES = ("do not", "hold on", "not now")
ANSWER_SECONDS = 8    # listening for the answer at most this long
SPEAK_TIMEOUT = 120
TRIES = 2
AGAIN = "Sorry, I didn't catch that. Yes or no?"

_lock = threading.Lock()  # one question at a time


def parse(text):
    """True for a yes, False for a no, None when it isn't clear (both, or neither)."""
    text = (text or "").lower().replace("’", "'")
    for phrase in SAME_AS_NO:
        text = text.replace(phrase, "no")
    for phrase in SAME_AS_YES:
        text = text.replace(phrase, "yes")
    words = set(re.findall(r"[a-z']+", text))
    yes = bool(words & YES) or any(p in text for p in YES_PHRASES)
    no = bool(words & NO) or any(p in text for p in NO_PHRASES)
    if yes == no:
        return None
    return yes


def spoken(question):
    """The question as she says it: a path becomes its last part (a file's name), symbols go."""
    lines = []
    for line in question.splitlines():
        match = re.search(r"[A-Za-z]:\\", line)
        if match:
            path = line[match.start():].strip().rstrip(".,;:)\"'")
            line = line[:match.start()] + (Path(path).name or path)
        lines.append(line)
    text = re.sub(r"[^\w\s.,:;!?'\"()%&/+-]", " ", " ".join(lines))
    return re.sub(r"\s+", " ", text).strip()[:400]


def available():
    return voice.tts_on() and voice.stt_on() and not voice.muted["on"]


def _say(text):
    """Reads text aloud and waits until it has been played. False if her voice couldn't say it."""
    done, failed = threading.Event(), []
    voice.speak(text, on_error=lambda e: (failed.append(e), done.set()), on_done=done.set)
    return done.wait(SPEAK_TIMEOUT) and not failed


def _listen():
    """The answer's audio (wav bytes), or None if nothing was said."""
    heard = threading.Event()
    recorder = voice.Recorder(on_silence=heard.set, longest=ANSWER_SECONDS)
    end = time.monotonic() + ANSWER_SECONDS + 2
    while not heard.wait(0.1) and time.monotonic() < end and not voice.muted["on"]:  # Ctrl+F3 closes it at once
        pass
    wav = recorder.stop()
    return wav if recorder.cut is not None and recorder.cut.spoke and not voice.muted["on"] else None


def ask(question):
    """Asks aloud; True / False, or None if her voice can't ask now."""
    if not available():
        return None
    with _lock:
        before = hud.current()
        try:
            for attempt in range(TRIES):
                hud.activity("talk")
                if not _say(spoken(question) if attempt == 0 else AGAIN):
                    return None
                hud.activity("listen")
                try:
                    wav = _listen()
                except Exception as e:  # no microphone
                    print(f"Voice confirm: listening failed: {e}")
                    return None
                if voice.muted["on"]:
                    return None
                if wav is None:
                    continue
                if not voice.local_stt.loaded():
                    voice.local_stt.start()
                answer = parse(voice.local_stt.transcribe(wav))
                if answer is not None:
                    return answer
            return False
        finally:
            hud.activity(before)
