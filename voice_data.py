"""Speech samples for fine-tuning Whisper on the owner's voice: audio plus the right text.

Only clips whose text the owner confirmed (a Telegram voice note answered "Yes", a 🎤
transcript marked ✓ or corrected with ✎) or read aloud in voice_recorder.py are kept.
They live in paths.voice_home()/data, never in the repo:
  clips/<id>.flac   the audio as it was heard (mono, its own sample rate)
  samples.jsonl     one JSON line per clip: id, audio (relative path), text (right),
                    heard (what speech-to-text made of it, or null), source
                    (telegram | mic | reading), seconds, created

.env: UMAY_VOICE_DATA=off stops saving.
"""
import io
import json
import os
import threading
import uuid
from datetime import datetime

import soundfile as sf

import paths

_lock = threading.Lock()


def enabled():
    return os.environ.get("UMAY_VOICE_DATA", "").strip().lower() != "off"


def data_dir():
    return paths.voice_home() / "data"


def save(wav_bytes, text, source, heard=None):
    """Keeps one confirmed clip. Returns its id, or None if saving is off or there's nothing to keep."""
    text = (text or "").strip()
    if not enabled() or not wav_bytes or not text:
        return None
    audio, rate = sf.read(io.BytesIO(wav_bytes), dtype="float32", always_2d=True)
    audio = audio.mean(axis=1)
    if not len(audio):
        return None
    clip_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    folder = data_dir()
    (folder / "clips").mkdir(parents=True, exist_ok=True)
    sf.write(folder / "clips" / f"{clip_id}.flac", audio, rate, format="FLAC", subtype="PCM_16")
    entry = {
        "id": clip_id, "audio": f"clips/{clip_id}.flac", "text": text, "heard": heard, "source": source,
        "seconds": round(len(audio) / rate, 2), "created": datetime.now().isoformat(timespec="seconds"),
    }
    with _lock, open(folder / "samples.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return clip_id


def samples():
    """Every saved sample, oldest first (lines that don't parse are skipped)."""
    path = data_dir() / "samples.jsonl"
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries
