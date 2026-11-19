"""Web search and page reading with no account, no key and no credit (the owner's wish, 2026-11-18): nothing here
goes to Tavily or to any model in the cloud.

search(): the `ddgs` package asks several search engines (Bing, Brave, DuckDuckGo, Google ...) the way a browser
does and returns title, link and snippet; the question itself reaches those engines, as it would from a browser.
read_page(): fetches a page and cuts the article text out of it on this PC (`trafilatura`). enrich() adds the
text of the first pages to the results, so the model reads more than a snippet.

Traps found while building it: a query now and then comes back "No results" from one engine and fine from the
next, so it is asked again with another engine; the results carry ads (bing.com/aclick ...), which are dropped;
the model picks the URLs it reads and a page can hold orders, so read_page refuses addresses inside the PC's
own network (localhost, 192.168.x.x ...) and is bounded in size and time. Safe-search is off: it filtered
ordinary answers (medicine, security, adult life) and the owner wants every question answered.
"""
import concurrent.futures
import ipaddress
import socket
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from urllib.parse import urlparse

import requests

# One per attempt. Each engine fails for some queries and answers others (measured 2026-11-18 on five queries: "auto",
# Bing, Yahoo, DuckDuckGo and Brave each missed one to three), so a query goes down this list until one answers.
ENGINES = ("auto", "yahoo", "bing", "duckduckgo", "brave", "mojeek", "startpage")
ADS = ("bing.com/aclick", "duckduckgo.com/y.js", "googleadservices.com", "googlesyndication.com", "/aclk?")
ROUNDS = 2
PAUSE_SECONDS = 1.5
PAGE_BYTES = 2_000_000
PAGE_SECONDS = 8
AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


def _ddgs():
    from ddgs import DDGS        # imported late: the package takes a second to load and Umay may never search
    return DDGS()


def _timelimit(topic, time_range):
    """ddgs' time limit (d, w, m, y) from Tavily's words, which web_search still speaks."""
    if topic == "news":
        return "d"
    return {"day": "d", "week": "w", "month": "m", "year": "y"}.get(time_range)


def _rfc2822(iso):
    """Tavily gives news dates as RFC 2822 and web_search's 24-hour rule reads that."""
    try:
        at = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return ""
    return format_datetime(at if at.tzinfo else at.replace(tzinfo=timezone.utc))


def _clean(rows, news, max_results):
    seen, out = set(), []
    for row in rows or []:
        url = (row.get("url") or row.get("href") or "").strip()
        if not url.startswith(("http://", "https://")) or any(a in url.lower() for a in ADS):
            continue
        parsed = urlparse(url)
        key = (parsed.netloc.lower().removeprefix("www."), parsed.path.rstrip("/"))
        if key in seen:
            continue
        seen.add(key)
        item = {"title": (row.get("title") or "").strip(), "url": url, "content": (row.get("body") or "").strip()}
        if news:
            item["published_date"] = _rfc2822(row.get("date"))
        out.append(item)
        if len(out) >= max_results:
            break
    return out


def search(query, max_results=5, topic=None, time_range=None):
    """[{title, url, content[, published_date]}]: Tavily's shape, so web_search reads either. [] when no engine
    answered (offline, blocked); never raises."""
    news = topic == "news"
    limit = _timelimit(topic, time_range)
    for round_ in range(ROUNDS):
        if round_:
            time.sleep(PAUSE_SECONDS)             # the engines also refuse a burst: the second round is a moment later
        for engine in ENGINES:
            try:
                client = _ddgs()
                ask = client.news if news else client.text
                rows = ask(query, max_results=max_results * 3, safesearch="off", backend=engine,
                           **({"timelimit": limit} if limit else {}))
            except Exception:                     # ddgs raises its own exceptions for "no results", timeouts, blocks
                continue
            found = _clean(rows, news, max_results)
            if found:
                return found
    return []


def public_address(url):
    """True when the URL is http(s) and its host resolves only to addresses on the internet (not this PC or its network)."""
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(parsed.hostname, parsed.port or 443)}
    except (socket.gaierror, UnicodeError):
        return False
    return bool(addresses) and all(ipaddress.ip_address(a).is_global for a in addresses)


def read_page(url, max_chars=1500):
    """The article text of a page, cut to max_chars ("" when it can't be read). Redirects are followed only to
    public addresses; the download stops at PAGE_BYTES."""
    if not public_address(url):
        return ""
    try:
        import trafilatura
        with requests.get(url, headers={"User-Agent": AGENT, "Accept-Language": "en,tr;q=0.8"}, timeout=PAGE_SECONDS,
                          stream=True, allow_redirects=True) as response:
            if response.status_code != 200 or not public_address(response.url):
                return ""
            if "html" not in response.headers.get("content-type", "html").lower():
                return ""
            body = b""
            for chunk in response.iter_content(65536):
                body += chunk
                if len(body) >= PAGE_BYTES:
                    break
        html = body.decode(response.encoding or response.apparent_encoding or "utf-8", errors="replace")
        text = trafilatura.extract(html, include_comments=False, include_tables=False) or ""
    except Exception:
        return ""
    text = " ".join(text.split())
    return text[:max_chars] + ("..." if len(text) > max_chars else "")


def enrich(results, pages=2, max_chars=1500):
    """Adds "page" (the article text) to the first `pages` results, read at the same time. A page that can't be
    read keeps only its snippet."""
    head = results[:pages]
    if not head:
        return results
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(head)) as pool:
        for row, text in zip(head, pool.map(lambda r: read_page(r["url"], max_chars), head)):
            if text:
                row["page"] = text
    return results
