"""How a market looks: "Umay, how is Nvidia looking?", "Aselsan için ne düşünüyorsun?". From a year of Yahoo's daily
closes (hud_feeds.read_chart) and the last days' Google News headlines about it: the day's, week's and month's
change, the 20- and 50-day averages, RSI(14), where it sits in its 52-week range, and one sentence on which way
it leans, with why.

The lean is a rule on those numbers (lean()), not the model's guess: the owner asked for an "I expect it to
rise / fall" with a reason (2026-11-04), and a small model left to choose would sound sure of anything. It is a
reading of the chart, not a forecast, and the answer says so once. Nothing here buys, sells or advises.
"""
import time

import requests

import hud_feeds
import markets_follow

YEAR_SPAN = "1Y"
NEWS_ITEMS = 3


def average(closes, n):
    return sum(closes[-n:]) / n if len(closes) >= n else None


def change(closes, n):
    """% change over the last n closes, or None."""
    return (closes[-1] / closes[-1 - n] - 1) * 100 if len(closes) > n and closes[-1 - n] else None


def rsi(closes, n=14):
    """Wilder's RSI over the closes, or None with too few."""
    if len(closes) <= n:
        return None
    moves = [b - a for a, b in zip(closes, closes[1:])]
    gain = sum(max(m, 0) for m in moves[:n]) / n
    loss = sum(max(-m, 0) for m in moves[:n]) / n
    for m in moves[n:]:
        gain = (gain * (n - 1) + max(m, 0)) / n
        loss = (loss * (n - 1) + max(-m, 0)) / n
    return 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)


def signals(chart):
    """The numbers from read_chart's year of daily closes (pure)."""
    closes = chart["closes"]
    price = chart.get("price") or (closes[-1] if closes else None)
    if price is None or len(closes) < 2:
        return None
    if closes[-1] != price:
        closes = closes[:-1] + [price]  # today's bar may lag the live price
    low, high = chart.get("year_low"), chart.get("year_high")
    return {"price": price, "day": change(closes, 1), "week": change(closes, 5), "month": change(closes, 21),
            "ma20": average(closes, 20), "ma50": average(closes, 50), "rsi": rsi(closes),
            "range": (price - low) / (high - low) * 100 if low is not None and high and high > low else None}


def lean(s):
    """("up" | "down" | None, [reasons]): trend votes (price over its 20- and 50-day averages, the 20 over the
    50, the week's change) and RSI past 70 / under 30 against them; two votes either way make a lean."""
    votes, reasons = 0, []
    for avg, days in ((s["ma20"], 20), (s["ma50"], 50)):
        if avg:
            votes += 1 if s["price"] > avg else -1
    if s["ma20"] and s["ma50"]:
        votes += 1 if s["ma20"] > s["ma50"] else -1
    if s["week"] is not None and abs(s["week"]) >= 0.5:
        votes += 1 if s["week"] > 0 else -1
    if s["rsi"] is not None and s["rsi"] >= 70:
        votes -= 1
    if s["rsi"] is not None and s["rsi"] <= 30:
        votes += 1
    way = "up" if votes >= 2 else "down" if votes <= -2 else None

    above = [d for avg, d in ((s["ma20"], 20), (s["ma50"], 50)) if avg and (s["price"] > avg) == (way != "down")]
    if way and above:
        reasons.append(f"it is {'above' if way == 'up' else 'below'} its "
                       + " and ".join(f"{d}-day" for d in above) + (" averages" if len(above) > 1 else " average"))
    if way and s["week"] is not None and (s["week"] > 0) == (way == "up") and abs(s["week"]) >= 0.5:
        reasons.append(f"{'up' if s['week'] > 0 else 'down'} {abs(s['week']):.1f}% this week")
    if s["rsi"] is not None and (s["rsi"] >= 70 or s["rsi"] <= 30):
        high = s["rsi"] >= 70
        joint = "though " if way == ("up" if high else "down") else "and " if way else ""
        reasons.append(f"{joint}its RSI of {s['rsi']:.0f} says it is {'overbought' if high else 'oversold'}, and "
                       f"a {'pullback' if high else 'bounce'} often follows")
    if not way:
        reasons = ["its signals point different ways"] + reasons
    elif not reasons or reasons[0].startswith(("though", "and")):
        reasons = ["its trend points that way"] + reasons
    return way, reasons


def _pct(v):
    return "n/a" if v is None else f"{v:+.1f}%"


def text(name, symbol, s, headlines, now=None):
    """What the tool answers (pure)."""
    lines = [f"{name} ({symbol}): {hud_feeds.price_text(s['price'])}, {_pct(s['day'])} today, {_pct(s['week'])} "
             f"this week, {_pct(s['month'])} this month."]
    facts = []
    for avg, days in ((s["ma20"], 20), (s["ma50"], 50)):
        if avg:
            facts.append(f"{'above' if s['price'] > avg else 'below'} its {days}-day average "
                         f"({hud_feeds.price_text(avg, like=s['price'])})")
    if s["rsi"] is not None:
        facts.append(f"RSI {s['rsi']:.0f}")
    if s["range"] is not None:
        facts.append(f"{s['range']:.0f}% of the way up its 52-week range")
    if facts:
        lines.append("It is " + ", ".join(facts) + ".")
    if headlines:
        lines.append("Recent news: " + " | ".join(f"{h['title']} ({h['source']}, {hud_feeds.age(h['at'], now)} ago)"
                                                  for h in headlines))
    way, reasons = lean(s)
    if way:
        lines.append(f"Lean: I expect {name} to {'rise' if way == 'up' else 'fall'}, because {', '.join(reasons)}.")
    else:
        lines.append(f"Lean: I don't see a clear way for {name}, because {', '.join(reasons)}.")
    # The small model, left alone, talked about gardens and weather and never said which way (2026-11-04).
    lines.append("(Answer with the Lean sentence first, word for word, then one or two short sentences with what "
                 "matters most above, a headline only if it matters. It is your reading of the chart, not advice.)")
    return "\n".join(lines)


def news_query(name, symbol):
    """What to search the news for: "gold price", not "GOLD" (it found a Mayor Roy Gold, 2026-11-04)."""
    if symbol.endswith("=F"):
        return f"{name.lower()} price"
    if symbol.endswith("=X"):
        return "Turkish lira" if "TRY" in symbol else symbol[:-2]
    return name


def _headlines(query):
    try:
        params = hud_feeds._region_params(hud_feeds.news_settings()[2])
        content = hud_feeds._feed("https://news.google.com/rss/search", {"q": hud_feeds.search_query(query), **params})
        return hud_feeds.merge([hud_feeds.parse_rss(content, query, "watch")])[:NEWS_ITEMS]  # one of each story
    except Exception:
        return []  # the numbers still stand without news


def market_outlook(name: str) -> str:
    """The tool."""
    name = name.strip(" ,.\"'")
    if not name:
        return "Which market should I look at?"
    try:
        found = markets_follow.resolve(name)
        if found is None:
            return f"Yahoo Finance doesn't know \"{name}\". Give me its symbol (e.g. AAPL, THYAO.IS)."
        symbol, shown = found
        chart = hud_feeds.read_chart(symbol, YEAR_SPAN)
    except (requests.RequestException, ValueError) as e:
        return f"I couldn't read {name} from Yahoo Finance: {e}"
    s = signals(chart)
    if s is None:
        return f"Yahoo Finance has no prices for {shown} ({symbol}) right now."
    return text(shown, symbol, s, _headlines(news_query(shown if shown != symbol else name, symbol)), time.time())


TOOLS = [{
    "type": "function",
    "function": {
        "name": "market_outlook",
        "description": (
            "How a stock, index, currency, commodity or crypto looks now: its price and changes, trend, RSI, recent "
            "news and which way it leans, with why. Use when the user asks how something or a whole market (the "
            "Turkish stock market, borsa) is doing, "
            "what you think of it, or whether it will go up or down."),
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "A company or market name, or its symbol (\"Nvidia\", \"BTC\")."},
        }, "required": ["name"]},
    },
}]
TOOL_FUNCTIONS = {"market_outlook": market_outlook}
