"""Which markets the MARKETS panel shows, changed by voice or text: "add Apple to the markets", "remove Tesla from
the markets", "Aselsan'ı borsaya ekle", "Bitcoin'i borsadan çıkar". The HUD reads the quotes again at once.

- The list goes to hud_feeds.MARKETS_FILE (%LOCALAPPDATA%\\Umay); from the first change on it is used instead of
  UMAY_MARKET_SYMBOLS. A new symbol goes to the end: the panel has room for about PANEL_ROWS, its detail view
  for all of them.
- A name becomes Yahoo's symbol: one Umay knows (ALIASES, hud_feeds.NAMES: "gold", "altın", "dolar"), else Yahoo
  Finance's search, whose answer counts only when it is the symbol said ("NVDA", "THYAO" for THYAO.IS) or its
  name has every word said ("gümüş" found General Motors, "Turkish Airlines" nothing: 2026-11-04).
- asked() catches the plain ways of saying it before any model sees it, like news_follow.py; the follow_market
  tool covers the rest. Both need the markets named ("add Apple" alone could be anything).
"""
import re
import unicodedata

import requests

import hud
import hud_feeds
from news_follow import ADDRESS, FILLER, POLITE, SPOKEN_TAIL, SUFFIX

PANEL_ROWS = 11
SEARCH_URL = "https://query1.finance.yahoo.com/v1/finance/search"
KINDS = {"EQUITY", "ETF", "INDEX", "CURRENCY", "CRYPTOCURRENCY", "FUTURE", "MUTUALFUND"}
ALIASES = {
    "gold": "GC=F", "altın": "GC=F", "silver": "SI=F", "gümüş": "SI=F", "oil": "CL=F", "petrol": "CL=F",
    "brent": "BZ=F", "dollar": "USDTRY=X", "dolar": "USDTRY=X", "usd": "USDTRY=X", "euro": "EURTRY=X",
    "avro": "EURTRY=X", "eur": "EURTRY=X", "pound": "GBPTRY=X", "sterlin": "GBPTRY=X", "gbp": "GBPTRY=X",
    "btc": "BTC-USD", "eth": "ETH-USD", "bist": "XU100.IS", "borsa istanbul": "XU100.IS", "thy": "THYAO.IS",
    "turkish airlines": "THYAO.IS", "türk hava yolları": "THYAO.IS", "s&p": "^GSPC", "dow": "^DJI",
    "google": "GOOGL",
}
NOISE = re.compile(r"(?<!\S)(?:the|stock|stocks|shares|share|price|hisse|hissesi|hisseleri|fiyati|lira|tl|try|kuru|"
                   r"kur)(?!\S)")  # matched on _plain text: no accents, ı as i
MARKET_EN = r"(?:the\s+|my\s+)?(?:markets?|stocks?|watch\s*list)(?:\s+panel)?"
ADD_EN = re.compile(rf"^{POLITE}(?:add|follow|track|watch|put)\s+(?P<name>.+?)\s+(?:to|in|on|into)\s+{MARKET_EN}\W*$",
                    re.I | re.S)
REMOVE_EN = re.compile(rf"^{POLITE}(?:remove|drop|delete|unfollow|stop\s+(?:following|tracking|watching))\s+"
                       rf"(?P<name>.+?)\s+(?:from|in|on|off)\s+{MARKET_EN}\W*$", re.I | re.S)
MARKET_TR = r"(?:borsa\w*|piyasa\w*|markets?['’]?\w*)"
HAS_MARKET_TR = re.compile(rf"(?<!\S){MARKET_TR}", re.I)
FROM_MARKET_TR = re.compile(r"(?<!\S)(?:borsa\w*d[ae]n|piyasa\w*d[ae]n|markets?['’]?t[ae]n)(?!\S)", re.I)
ADD_TR = re.compile(r"\b(?:ekle|takip\s+et)\w*", re.I)
REMOVE_TR = re.compile(r"\b(?:takip(?:ten)?\s+çıkar|çıkar|sil|kaldır)\w*", re.I)
MARKET_WORD = re.compile(r"(?<!\S)(?:borsa\w+|piyasa\w*|markets?|stocks?)(?!\S)", re.I)  # bare "Borsa" stays: Borsa İstanbul
HAS_NEWS_TR = re.compile(r"\bhaber", re.I)


def asked(text):
    """("follow" | "unfollow", name) when the owner plainly asks to change the markets shown, else None."""
    text = ADDRESS.sub("", SPOKEN_TAIL.sub("", text or "")).strip()
    m = REMOVE_EN.match(text)
    if m:
        return _found("unfollow", m.group("name"))
    m = ADD_EN.match(text)
    if m:
        return _found("follow", m.group("name"))
    if HAS_NEWS_TR.search(text) or not HAS_MARKET_TR.search(text):
        return None  # "Borsa İstanbul'u haberlerde takip et" is news_follow's
    m = REMOVE_TR.search(text)
    if m and FROM_MARKET_TR.search(text):
        return _found("unfollow", text[:m.start()] + " " + text[m.end():])
    m = ADD_TR.search(text)
    if m:
        return _found("follow", text[:m.start()] + " " + text[m.end():])
    return None


def _found(action, raw):
    name = MARKET_WORD.sub(" ", SUFFIX.sub("", raw or ""))
    name = re.sub(r"\s+", " ", FILLER.sub(" ", name)).strip(" ,.:;!?-\"'")
    return (action, name) if name and len(name.split()) <= 5 else None


def _plain(text):
    """Lower case, no accents, no punctuation: "Türk Hava Yolları" -> "turk hava yollari"."""
    text = unicodedata.normalize("NFKD", str(text).lower().replace("ı", "i"))
    return re.sub(r"[^\w&]+", " ", "".join(c for c in text if not unicodedata.combining(c))).strip()


def _key(name):
    return re.sub(r"\s+", " ", NOISE.sub(" ", _plain(name))).strip()


def known(name):
    """The symbol for a name Umay knows without asking Yahoo, else None."""
    key = _key(name)
    aliases = {_key(a): s for a, s in ALIASES.items()}
    aliases.update({_key(n): s for s, n in hud_feeds.NAMES.items()})
    if key in aliases:
        return aliases[key]
    raw = name.strip().upper()
    return raw if raw in hud_feeds.NAMES else None


def short_name(name):
    """Yahoo's "Apple Inc." -> "APPLE" for the panel."""
    name = str(name).upper().strip()
    while True:
        cut = re.sub(r"[,.]?\s+(?:INC|CORP|CORPORATION|CO|COMPANY|LTD|PLC|N\.?V|S\.?A|A\.?S|AG|SE|HOLDINGS?|"
                     r"GROUP|CLASS [A-C])\.?$", "", name)
        if cut == name:
            return name[:22].strip()
        name = cut


def pick(said, quotes):
    """Yahoo's search answer -> (symbol, name) of the first quote that is what was said, else None (pure)."""
    words = _key(said).split()
    raw = said.strip().upper()
    for q in quotes:
        symbol = str(q.get("symbol") or "").upper()
        if not symbol or q.get("quoteType") not in KINDS:
            continue
        name = q.get("shortname") or q.get("longname") or symbol
        hay = _key(f"{q.get('shortname') or ''} {q.get('longname') or ''}").split()
        if raw in (symbol, symbol.split(".")[0]) or (words and all(w in hay for w in words)):
            return symbol, short_name(name)
    return None


def _search(name):
    r = requests.get(SEARCH_URL, headers=hud_feeds.HEADERS, timeout=hud_feeds.TIMEOUT,
                     params={"q": name, "quotesCount": 8, "newsCount": 0, "listsCount": 0})
    r.raise_for_status()
    return r.json().get("quotes") or []


def resolve(name):
    """(symbol, name for the panel), or None when Yahoo doesn't know it. Raises requests' errors."""
    symbol = known(name)
    if symbol:
        return symbol, hud_feeds.name_of(symbol)
    return pick(name, _search(name))


def _current():
    saved = hud_feeds.saved_markets()
    if saved is not None:
        return saved["symbols"], dict(saved["names"])
    return hud_feeds.market_symbols(), {}


def _listing(symbols):
    if not symbols:
        return "The markets panel is empty."
    return "The markets panel shows: " + ", ".join(f"{hud_feeds.name_of(s)} ({s})" for s in symbols) + "."


def _refresh():
    hud.tell({"refresh": "markets"})


def follow(name):
    name = name.strip(" ,.\"'")
    if not name:
        return "Which market should I add?"
    try:
        found = resolve(name)
    except (requests.RequestException, ValueError):
        return "I couldn't reach Yahoo Finance to look that up. Try again in a bit, or give me its symbol."
    if found is None:
        return (f"Yahoo Finance doesn't know \"{name}\". Give me its symbol (e.g. THYAO.IS for Turkish Airlines, "
                "AAPL for Apple).")
    symbol, shown = found
    symbols, names = _current()
    if symbol in symbols:
        return f"{hud_feeds.name_of(symbol)} ({symbol}) is already on the markets panel."
    if symbol not in hud_feeds.NAMES:
        names[symbol] = shown
    hud_feeds.save_markets(symbols + [symbol], names)
    _refresh()
    answer = f"I added {shown} ({symbol}) to the markets panel."
    if len(symbols) + 1 > PANEL_ROWS:
        answer += f" The panel has room for about {PANEL_ROWS}; its detail view shows them all."
    return answer


def unfollow(name):
    name = name.strip(" ,.\"'")
    symbols, names = _current()
    key, raw = _key(name), name.upper()
    gone = [s for s in symbols if name and (raw in (s, s.split(".")[0]) or key == _key(hud_feeds.name_of(s))
                                            or known(name) == s)]
    if not gone and name:
        try:
            found = resolve(name)
        except (requests.RequestException, ValueError):
            found = None
        gone = [s for s in symbols if found and s == found[0]]
    if not gone:
        return f"\"{name or 'That'}\" isn't on the markets panel. {_listing(symbols)}"
    shown = [f"{hud_feeds.name_of(s)} ({s})" for s in gone]
    hud_feeds.save_markets([s for s in symbols if s not in gone], {s: n for s, n in names.items() if s not in gone})
    _refresh()
    return f"I removed {', '.join(shown)} from the markets panel."


def run(action, name=""):
    if action == "follow":
        return follow(name)
    if action == "unfollow":
        return unfollow(name)
    return _listing(hud_feeds.market_symbols())


def follow_market(action: str, name: str = "") -> str:
    """The tool."""
    return run({"add": "follow", "remove": "unfollow"}.get(action.strip().lower(), action.strip().lower()), name)


TOOLS = [{
    "type": "function",
    "function": {
        "name": "follow_market",
        "description": (
            "Adds a stock, index, currency, commodity or crypto to the HUD's markets panel, removes one, or lists "
            "them. Use when the user asks to add, follow, remove or unfollow something in the markets / stocks / "
            "borsa, or asks what the markets panel shows. Not for how something is doing: that's market_outlook."),
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["add", "remove", "list"]},
            "name": {"type": "string", "description": "A company or market name, or its symbol (\"Apple\", \"AAPL\")."},
        }, "required": ["action"]},
    },
}]
TOOL_FUNCTIONS = {"follow_market": follow_market}
