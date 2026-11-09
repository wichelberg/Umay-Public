"""Who the newsfeed follows, changed by voice or text: "Umay, follow Nvidia in the news", "unfollow Elon Musk",
"haberlerde Nvidia'yı takip et", "Elon Musk'ı takipten çıkar". The HUD's NEWSFEED reads its feeds again at once.

- The list goes to hud_feeds.WATCH_FILE (%LOCALAPPDATA%\\Umay); from the first change on it is used instead of
  UMAY_NEWS_WATCH.
- asked() catches the plain ways of saying it before any model sees it (like claude_terminal.py: the small local
  model could get it wrong); the follow_news tool covers other ways, and lists who is followed. To keep it from
  firing on "follow the instructions", following needs the news named; unfollowing doesn't.
- A name is matched by its tag or any of its spellings: "unfollow Turkey" removes "Türkiye|Turkey|-Thanksgiving".
"""
import re

import hud
import hud_feeds

SPOKEN_TAIL = re.compile(r"\n\n\[.*\Z", re.S)  # voice.SPOKEN_HINT / the language reminder after a message
ADDRESS = re.compile(r"^\W*(?:(?:hey|ok|okay)\s+)?umay\b\W*", re.I)  # "Umay, ..."
POLITE = r"(?:(?:can|could|would|will) you\s+)?(?:please\s+)?"
NEWS_EN = r"(?:the\s+|my\s+)?news(?:\s*feed)?(?:\s+panel)?"
FOLLOW_EN = re.compile(rf"^{POLITE}(?:follow|add|track)\s+(?P<name>.+?)\s+(?:in|to|on|into)\s+{NEWS_EN}\W*$",
                       re.I | re.S)
UNFOLLOW_EN = re.compile(rf"^{POLITE}(?:(?:unfollow|stop following)\s+(?P<a>.+?)(?:\s+(?:from|in|on)\s+{NEWS_EN})?"
                         rf"|(?:remove|drop|delete)\s+(?P<b>.+?)\s+(?:from|in|on)\s+{NEWS_EN})\W*$", re.I | re.S)
NEWS_TR = r"(?:haber\w*(?:\s+akış\w*)?|news\s*feed\S*)"  # haberlerde, haberlere, haber akışından, news feed'e
UNFOLLOW_TR = re.compile(r"\btakip(?:ten)?\s+çıkar\w*|\btakip\s+etmeyi\s+bırak\w*|\btakibi\s+bırak\w*", re.I)
FROM_NEWS_TR = re.compile(r"\b(?:haber\w*d[ae]n|haber\s+akışından|news\s*feed\S*d[ae]n)\s+(?P<name>.+?)\s+"
                          r"(?:çıkar|sil|kaldır)\w*|\b(?P<name2>.+?)\s+(?:haber\w*d[ae]n|haber\s+akışından|"
                          r"news\s*feed\S*d[ae]n)\s+(?:çıkar|sil|kaldır)\w*", re.I)
FOLLOW_TR = re.compile(rf"\b{NEWS_TR}\s+(?:takip\s+et|ekle)\w*|\btakip\s+et\w*", re.I)
HAS_NEWS_TR = re.compile(rf"\b{NEWS_TR}", re.I)
SUFFIX = re.compile(r"['’]\w+")  # Nvidia'yı, Elon Musk'ı
FILLER = re.compile(r"(?<!\S)(?:lütfen|please|artık|ayrıca|şimdi|da|de|also|now)(?!\S)", re.I)  # "artık Tesla'yı da"


def _name(text):
    text = FILLER.sub(" ", HAS_NEWS_TR.sub(" ", SUFFIX.sub("", text)))
    return re.sub(r"\s+", " ", text).strip(" ,.:;!?-\"'")


def asked(text):
    """("follow" | "unfollow", name) when the owner plainly asks to change who is followed, else None."""
    text = ADDRESS.sub("", SPOKEN_TAIL.sub("", text or "")).strip()
    m = UNFOLLOW_EN.match(text)
    if m:
        return _found("unfollow", m.group("a") or m.group("b"))
    m = FOLLOW_EN.match(text)
    if m:
        return _found("follow", m.group("name"))
    m = UNFOLLOW_TR.search(text)
    if m:
        return _found("unfollow", text[:m.start()] + " " + text[m.end():])
    m = FROM_NEWS_TR.search(text)
    if m:
        return _found("unfollow", m.group("name") or m.group("name2"))
    m = FOLLOW_TR.search(text)
    if m and HAS_NEWS_TR.search(text):
        return _found("follow", text[:m.start()] + " " + text[m.end():])
    return None


def _found(action, raw):
    name = _name(raw or "")
    return (action, name) if name and len(name.split()) <= 5 else None


def _same(a, b):
    """Two entries name the same: their tags or a spelling in common."""
    def keys(n):
        return {s.lower() for s in hud_feeds.spellings(n)} | {hud_feeds.tag_of(n).lower()}
    return bool(keys(a) & keys(b))


def _listing(watch):
    if not watch:
        return "You don't follow anyone in the news."
    return "In the news you follow: " + ", ".join(hud_feeds.tag_of(n) for n in watch) + "."


def _refresh():
    hud.tell({"refresh": "news"})


def follow(name):
    name = name.strip(" ,.\"'")
    if not name:
        return "Who should I follow in the news?"
    watch = hud_feeds.watch_list()
    known = next((n for n in watch if _same(n, name)), None)
    if known is not None:
        return f"You already follow {hud_feeds.tag_of(known)} in the news."
    hud_feeds.save_watch(watch + [name])
    _refresh()
    return f"I'm following {hud_feeds.tag_of(name)} in the newsfeed now. Its articles show up in a moment."


def unfollow(name):
    name = name.strip(" ,.\"'")
    watch = hud_feeds.watch_list()
    gone = [n for n in watch if name and _same(n, name)]
    if not gone:
        return f"You don't follow {name or 'that'} in the news. {_listing(watch)}"
    hud_feeds.save_watch([n for n in watch if n not in gone])
    _refresh()
    return f"I stopped following {', '.join(hud_feeds.tag_of(n) for n in gone)} in the newsfeed."


def run(action, name=""):
    if action == "follow":
        return follow(name)
    if action == "unfollow":
        return unfollow(name)
    return _listing(hud_feeds.watch_list())


def follow_news(action: str, name: str = "") -> str:
    """The tool."""
    return run(action.strip().lower(), name)


TOOLS = [{
    "type": "function",
    "function": {
        "name": "follow_news",
        "description": (
            "Changes who or what the HUD's newsfeed follows (people, companies, countries, topics), or lists "
            "them. Use when the user asks to follow, add, unfollow or remove someone in the news, or asks who "
            "they follow there."),
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["follow", "unfollow", "list"]},
            "name": {"type": "string", "description": "The name to follow or unfollow, e.g. \"Nvidia\"."},
        }, "required": ["action"]},
    },
}]
TOOL_FUNCTIONS = {"follow_news": follow_news}
