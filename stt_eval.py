"""Speech-to-text scoring and data prep for train_stt.py, without torch (so tests can run it).

- wer(): word error rate after Turkish-aware normalizing, also ignoring numbers (Whisper
  writes "15" where a reference says "on beş": not really a mistake).
- split_by_sentence(): owner's samples into train/test by sentence, stable across runs.
- cut_points() + assign_words(): a clip longer than Whisper's 30 s window is cut at its
  quietest moments, and its confirmed text is shared out over the pieces by aligning it
  with what the base model heard in each piece.
- reading_sections() + user_voice_files(): the read-aloud texts (voice_reading_tr.txt)
  and the owner's recordings of them in UserVoice (N_<device>.<ext>).
"""
import difflib
import re
import zlib
from pathlib import Path

import numpy as np

AUDIO_EXTENSIONS = {".m4a", ".mp3", ".wav", ".ogg", ".oga", ".opus", ".flac", ".aac", ".webm", ".mp4", ".3gp", ".amr"}
_RECORDING_NAME = re.compile(r"^(\d+)(?:[ _.-]+(.*))?$")

NUMBER_WORDS = re.compile(r"\b(bir|iki|üç|dört|beş|altı|yedi|sekiz|dokuz|on|yirmi|otuz|kırk|elli|altmış|yetmiş|"
                          r"seksen|doksan|yüz|bin|milyon|virgül)\b", re.I)


def norm_text(text):
    """Lowercase (Turkish I/İ), no circumflex, apostrophes or punctuation."""
    text = text.replace("I", "ı").replace("İ", "i").lower().translate(str.maketrans("âîû", "aiu"))
    text = re.sub(r"['’]", "", text)
    return " ".join(re.sub(r"[^\w\s]", " ", text).split())


def edits(a, b):
    """Word-level edit distance between two word lists."""
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def _words(text, ignore_numbers):
    words = norm_text(text).split()
    if ignore_numbers:
        words = [w for w in words if not NUMBER_WORDS.fullmatch(w) and not re.search(r"\d", w)]
    return words


def wer(pairs):
    """{"wer", "wer_ignoring_numbers", "clips"} for (reference, heard) pairs, in percent.
    Ignoring numbers drops number words and digits on both sides, so "15" vs "on beş" isn't
    counted. (Dropping whole sentences with numbers instead fails on long recordings:
    nearly every Turkish sentence has "bir".)"""
    def rate(ignore_numbers):
        words = sum(len(_words(ref, ignore_numbers)) for ref, _ in pairs)
        errors = sum(edits(_words(ref, ignore_numbers), _words(hyp, ignore_numbers)) for ref, hyp in pairs)
        return round(100 * errors / words, 2) if words else None
    return {"wer": rate(False), "wer_ignoring_numbers": rate(True), "clips": len(pairs)}


def split_by_sentence(samples, test_share):
    """(train, test): a stable hash of the sentence decides, so a sentence read twice never
    lands on both sides and the split stays the same as data grows."""
    train, test = [], []
    for sample in samples:
        bucket = zlib.crc32(norm_text(sample["text"]).encode()) % 1000
        (test if bucket < test_share * 1000 else train).append(sample)
    return train, test


def cut_points(audio, rate, max_seconds=28.0, min_seconds=8.0, window_seconds=0.25):
    """Sample indexes to cut `audio` at so every piece is at most max_seconds long: each cut is
    the quietest window between min_seconds and max_seconds after the previous one."""
    window = max(1, int(window_seconds * rate))
    cuts, start = [], 0
    while len(audio) - start > max_seconds * rate:
        lo, hi = start + int(min_seconds * rate), start + int(max_seconds * rate)
        frames = audio[lo:hi][: (hi - lo) // window * window].reshape(-1, window)
        quietest = int(np.argmin((frames ** 2).mean(axis=1)))
        cut = lo + quietest * window + window // 2
        cuts.append(cut)
        start = cut
    return cuts


SKIPPED_RUN = 3  # this many reference words in a row with nothing heard = not read aloud


def assign_words(reference, heard_pieces, min_similarity=0.6):
    """Shares the reference text's words out over the pieces, following where the matching
    heard words are. Returns one text per piece, or None for a piece that can't be trusted
    (better dropped than trained on a wrong label):
    - a run of SKIPPED_RUN+ reference words with nothing heard for them was skipped while
      reading (or the recording started late): left out, or the model would learn to
      "hear" words nobody said. Shorter gaps stay: those are words the model missed.
    - a run of SKIPPED_RUN+ heard words that aren't in the text (an aside, a restart)
      makes its piece unsure.
    - a piece whose share doesn't look like what was heard in it."""
    ref_words = reference.split()
    ref_norm = [norm_text(w) for w in ref_words]
    heard, owner = [], []
    for index, piece in enumerate(heard_pieces):
        words = norm_text(piece).split()
        heard += words
        owner += [index] * len(words)
    if not heard:
        return [None] * len(heard_pieces)

    # Each reference word goes to the piece of the heard word it lines up with.
    piece_of = [None] * len(ref_words)
    skipped, unsure = set(), set()
    matcher = difflib.SequenceMatcher(None, ref_norm, heard, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        extra_ref, extra_heard = (i2 - i1) - (j2 - j1), (j2 - j1) - (i2 - i1)
        # "on iki bin beş yüz" is heard as "12500": not a skip or an aside.
        numbers = (any(NUMBER_WORDS.fullmatch(w) for w in ref_norm[i1:i2])
                   or any(re.search(r"\d", w) for w in heard[j1:j2]))
        if tag == "delete" and i2 - i1 >= SKIPPED_RUN:  # nothing heard at all: no digits to excuse it
            skipped.update(range(i1, i2))
            continue
        if not numbers and (extra_heard >= SKIPPED_RUN or (tag == "replace" and extra_ref >= SKIPPED_RUN)):
            unsure.update(owner[j] for j in range(j1, j2))
        for k, i in enumerate(range(i1, i2)):
            if j2 > j1:
                piece_of[i] = owner[min(j2 - 1, j1 + k * (j2 - j1) // max(1, i2 - i1))]
    for i in range(len(piece_of)):  # short gaps follow the word before
        if piece_of[i] is None and i not in skipped:
            earlier = [piece_of[k] for k in range(i - 1, -1, -1) if piece_of[k] is not None]
            piece_of[i] = earlier[0] if earlier else next((p for p in piece_of if p is not None), 0)

    texts = []
    for index, piece in enumerate(heard_pieces):
        text = " ".join(w for w, p in zip(ref_words, piece_of) if p == index)
        # Word by word, and without autojunk: on long strings it drops frequent items (spaces,
        # common letters) and the ratio collapses for pieces that match fine.
        similarity = difflib.SequenceMatcher(None, _words(text, True), _words(piece, True), autojunk=False).ratio()
        texts.append(text if text and index not in unsure and similarity >= min_similarity else None)
    return texts


def reading_sections(text):
    """{section number: its text} from the read-aloud file: "### N" starts a section,
    lines starting with # are comments."""
    sections, current = {}, None
    for line in text.splitlines():
        header = re.match(r"^###\s*(\d+)\s*$", line.strip())
        if header:
            current = int(header.group(1))
            sections[current] = []
        elif current is not None and line.strip() and not line.lstrip().startswith("#"):
            sections[current].append(line.strip())
    return {number: " ".join(lines) for number, lines in sections.items() if lines}


def user_voice_files(folder):
    """[(path, section number, device)] for recordings named N_<device>.<ext> (e.g. 3_telefon.m4a,
    or just 3.m4a), sorted. Other files are ignored."""
    found = []
    for path in sorted(Path(folder).iterdir()) if Path(folder).is_dir() else []:
        match = _RECORDING_NAME.match(path.stem.strip())
        if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS and match:
            found.append((path, int(match.group(1)), (match.group(2) or "kayit").strip().lower()))
    return found
