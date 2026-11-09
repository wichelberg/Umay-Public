"""Text prep for the local TTS: what it should say, sentence by sentence.

Umay speaks English through Piper, which reads digits and names itself: prepare() only tidies the
spaces for it. The Turkish steps below run for Chatterbox (language "tr"):
- Numbers become Turkish words, since the model reads digits badly ("17:30" came out as
  "yün yeti müsırda"): times, percentages, decimals, plain numbers. A suffix written after an
  apostrophe already follows how the number is said, so it is just joined on: "17:30'da" ->
  "on yedi otuzda", "5'te" -> "beşte".
- English names Umay says often are respelled the way they're said in Turkish
  ("Claude Code" -> "Klod Kod"); PRONOUNCE holds them.
- sentences() cuts a reply into pieces read one by one, so the first can play while the
  next is made. Very short pieces are joined to the next one (choppy otherwise) and very long
  ones are cut at commas (the model drifts on long inputs). The first piece is kept short so
  the first sound comes early: Chatterbox takes ~2.5 s for 3-4 words and ~4 s for 10.
"""
import re

ONES = ["", "bir", "iki", "üç", "dört", "beş", "altı", "yedi", "sekiz", "dokuz"]
TENS = ["", "on", "yirmi", "otuz", "kırk", "elli", "altmış", "yetmiş", "seksen", "doksan"]
SCALES = [(10 ** 12, "trilyon"), (10 ** 9, "milyar"), (10 ** 6, "milyon"), (1000, "bin")]

# Said the Turkish way. Longer names first (re tries the alternatives in order).
PRONOUNCE = {
    "Claude Code": "Klod Kod", "Claude": "Klod", "VS Code": "Vi Es Kod", "GitHub": "Githab",
    "YouTube": "Yutub", "WhatsApp": "Vatsap", "Spotify": "Spotifay", "Discord": "Diskord",
    "OpenRouter": "Open Rauter", "Python": "Paytın", "Opera GX": "Opera Ci İks", "Steam": "Stim",
    "Wichelberg": "Vihelberg", "Windows": "Vindovs", "Chrome": "Krom", "Google": "Gugıl",
    "GPU": "ci pi yu", "CPU": "si pi yu", "AI": "ey ay", "TTS": "ti ti es", "STT": "es ti ti",
}
MIN_CHARS = 25
MAX_CHARS = 220
FIRST_WORDS = (3, 8)  # the first piece: cut at a comma in this range of words if it is longer,
FIRST_CUT = 7         # else after this many words
GROWTH = 1.5          # each piece is made while the one before plays: at most this many times as long


def number_words(n):
    """Non-negative integer -> Turkish words ("bin iki yüz elli"; "bir bin" is just "bin")."""
    if n == 0:
        return "sıfır"
    words = []
    for scale, name in SCALES:
        if n >= scale:
            count, n = divmod(n, scale)
            if not (scale == 1000 and count == 1):
                words.append(number_words(count))
            words.append(name)
    hundreds, n = divmod(n, 100)
    if hundreds:
        words += ([ONES[hundreds]] if hundreds > 1 else []) + ["yüz"]
    tens, ones = divmod(n, 10)
    words += [w for w in (TENS[tens], ONES[ones]) if w]
    return " ".join(words)


def _number(text):
    """A number as written ("1.250", "3,4", "2026") -> words."""
    if re.fullmatch(r"\d{1,3}(\.\d{3})+", text):  # 1.250.000: dots group thousands in Turkish
        return number_words(int(text.replace(".", "")))
    whole, frac = re.fullmatch(r"(\d+)(?:[.,](\d+))?", text).groups()
    words = number_words(int(whole))
    if frac:  # read digit groups as written: 3,05 -> üç virgül sıfır beş
        zeros = len(frac) - len(frac.lstrip("0"))
        words += " virgül " + " ".join(["sıfır"] * zeros + ([number_words(int(frac))] if frac.strip("0") else []))
    return words


def _suffix(match):
    return match.group("suffix") or ""


def say_numbers(text):
    """Digits -> Turkish words, keeping suffixes written after an apostrophe."""
    suffix = r"(?:['’](?P<suffix>[a-zçğıöşü]+))?"
    text = re.sub(r"\b(?P<h>\d{1,2}):(?P<m>\d{2})\b" + suffix,  # 17:30'da (a time)
                  lambda m: f"{number_words(int(m.group('h')))} "
                            f"{number_words(int(m.group('m'))) if int(m.group('m')) else 'sıfır sıfır'}{_suffix(m)}",
                  text)
    text = re.sub(r"%\s?(?P<n>\d+(?:[.,]\d+)?)" + suffix,  # %30'u
                  lambda m: f"yüzde {_number(m.group('n'))}{_suffix(m)}", text)
    text = re.sub(r"\b(?P<n>\d{1,3}(?:\.\d{3})+(?!\d)|\d+(?:[.,]\d+)?)" + suffix,
                  lambda m: f"{_number(m.group('n'))}{_suffix(m)}", text)
    return text


def respell(text):
    names = sorted(PRONOUNCE, key=len, reverse=True)
    pattern = r"\b(" + "|".join(re.escape(n) for n in names) + r")(?=['’]|\b)['’]?"
    return re.sub(pattern, lambda m: PRONOUNCE[m.group(1)], text)


def prepare(text, language="en"):
    """What the TTS should read for `text` (already cleaned by voice.speakable), in `language`."""
    if language == "tr":
        text = say_numbers(respell(text))
    return re.sub(r"\s+", " ", text).strip()


def sentences(text):
    """`text` cut into pieces to read one after another."""
    pieces = [p.strip() for p in re.split(r"(?<=[.!?…])\s+|\n+", text) if p.strip()]
    long_cut = []
    for piece in pieces:
        while len(piece) > MAX_CHARS:
            cut = piece.rfind(", ", 0, MAX_CHARS)
            cut = cut + 1 if cut > MIN_CHARS else piece.rfind(" ", 0, MAX_CHARS)
            if cut <= 0:
                break
            long_cut.append(piece[:cut].strip())
            piece = piece[cut:].strip()
        long_cut.append(piece)
    joined = []
    for piece in long_cut:
        if joined and len(joined[-1]) < MIN_CHARS:
            joined[-1] = f"{joined[-1]} {piece}"
        else:
            joined.append(piece)
    if len(joined) > 1 and len(joined[-1]) < MIN_CHARS:  # a short last one goes with the one before
        joined[-2:] = [f"{joined[-2]} {joined[-1]}"]
    return _quick_start(joined)


def _quick_start(pieces):
    """The first piece cut short: at its first comma (or sentence end: a short sentence was joined
    to the next) after FIRST_WORDS words, else after FIRST_CUT words if it is longer than that. What
    is left of it is read next on its own. Then every piece much longer than the one before is cut
    at a comma (Chatterbox makes a piece in ~0.6-0.9x its length, so it has to be ready by the time
    the one before has been played)."""
    if not pieces:
        return pieces
    words = pieces[0].split()
    low, high = FIRST_WORDS
    cut = next((n for n in range(low, min(high, len(words) - low) + 1)
                if words[n - 1].endswith((",", ";", ":", ".", "!", "?", "…"))), None)
    if cut is None and len(words) > high:
        cut = min(FIRST_CUT, len(words) - low)
    if cut is not None:
        pieces = [" ".join(words[:cut]), " ".join(words[cut:]), *pieces[1:]]
    grown = pieces[:1]
    for piece in pieces[1:]:
        while len(piece) > GROWTH * len(grown[-1]):
            commas = [m.start() for m in re.finditer(", ", piece)
                      if m.start() + 1 >= MIN_CHARS and len(piece) - m.end() >= MIN_CHARS]
            if not commas:
                break
            comma = max((c for c in commas if c < GROWTH * len(grown[-1])), default=commas[0])
            grown.append(piece[:comma + 1])
            piece = piece[comma + 2:]
        grown.append(piece)
    return grown
