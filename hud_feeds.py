"""What the HUD's news and markets panels show. No Qt: each read_* returns a plain dict, and the HUD calls them
off its UI thread (like hud_data).

- News: Google News RSS (free, no key). The people and companies followed (UMAY_NEWS_WATCH, a search each,
  the last NEWS_DAYS days) and Google News' sections (UMAY_NEWS_TOPICS: TOP, WORLD, BUSINESS, TECHNOLOGY,
  SCIENCE, HEALTH, SPORTS, ENTERTAINMENT, NATION), in UMAY_NEWS_REGION's language ("en-US", "tr-TR"...).
  An article found by two searches is shown once, with both tags. Once the owner changes who is followed by
  voice or text (news_follow.py), that list (WATCH_FILE) is used instead of UMAY_NEWS_WATCH.
- Markets: Yahoo Finance's chart API (free, no key): every symbol in UMAY_MARKET_SYMBOLS in one request, its
  price, the change since the last close and the day's curve; one symbol over a range for the detail view.
  US stocks and crypto are live; Borsa Istanbul (.IS) comes about 15 minutes late on Yahoo. Once the owner
  changes the symbols by voice or text (markets_follow.py), that list (MARKETS_FILE) is used instead.

.env: UMAY_NEWS_WATCH, UMAY_NEWS_TOPICS, UMAY_NEWS_REGION, UMAY_MARKET_SYMBOLS.
"""
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import requests

import paths

TIMEOUT = 10
# Yahoo answers a browser-like client; Google News doesn't mind either way.
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/130.0 Safari/537.36"}

NEWS_WATCH = "Anthropic,Elon Musk,Sam Altman,OpenAI,Türkiye|Turkey|Erdogan|-Thanksgiving|-recipe|-recipes|-roast|-bingo,UK|Britain|British"
NEWS_TOPICS = "TECHNOLOGY,BUSINESS,WORLD,SCIENCE"
NEWS_REGION = "en-US"
NEWS_DAYS = 3
NEWS_PER_TAG = 20
WATCH_FILE = "news_watch.json"  # in %LOCALAPPDATA%\Umay, shared by Umay.exe and a source checkout
MARKETS_FILE = "markets.json"  # {"symbols": [...], "names": {symbol: name}}: markets_follow.py's list
SECTIONS = {"WORLD", "NATION", "BUSINESS", "TECHNOLOGY", "ENTERTAINMENT", "SPORTS", "SCIENCE", "HEALTH"}

MARKET_SYMBOLS = "XU100.IS,^GSPC,^IXIC,NVDA,TSLA,GOOGL,MSFT,USDTRY=X,GBPTRY=X,BTC-USD,GC=F"
LIVE_SECONDS = 15 * 60  # the last trade this recent: the market is open
NAMES = {
    "^GSPC": "S&P 500", "^IXIC": "NASDAQ", "^DJI": "DOW JONES", "^RUT": "RUSSELL 2000", "^VIX": "VIX",
    "^FTSE": "FTSE 100", "^GDAXI": "DAX", "^N225": "NIKKEI 225", "^STOXX50E": "EURO STOXX 50",
    "XU100.IS": "BIST 100", "XU030.IS": "BIST 30", "THYAO.IS": "TURKISH AIRLINES", "USDTRY=X": "USD / TRY", "EURTRY=X": "EUR / TRY",
    "EURUSD=X": "EUR / USD", "GBPTRY=X": "GBP / TRY", "BTC-USD": "BITCOIN", "ETH-USD": "ETHEREUM",
    "SOL-USD": "SOLANA", "GC=F": "GOLD", "SI=F": "SILVER", "CL=F": "CRUDE OIL", "BZ=F": "BRENT",
    "NVDA": "NVIDIA", "TSLA": "TESLA", "GOOGL": "ALPHABET", "GOOG": "ALPHABET", "MSFT": "MICROSOFT",
    "AAPL": "APPLE", "AMZN": "AMAZON", "META": "META", "AMD": "AMD", "TSM": "TSMC", "ORCL": "ORACLE",
}
# Detail view ranges: Yahoo's range and the bar that fits it.
RANGES = {"1D": ("1d", "2m"), "5D": ("5d", "15m"), "1M": ("1mo", "60m"), "6M": ("6mo", "1d"), "1Y": ("1y", "1d"),
          "5Y": ("5y", "1wk")}


def _list(name, default):
    raw = os.environ.get(name, "").strip()
    return [x.strip() for x in (raw or default).split(",") if x.strip()]


# --- news ------------------------------------------------------------------------------------------

def watch_file():
    return paths.voice_home().parent / WATCH_FILE


def watch_list():
    """Who is followed: the list the owner last set by voice or text, else UMAY_NEWS_WATCH."""
    try:
        names = json.loads(watch_file().read_text(encoding="utf-8"))
        if isinstance(names, list):
            return [str(n).strip() for n in names if str(n).strip()]
    except (OSError, ValueError):
        pass
    return _list("UMAY_NEWS_WATCH", NEWS_WATCH)


def save_watch(names):
    path = watch_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(names, ensure_ascii=False, indent=1), encoding="utf-8")


def news_settings():
    """(watch, topics, region): who is followed, the sections, the language-COUNTRY."""
    region = os.environ.get("UMAY_NEWS_REGION", "").strip() or NEWS_REGION
    return watch_list(), [t.upper() for t in _list("UMAY_NEWS_TOPICS", NEWS_TOPICS)], region


def _region_params(region):
    lang, _, country = region.partition("-")
    country = (country or lang).upper()
    return {"hl": f"{lang}-{country}" if lang == "en" else lang, "gl": country, "ceid": f"{country}:{lang}"}


def _feed(url, params):
    r = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.content


def tag_of(name):
    """What a followed name or section shows as: "ELON MUSK", "TECHNOLOGY", TOP as "TOP STORIES"; a name with
    other spellings ("Türkiye|Turkey|Turkish") as its first."""
    name = name.split("|")[0].strip()
    return "TOP STORIES" if name.upper() == "TOP" else name.upper()


def spellings(name):
    """"Türkiye|Turkey|-Thanksgiving" -> ["Türkiye", "Turkey"]: any of them is that name."""
    return [a.strip() for a in name.split("|") if a.strip() and not a.strip().startswith("-")]


def search_query(name):
    """Google News' query for a followed name: '"OpenAI" when:3d', '("Türkiye" OR "Turkey") -Thanksgiving when:3d'
    (a "-word" leaves out articles with it: in November "Turkey" is mostly the bird, 2026-11-03)."""
    alts = [f'"{a}"' for a in spellings(name)]
    leave_out = [a.strip() for a in name.split("|") if a.strip().startswith("-") and len(a.strip()) > 1]
    return " ".join([alts[0] if len(alts) == 1 else "(" + " OR ".join(alts) + ")", *leave_out, f"when:{NEWS_DAYS}d"])


def parse_rss(content, tag, kind, now=None):
    """Google News RSS -> [{"title", "source", "link", "at", "tags", "kind"}], newest first (pure, for the
    tests). Its titles end in " - <source>", which goes."""
    now = now or time.time()
    out = []
    for item in ET.fromstring(content).iter("item"):
        title = (item.findtext("title") or "").strip()
        source = (item.findtext("source") or "").strip()
        if source and title.endswith(" - " + source):
            title = title[: -len(source) - 3].rstrip()
        try:
            at = parsedate_to_datetime(item.findtext("pubDate") or "").timestamp()
        except (TypeError, ValueError):
            at = None
        if not title or at is None or at > now + 3600:
            continue
        out.append({"title": title, "source": source, "link": (item.findtext("link") or "").strip(), "at": at,
                    "tags": [tag], "kind": kind})
    out.sort(key=lambda x: -x["at"])
    return out


def _key(title):
    return re.sub(r"\W+", " ", title.lower()).strip()


def merge(lists):
    """One list from several feeds' items, newest first: an article in two of them once, with both tags (a
    followed name's tag first)."""
    seen = {}
    for items in lists:
        for item in items:
            known = seen.get(_key(item["title"]))
            if known is None:
                seen[_key(item["title"])] = dict(item, tags=list(item["tags"]))
                continue
            for tag in item["tags"]:
                if tag not in known["tags"]:
                    known["tags"].append(tag)
            if item["kind"] == "watch" and known["kind"] != "watch":
                known["kind"] = "watch"
                known["tags"] = item["tags"] + [t for t in known["tags"] if t not in item["tags"]]
    return sorted(seen.values(), key=lambda x: -x["at"])


def tag_names(items, watch):
    """A followed name in an article's title tags it too ("OpenAI, Anthropic expose..." under both)."""
    for item in items:
        title = item["title"].lower()
        for name in watch:
            tag = tag_of(name)
            if tag not in item["tags"] and any(re.search(r"\b" + re.escape(a.lower()) + r"\b", title)
                                               for a in spellings(name)):
                item["tags"].append(tag)
                if item["kind"] != "watch":
                    item["kind"], item["tags"] = "watch", [tag] + item["tags"][:-1]
    return items


def read_news():
    """The followed names' and the sections' latest articles. A feed that fails is left out (listed in
    "failed"); all of them failing raises."""
    watch, topics, region = news_settings()
    params = _region_params(region)
    lists, failed = [], []
    for name in watch:
        try:
            content = _feed("https://news.google.com/rss/search", {"q": search_query(name), **params})
            lists.append(parse_rss(content, tag_of(name), "watch")[:NEWS_PER_TAG])
        except (requests.RequestException, ET.ParseError) as e:
            failed.append((name, str(e)))
    for topic in topics:
        if topic != "TOP" and topic not in SECTIONS:
            failed.append((topic, "not a Google News section"))
            continue
        url = ("https://news.google.com/rss" if topic == "TOP"
               else f"https://news.google.com/rss/headlines/section/topic/{topic}")
        try:
            lists.append(parse_rss(_feed(url, params), tag_of(topic), "topic")[:NEWS_PER_TAG])
        except (requests.RequestException, ET.ParseError) as e:
            failed.append((topic, str(e)))
    if not lists and failed:
        raise ConnectionError(f"no news feed answered: {failed[0][0]}: {failed[0][1]}")
    return {"items": tag_names(merge(lists), watch), "watch": [tag_of(n) for n in watch], "topics": [tag_of(t) for t in topics],
            "region": region, "failed": [n for n, _ in failed], "at": time.time()}


def spread(items, n):
    """Up to n items, newest first, but each tag's newest before any tag's second (a country in the news all
    day doesn't push a company's one article out)."""
    firsts, seen = [], set()
    for item in items:
        if item["tags"] and item["tags"][0] not in seen:
            seen.add(item["tags"][0])
            firsts.append(item)
    picked = firsts[:n]
    picked += [i for i in items if i not in picked][: n - len(picked)]
    return sorted(picked, key=lambda i: -i["at"])


def age(at, now=None):
    """"now", "12m", "3h", "2d"."""
    s = max(0, (now or time.time()) - at)
    if s < 60:
        return "now"
    if s < 3600:
        return f"{int(s // 60)}m"
    if s < 86400:
        return f"{int(s // 3600)}h"
    return f"{int(s // 86400)}d"


# --- markets --------------------------------------------------------------------------------------

def markets_file():
    return paths.voice_home().parent / MARKETS_FILE


def saved_markets():
    """markets_follow.py's {"symbols", "names"}, or None while the owner hasn't changed them."""
    try:
        saved = json.loads(markets_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(saved, dict) or not isinstance(saved.get("symbols"), list):
        return None
    names = saved.get("names") if isinstance(saved.get("names"), dict) else {}
    return {"symbols": [str(s).strip().upper() for s in saved["symbols"] if str(s).strip()], "names": names}


def save_markets(symbols, names):
    path = markets_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"symbols": symbols, "names": names}, ensure_ascii=False, indent=1), encoding="utf-8")


def market_symbols():
    saved = saved_markets()
    if saved is not None:
        return saved["symbols"]
    return [s.upper() for s in _list("UMAY_MARKET_SYMBOLS", MARKET_SYMBOLS)]


def name_of(symbol):
    symbol = symbol.upper()
    if symbol in NAMES:
        return NAMES[symbol]
    saved = saved_markets()
    return str((saved or {}).get("names", {}).get(symbol) or symbol)


def _last(values):
    return next((v for v in reversed(values or []) if v is not None), None)


def parse_spark(data, symbols, now=None):
    """Yahoo's spark answer -> one quote per symbol it knew, in `symbols`' order (pure, for the tests)."""
    now = now or time.time()
    out = []
    for symbol in symbols:
        s = data.get(symbol)
        if not s:
            continue
        closes, times = s.get("close") or [], s.get("timestamp") or []
        price = _last(closes)
        prev = s.get("previousClose") or s.get("chartPreviousClose")
        if price is None:
            continue
        last_at = next((t for t, c in zip(reversed(times), reversed(closes)) if c is not None), None)
        change = price - prev if prev else None
        out.append({"symbol": symbol, "name": name_of(symbol), "price": price, "prev": prev, "change": change,
                    "pct": change / prev * 100 if prev else None, "points": [c for c in closes if c is not None],
                    "at": last_at, "live": last_at is not None and now - last_at < LIVE_SECONDS})
    return out


def read_quotes(symbols):
    """Quotes for any symbols (parse_spark's shape), on the panel or not (price_alerts.py)."""
    if not symbols:
        return []
    r = requests.get("https://query1.finance.yahoo.com/v8/finance/spark", headers=HEADERS, timeout=TIMEOUT,
                     params={"symbols": ",".join(symbols), "range": "1d", "interval": "5m"})
    r.raise_for_status()
    return parse_spark(r.json(), symbols)


def read_markets():
    """Every market symbol: price, change since the last close and today's curve (5 min bars)."""
    symbols = market_symbols()
    if not symbols:
        return {"quotes": [], "missing": [], "at": time.time()}
    quotes = read_quotes(symbols)
    if not quotes:
        raise ValueError("Yahoo knew none of the market symbols")
    return {"quotes": quotes, "missing": [s for s in symbols if s not in {q["symbol"] for q in quotes}],
            "at": time.time()}


def parse_chart(data, symbol, span):
    """Yahoo's chart answer for one symbol over a RANGES span -> what the detail view draws (pure)."""
    result = (data.get("chart") or {}).get("result") or []
    if not result:
        error = (data.get("chart") or {}).get("error") or {}
        raise ValueError(error.get("description") or f"no chart for {symbol}")
    res = result[0]
    meta = res.get("meta") or {}
    quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    pairs = [(t, c) for t, c in zip(res.get("timestamp") or [], quote.get("close") or []) if c is not None]
    price = meta.get("regularMarketPrice") or (pairs[-1][1] if pairs else None)
    # 1D: against yesterday's close; longer: against where the range started.
    base = meta.get("previousClose") if span == "1D" else (pairs[0][1] if pairs else None)
    base = base or meta.get("chartPreviousClose")
    change = price - base if price is not None and base else None
    return {
        "symbol": symbol, "span": span, "name": meta.get("longName") or meta.get("shortName") or name_of(symbol),
        "exchange": meta.get("fullExchangeName") or meta.get("exchangeName") or "",
        "currency": meta.get("currency") or "", "price": price, "base": base, "change": change,
        "pct": change / base * 100 if change is not None else None,
        "prev_close": meta.get("previousClose") or meta.get("chartPreviousClose"),
        "day_high": meta.get("regularMarketDayHigh"), "day_low": meta.get("regularMarketDayLow"),
        "year_high": meta.get("fiftyTwoWeekHigh"), "year_low": meta.get("fiftyTwoWeekLow"),
        "volume": meta.get("regularMarketVolume"), "market_at": meta.get("regularMarketTime"),
        "times": [t for t, _ in pairs], "closes": [c for _, c in pairs], "at": time.time(),
    }


def read_chart(symbol, span="1D"):
    rng, interval = RANGES[span]
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{requests.utils.quote(symbol)}",
                     headers=HEADERS, timeout=TIMEOUT, params={"range": rng, "interval": interval})
    if r.status_code == 404:
        return parse_chart(r.json(), symbol, span)  # says why (an unknown symbol)
    r.raise_for_status()
    return parse_chart(r.json(), symbol, span)


def price_text(value, like=None):
    """12,345 · 202.06 · 49.9719: fewer decimals the bigger the number (or the price `like`, for a change)."""
    if value is None:
        return "—"
    a = abs(value if like is None else like)
    digits = 0 if a >= 10000 else 2 if a >= 100 else 3 if a >= 10 else 4
    return f"{value:,.{digits}f}"


def pct_text(pct):
    return "—" if pct is None else f"{pct:+.2f}%"


def volume_text(v):
    if v is None:
        return "—"
    for unit, size in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if v >= size:
            return f"{v / size:.2f}{unit}"
    return f"{v:.0f}"
