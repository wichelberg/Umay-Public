"""Local routing before the model: for each command, layer 1 (Umay could run one tool herself, no model call) or
layer 3 (the model with all tools, as today). Deterministic scoring over word categories (the owner's spec,
2026-11-05): G_w (how much of the text is understood command language), margin (how clearly one category wins), S_1;
a resolver per category turns the words into (tool, params) only when it is sure.

SHADOW ONLY for now (the owner's choice, 2026-11-05): nothing is executed here. ai_assistant asks route() for each
message the model handles and log() writes what layer 1 WOULD have done next to the tools the model really called,
to %LOCALAPPDATA%\\Umay\\router_shadow.jsonl. `python router.py review` summarises it. UMAY_ROUTER=off stops it.

Layer 2 (the model with only a few tools) is off: measured on this PC (qwen3.5-4b, Ollama) it was slower than
layer 3 (median 1.21 s vs 0.96 s per command: Ollama keeps the full tool list's prompt cached, every small set is
new) and no more accurate (50/77 vs 48/77 right first tools). Calibration (782 labelled commands, 504 of them the
owner's real requests): 0 wrong layer-1 actions; layer 1 took ~50% of plain commands but only 4% of the real log
(mostly chat, multi-step tasks, garbled speech; the easy Spotify commands are taken earlier by the shortcuts).
Layer-1 tools are only ones that are reversible or ask the owner first; never deletes, sending, or results the
model must interpret. When layer 1 is turned on, its tools must run through the model's own tool path
(ai_assistant), so every confirmation stays."""
import json
import os
import re
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------- thresholds
# The spec's values. With layer 2 off L2_S1 doesn't matter; on the calibration data every L1_GW in 0.40-1.00 and
# L1_MARGIN >= 0.02 gave 0 wrong actions (the resolvers carry the safety; the thresholds set the coverage).
DEFAULTS = {"L1_GW": 0.80, "L1_MARGIN": 0.10, "L2_S1": float("inf"), "W_VERB": 1.0}

# ---------------------------------------------------------------- categories: tools, keywords ("v:" = verb)
CATEGORIES = {
    "media": (["media_key"], "v:pause v:resume v:unpause v:play v:stop v:skip v:next v:previous v:start "
                             "music song songs track playback"),
    "volume": (["set_volume"], "volume sound v:mute v:unmute v:volume_up v:volume_down louder quieter "
                               "v:increase v:decrease v:lower v:raise loud quiet"),
    "video": (["control_video"], "video videos v:loop v:repeat fullscreen v:mute v:unmute"),
    "youtube": (["search_youtube", "open_youtube_result", "open_youtube_channel", "open_latest_youtube_video"],
                "youtube v:play v:watch v:search video videos channel"),
    "browser": (["open_in_browser", "web_tabs", "web_read_page", "web_act", "web_browser_data", "web_find_in_site"],
                "browser tab tabs website site page link url bookmark bookmarks history v:read v:download v:upload "
                "v:scroll v:zoom v:pin"),
    "web": (["web_search"], "v:search v:google weather forecast news price v:find"),
    "memory": (["search_memory"], "v:remember earlier conversation"),
    "app_launch": (["find_applications", "launch_application"],
                   "v:open v:launch v:start v:run app application program game"),
    "app_close": (["close_app", "list_open_apps"], "v:close v:quit v:exit app application window"),
    "app_window": (["window_control"], "v:move v:minimize v:maximize v:restore window monitor"),
    "mail": (["draft_mail"], "mail email e-mail outlook v:write v:draft"),
    "processes": (["list_processes", "end_process"], "process processes v:kill v:end task_manager"),
    "files": (["open_path", "list_directory", "search_files", "read_file", "create_folder", "write_file", "move_path",
               "delete_path", "organize_downloads"],
              "v:open file files folder folders document directory v:delete v:move v:rename v:create v:write "
              "v:read v:find v:organize v:tidy txt pdf"),
    "python": (["run_python", "list_runs", "stop_run"], "python script v:run v:stop"),
    "code": (["open_in_vscode", "open_terminal"], "vscode terminal project editor"),
    "shutdown": (["schedule_shutdown", "cancel_shutdown", "shutdown_status"],
                 "v:shut_down v:restart v:reboot v:power_off v:cancel v:stop v:turn_off v:abort computer pc laptop"),
    "lock": (["lock_pc"], "v:lock computer pc laptop"),
    "status": (["pc_status"], "cpu ram memory disk storage status uptime battery usage computer pc laptop"),
    "steam": (["list_steam_games", "uninstall_steam_game"], "steam games v:uninstall"),
    "screen": (["look_at_screen", "send_screenshot", "click", "scroll", "type_text", "press_keys", "move_mouse"],
               "screen screenshot v:click v:scroll v:type v:press mouse cursor monitor button key v:maximize "
               "v:minimize"),
    "camera": (["send_camera_photo", "learn_my_face"], "camera photo picture face v:learn"),
    "claude": (["open_claude_code"], "claude_code claude"),
    "call": (["call_owner"], "v:call"),
    "spotify": (["spotify"], "spotify playlist album v:shuffle v:like liked"),
    "alarms": (["list_alarms", "add_reminder", "set_alarm_active", "delete_alarm"],
               "v:remind reminder reminders alarm alarms v:wake_up v:turn_off v:turn_on v:disable v:enable"),
    "calendar": (["add_to_calendar", "read_plan_item"], "calendar event events"),
    "whatsapp": (["whatsapp_chats", "whatsapp_messages", "send_whatsapp_file"],
                 "whatsapp message messages chat chats v:send v:text"),
    "sleep": (["start_sleep_mode"], "sleep_mode"),
    "news": (["follow_news"], "news newsfeed v:follow v:unfollow"),
    "markets": (["follow_market", "market_outlook", "price_alert"],
                "stock stocks market markets crypto bitcoin currency shares price alarm alert v:follow v:unfollow"),
    "plan": (["plan_day"], "plan todo task tasks done"),
}
SLOT_CATS = {"youtube", "app_launch", "app_close", "files", "web", "memory", "whatsapp", "news", "markets", "plan",
             "claude", "browser", "alarms", "spotify", "code", "processes", "steam"}
TIME_CATS = {"alarms", "shutdown"}

KEYWORDS = defaultdict(set)   # word -> categories
VERBS = set()
for _cat, (_tools, _words) in CATEGORIES.items():
    for _w in _words.split():
        if _w.startswith("v:"):
            _w = _w[2:]
            VERBS.add(_w)
        KEYWORDS[_w].add(_cat)

OPEN_VERBS = {"open", "launch", "start", "run", "play", "load"}
CLOSE_VERBS = {"close", "quit", "exit"}
TOGGLE_VERBS = {"turn_off", "turn_on", "disable", "enable"}

QUESTION = set("what whats how why when where who which whose".split())
GENERIC_VERBS = set("show list make set get give tell put let do go want need check see try help use bring keep "
                    "take find_out".split())
CONTEXT = set("it its them that this these those there here same again one ones first second third fourth fifth "
              "last other another".split())
DEPENDENT = {"it", "its", "them", "that", "there", "those", "these"}   # the right side of "and" leans on the left
DEICTIC_NOUNS = {"song", "track", "music", "video", "playlist", "album"}  # "this song": what's playing, not history
NEGATION = set("not no dont never without nothing cant cannot wont didnt doesnt isnt arent stop_doing".split())
CONSULT = set("should think recommend suggest wonder whether maybe opinion".split())
FILLERS = set("please could can would will you your the a an my me i im ive id we us our umay umai yumei yume yumi "
              "hey hi ok okay yeah yes yep sure thanks thank so just now right up real quick quickly also as well too "
              "kindly for to of with by is are was were be been am do does did some all every any wanna gonna "
              "really actually like_to alright and_also mate love dear oh uh um er hmm bit little lot slightly "
              "percent".split())
PREPS = set("in on at from inside into within".split())
TIME_WORDS = set("minute minutes min mins hour hours hr hrs seconds sec secs day days week weeks tomorrow today "
                 "tonight morning evening afternoon noon midnight am pm oclock half quarter later daily weekly "
                 "hourly".split())
NOT_COUNTED = QUESTION | GENERIC_VERBS | CONTEXT | NEGATION | CONSULT | {"or"}
AUX_QUESTION = {"did", "do", "does", "is", "are", "was", "were", "have", "has", "had", "isnt", "arent", "didnt",
                "doesnt", "dont"}
ADDRESS = {"umay", "umai", "yumei", "yume", "yumi", "hey", "hi", "ok", "okay", "oh", "so", "and", "but", "well",
           "yeah", "yes", "no", "uh", "um", "thanks", "thank", "you"}
REMIND_END = {"in", "at", "on", "tomorrow", "tonight", "today", "every", "daily", "weekly", "hourly", "and", "then"}

# multi-word phrases -> one token (applied in order on the lowered text)
PHRASES = [
    (r"\bmake it (louder|quieter)\b", r"\1"),
    (r"\block (?:the |my )?screen\b", "lock"),
    (r"\bturn (?:it|this|that|the (?:volume|sound|music))? ?up\b", "volume_up"),
    (r"\bturn (?:it|this|that|the (?:volume|sound|music))? ?down\b", "volume_down"),
    (r"\bturn up\b", "volume_up"), (r"\bturn down\b", "volume_down"),
    (r"\bvolume up\b", "volume_up"), (r"\bvolume down\b", "volume_down"),
    (r"\bshut (?:it |the (?:pc|computer|laptop) )?down\b", "shut_down"), (r"\bshutdown\b", "shut_down"),
    (r"\bpower (?:it )?off\b", "power_off"),
    (r"\b(?:turn|switch) off\b", "turn_off"), (r"\b(?:turn|switch) on\b", "turn_on"),
    (r"\bwake (?:me )?up\b", "wake_up"),
    (r"\b(?:look|search) (?:it )?up\b", "search"), (r"\blook for\b", "search"),
    (r"\bfull ?screen\b", "fullscreen"),
    (r"\b(?:vs|visual studio) code\b", "vscode"),
    (r"\btask manager\b", "task_manager"),
    (r"\b(?:claude|cloud) code\b", "claude_code"),
    (r"\bsleep mode\b", "sleep_mode"),
    (r"\bto ?-?do\b", "todo"),
    (r"\bafter that\b", "then"), (r"\band then\b", "then"),
    (r"\bwould like to\b|\bi'?d like to\b", "like_to"),
    (r"\bfind out\b", "find_out"),
    (r"\bstop (?:doing|that)\b", "stop_doing"),
    (r"\bset up\b", "set"), (r"\bopen up\b", "open"), (r"\bstart up\b", "start"), (r"\bbring up\b", "open"),
    (r"\bpull up\b", "open"), (r"\bclose down\b", "close"), (r"\bplay back\b", "play"),
]
SPOKEN = re.compile(r"\s*\[(?:This message|Bu mesaj).*\Z", re.S | re.I)
HINT = re.compile(r"\n\n[(\[].*\Z", re.S)   # voice.SPOKEN_HINT, companion's "(If they ...)": not the owner's words

SITES = {"google": "https://www.google.com", "gmail": "https://mail.google.com", "github": "https://github.com",
         "chatgpt": "https://chatgpt.com", "netflix": "https://www.netflix.com", "twitch": "https://www.twitch.tv",
         "reddit": "https://www.reddit.com", "wikipedia": "https://www.wikipedia.org",
         "instagram": "https://www.instagram.com", "linkedin": "https://www.linkedin.com",
         "twitter": "https://x.com"}
APP_HOME = {"opera gx": "browser", "google chrome": "browser", "microsoft edge": "browser", "spotify": "spotify",
            "steam": "steam", "whats app": "whatsapp", "whats app beta": "whatsapp", "visual studio code": "code",
            "vscode": "code", "terminal": "code", "task_manager": "processes"}
GENERIC_TAIL = {"gx", "browser", "hub", "app", "launcher", "client", "connect", "beta", "desktop", "media", "player"}
SKIP_APPS = {"run", "access", "family", "people", "documentation", "release notes", "files", "services", "get help",
             "get started", "journal", "click to do"}


# ---------------------------------------------------------------- entities
@dataclass
class Entity:
    name: str                 # canonical (display) name
    tokens: tuple             # how it is said
    kind: str                 # app | game | folder | file | site | alarm
    home: str = None
    ref: str = None           # app_id / path / url / alarm name
    derived: bool = False     # an alias made from the name, not the name itself


def _words(text, camel=False):
    """A name's words the way a command's words are made (tokenize: "Task Manager" -> task_manager); camel=True
    splits "GithubPuller" -> github puller."""
    if camel:
        text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    text = re.sub(r"[™®©]", "", text)
    text = re.sub(r"[_\-.]+", " ", text)
    return tokenize(text)


def _name_forms(text):
    return {tuple(f) for f in (_words(text), _words(text, camel=True)) if f}


JUNK_FILES = re.compile(r"^(?:desktop\.ini|thumbs\.db|~\$.*|\..*)$|\.(?:ini|db|tmp|log|lnk|url)$", re.I)


def live_data():
    """The PC's local names now: installed apps and games, the allowed folders' contents (and the projects in
    Documents\\Projects and Documents\\Work), Umay's alarms. Slow (Get-StartApps): call off the main path."""
    import pc_tools
    import reminder_tools

    apps = dict(pc_tools._installed_apps())
    try:
        alarms = [e["name"] for e in reminder_tools._load().get("events", [])]
    except (OSError, ValueError, KeyError):
        alarms = []
    places = []
    for root in pc_tools.ALLOWED_DIRS:
        places.append({"name": root.name, "path": str(root), "kind": "folder"})
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        for child in children:
            places.append({"name": child.stem if child.is_file() else child.name, "path": str(child),
                           "kind": "file" if child.is_file() else "folder"})
    for sub in ("Projects", "Work"):
        d = Path.home() / "Documents" / sub
        try:
            places += [{"name": c.name, "path": str(c), "kind": "folder"} for c in d.iterdir() if c.is_dir()]
        except OSError:
            pass
    return {"apps": apps, "alarms": alarms, "places": places}


def load_entities(data):
    """data: {"apps": {app_id: name}, "alarms": [name], "places": [{"name", "path", "kind"}]} -> [Entity]"""
    out = []
    steam_ids = {k for k in data["apps"] if k.startswith("steam://")}
    for app_id, raw in data["apps"].items():
        name = raw.strip()
        base = re.sub(r"\s*\(.*?\)\s*", " ", name).strip()
        low = " ".join(_words(base))
        if low in SKIP_APPS or not low:
            continue
        kind = "game" if app_id in steam_ids else "app"
        forms = _name_forms(base)
        full = tuple(_words(base))
        aliases = forms - {full}
        w = _words(base)
        if w[-1:] in (["browser"], ["app"]) and len(w) > 1:
            w = w[:-1]
            aliases.add(tuple(w))
        while w and re.fullmatch(r"\d+(\.\d+)*", w[-1]) and len(w) > 1:   # "Blender 4.2", "Tableau 2025.3"
            w = w[:-1]
            aliases.add(tuple(w))
        if w[:1] in (["microsoft"], ["windows"], ["google"]) and len(w) > 1:
            aliases.add(tuple(w[1:]))
        # the brand alone only before a generic tail ("Opera GX Browser" -> opera, "VLC media player" -> vlc), not
        # "Live captions" -> live, "Sound Recorder" -> sound
        if len(w) >= 2 and len(w[0]) >= 3 and set(w[1:]) <= GENERIC_TAIL:
            aliases.add((w[0],))
        home = APP_HOME.get(" ".join(_words(base, camel=True)).replace("vscode", "visual studio code"),
                            APP_HOME.get(" ".join(w), "app_launch"))
        out.append(Entity(name, full, kind, home, app_id))
        for a in aliases - {full}:
            out.append(Entity(name, a, kind, home, app_id, derived=True))
    for p in data["places"]:
        if p["kind"] == "file" and JUNK_FILES.search(Path(p["path"]).name):
            continue
        for toks in _name_forms(p["name"]):
            if len(toks) <= 4:
                out.append(Entity(p["name"], toks, p["kind"], "files", p["path"]))
    for site, url in SITES.items():
        out.append(Entity(site, (site,), "site", "browser", url))
    for a in data.get("alarms", []):
        toks = tuple(_words(a))
        if toks:
            out.append(Entity(a, toks, "alarm", "alarms", a))
    # a derived alias loses to a real name ("whatsapp": WhatsApp, not WhatsApp Beta); a one-word derived alias
    # shared by several different things is not an alias
    real = {e.tokens for e in out if not e.derived}
    out = [e for e in out if not (e.derived and e.tokens in real)]
    by_tokens = defaultdict(set)
    for e in out:
        by_tokens[e.tokens].add((e.kind, e.ref))
    return [e for e in out if not e.derived or len(e.tokens) > 1 or len(by_tokens[e.tokens]) == 1]


# ---------------------------------------------------------------- preprocessing
@dataclass
class Item:
    word: str = None
    entities: list = None      # several when a name is ambiguous (Documents twice)
    prep: str = None
    commandish: bool = False   # the name is also command words ("Weather", "Terminal"): it counts as both

    @property
    def is_entity(self):
        return self.entities is not None


def owner_text(text):
    return SPOKEN.sub("", HINT.sub("", text or "")).strip()


def tokenize(text):
    t = owner_text(text).lower().replace("’", "'")
    t = re.sub(r"\b([ap])\.\s?m\.?", r"\1m", t)
    t = re.sub(r"(\d+)\s*%", r"\1 percent", t)
    t = re.sub(r"\b(\d{1,2})(am|pm)\b", r"\1 \2", t)
    t = t.replace("'", "")
    for pattern, repl in PHRASES:
        t = re.sub(pattern, repl, t)
    return re.findall(r"\d{1,2}:\d{2}|\d+(?:\.\d+)?|[a-zçğıöşü_]+", t)


def match_entities(tokens, entities, by_first):
    """Longest match first. Names made only of command words (Weather, Camera, Terminal...) count only right after
    an open / close verb."""
    items, i = [], 0
    while i < len(tokens):
        best = None
        for e in by_first.get(tokens[i], ()):
            n = len(e.tokens)
            if tuple(tokens[i:i + n]) == e.tokens and (best is None or n > len(best[0].tokens)):
                best = [e]
            elif best and tuple(tokens[i:i + n]) == e.tokens and n == len(best[0].tokens):
                best.append(e)
        commandish = False
        if best:
            commandish = all(w in KEYWORDS or w in FILLERS or w in NOT_COUNTED or w in PREPS for w in best[0].tokens)
            if commandish and not any(t in OPEN_VERBS | CLOSE_VERBS for t in tokens[:i]):
                best = None          # "what's the weather": the word, not the Weather app
        if best:
            uniq = {(e.kind, e.ref): e for e in best}
            items.append(Item(entities=list(uniq.values()), commandish=commandish))
            i += len(best[0].tokens)
        else:
            items.append(Item(word=tokens[i]))
            i += 1
    return items


def _has_verb(items):
    return any(not it.is_entity and (it.word in VERBS or it.word in GENERIC_VERBS) for it in items)


def _verb_of(items):
    return next((it.word for it in items if not it.is_entity and it.word in VERBS), None)


def _only_entities(items):
    real = [it for it in items if it.is_entity or it.word not in FILLERS]
    return bool(real) and all(it.is_entity for it in real)


def split_tasks(items):
    """[(items, dependent)]"""
    segments, cur = [], []
    for it in items:
        if not it.is_entity and it.word in ("and", "then"):
            segments.append(cur)
            cur = []
        else:
            cur.append(it)
    segments.append(cur)
    tasks = [[segments[0], False]]
    for right in segments[1:]:
        left = tasks[-1][0]
        if any(not it.is_entity and it.word in DEPENDENT for it in right):
            tasks[-1] = [left + [Item(word="and")] + right, True]
        elif _has_verb(left) and _has_verb(right):
            tasks.append([right, False])
        elif _has_verb(left) and _only_entities(right):
            verb = _verb_of(left)
            tasks.append([([Item(word=verb)] if verb else []) + right, False])
        else:
            tasks[-1] = [left + [Item(word="and")] + right, tasks[-1][1]]
    return [(t, dep) for t, dep in tasks if t]


# ---------------------------------------------------------------- scoring
@dataclass
class Task:
    items: list
    dependent: bool
    scores: dict = field(default_factory=dict)
    T: float = 0.0
    words: list = field(default_factory=list)       # counted content words
    slot: list = field(default_factory=list)        # unmatched words taken as the parameter
    unknown: list = field(default_factory=list)     # unmatched words counted in T
    numbers: list = field(default_factory=list)
    objects: list = field(default_factory=list)     # entity items that are the object
    locations: list = field(default_factory=list)
    params: list = field(default_factory=list)      # entity items that are only parameters
    flags: set = field(default_factory=set)
    verb: str = None
    all_words: list = field(default_factory=list)
    text: list = field(default_factory=list)        # a reminder's own words ("remind me to <text>")

    def ranked(self):
        return sorted(self.scores.items(), key=lambda kv: -kv[1])

    @property
    def S1(self):
        r = self.ranked()
        return r[0][1] if r else 0.0

    @property
    def top(self):
        r = self.ranked()
        return r[0][0] if r else None

    @property
    def G(self):
        return sum(self.scores.values()) / self.T if self.T else 0.0

    @property
    def margin(self):
        r = self.ranked()
        if not r or not self.T:
            return 0.0
        return (r[0][1] - (r[1][1] if len(r) > 1 else 0.0)) / self.T

    @property
    def blocked(self):
        return bool(self.flags & {"negation", "context", "or", "consult", "dependent", "files2"})


def _decides(verb, entity):
    k = entity.kind
    if verb in OPEN_VERBS:
        if k in ("app", "game"):
            return "app_launch"
        if k in ("folder", "file"):
            return "files"
        if k == "site":
            return "browser"
    if verb in CLOSE_VERBS and k in ("app", "game"):
        return "app_close"
    if verb in TOGGLE_VERBS and k == "alarm":
        return "alarms"
    return None


def score(items, dependent, w_verb):
    t = Task(items, dependent)
    if dependent:
        t.flags.add("dependent")
    s = defaultdict(float)
    prep = None
    seen_verb = None
    unmatched = []
    text_mode, text_from = False, None   # inside "remind me to <text>": free text, not command words
    first = next((it.word for it in items if not it.is_entity and it.word not in ADDRESS), None)
    if first in AUX_QUESTION:        # "did you pause the music", "is discord open": a question, not a request
        t.flags.add("question")
    for i, it in enumerate(items):
        nxt = items[i + 1].word if i + 1 < len(items) and not items[i + 1].is_entity else None
        if text_from is not None and i >= text_from:
            text_mode, text_from = True, None
        if text_mode:
            w = None if it.is_entity else it.word
            if w in REMIND_END or (w and re.fullmatch(r"\d{1,2}:\d{2}", w)):
                text_mode = False
            else:
                said = [w] if w else list(it.entities[0].tokens)
                t.all_words.extend(said)
                t.slot.extend(said)
                t.text.extend(said)
                continue
        if it.is_entity:
            it.prep = prep
            e = it.entities[0]
            decided = _decides(seen_verb, e) if seen_verb and prep is None else None
            if prep is not None:
                homes = {x.home for x in it.entities if x.home}
                if len(homes) == 1:
                    s[homes.pop()] += 0.5
                    t.T += 0.5
                t.locations.append(it)
            elif decided:
                s[decided] += 1.0
                t.T += 1.0
                t.objects.append(it)
            else:
                t.params.append(it)
            if it.commandish:        # "open the weather": the Weather app, or the weather?
                for w in e.tokens:
                    H = KEYWORDS.get(w)
                    if H and e.home not in H:   # "launch steam": Steam's word is Steam's own category
                        for cat in H:
                            s[cat] += (w_verb if (w in VERBS and len(H) > 1) else 1.0) / len(H)
                        t.T += 1
                        t.words.append(w)
            continue                 # prep kept: "in the documents projects" is one place
        w = it.word
        t.all_words.append(w)
        if w in PREPS:
            prep = w
            continue
        if w in FILLERS or w in ("and", "then"):   # connectors left in a task that wasn't split
            continue
        if re.fullmatch(r"[\d.:]+", w):
            t.numbers.append(w)
            prep = None
            continue
        if w == "remind" or (w == "reminder" and seen_verb is None):
            j = i + 1
            while j < len(items) and not items[j].is_entity and items[j].word in ("me", "us", "him", "her"):
                j += 1
            if j < len(items) and not items[j].is_entity and items[j].word in ("to", "about", "that", "of"):
                text_from = j + 1
                items[j] = Item(word="to")   # "remind me that ...": not a reference to something said before
        if w in NEGATION:
            t.flags.add("negation")
        if w in ("this", "these") and nxt in DEICTIC_NOUNS:
            continue                 # "skip this song": the one playing
        if w in CONTEXT:
            t.flags.add("context")
        if w == "or":
            t.flags.add("or")
        if w in CONSULT:
            t.flags.add("consult")
        if w in QUESTION:
            t.flags.add("question")
        if w in NOT_COUNTED:
            if w in GENERIC_VERBS and seen_verb is None:
                pass
            continue
        H = KEYWORDS.get(w)
        if w in VERBS and seen_verb is None:
            seen_verb = w
            t.verb = w
        if H:
            c = w_verb if (w in VERBS and len(H) > 1) else 1.0
            for cat in H:
                s[cat] += c / len(H)
            t.T += 1
            t.words.append(w)
        else:
            unmatched.append(w)
        prep = None
    t.scores = dict(s)
    top = t.top
    for w in unmatched:
        if w in TIME_WORDS:
            if top in TIME_CATS:
                t.slot.append(w)
            else:
                t.T += 1
                t.unknown.append(w)
        elif top in SLOT_CATS:
            t.slot.append(w)
        else:
            t.T += 1
            t.unknown.append(w)
    file_objects = [o for o in t.objects if o.entities[0].kind in ("file", "folder")]
    if len(file_objects) >= 2:
        t.flags.add("files2")
    return t


# ---------------------------------------------------------------- resolvers: task -> (tool, params) or None
def _one_entity(item, kinds):
    """The one thing a name means, or None if it means several (apps: the same display name twice, a Start menu
    shortcut and a Steam game, is one app)."""
    es = [e for e in item.entities if e.kind in kinds]
    if len({e.ref for e in es}) == 1:
        return es[0]
    if es and all(e.kind in ("app", "game") for e in es) and len({e.name.strip().lower() for e in es}) == 1:
        return sorted(es, key=lambda e: e.ref)[0]
    return None


def _only(t, cats, extra=()):
    """Every counted word belongs to these categories: a word of another one means the resolver isn't sure."""
    return all(w in extra or any(c in cats for c in KEYWORDS.get(w, ())) for w in t.words)


def _has_prep(t):
    return any(w in PREPS for w in t.all_words)


def r_media(t):
    if t.slot or t.unknown or t.objects or t.locations or t.params or t.numbers:
        return None
    if not _only(t, {"media"}, {"video", "videos"}):
        return None
    if "previous" in t.words:
        return "media_key", {"action": "previous"}
    if t.verb in ("skip", "next") or ("next" in t.words and {"song", "track"} & set(t.words)):
        return "media_key", {"action": "next"}
    if t.verb in ("pause", "resume", "unpause", "stop", "play", "start"):
        if t.verb in ("play", "start", "stop") and not {"music", "song", "track", "playback"} & set(t.words):
            return None
        if t.verb in ("play", "start") and {"some", "any"} & set(t.all_words):
            return None              # "play some music": choose something to play, not the play/pause key
        return "media_key", {"action": "play_pause"}
    return None


def _number(t):
    nums = [n for n in t.numbers if re.fullmatch(r"\d+", n)]
    return int(nums[0]) if len(nums) == 1 and len(t.numbers) == 1 else None


def r_volume(t):
    words = set(t.words)
    if {"video", "videos"} & words or "youtube" in words or t.slot or t.unknown or t.objects or t.locations:
        return None
    if not _only(t, {"volume"}):
        return None
    n = _number(t)
    if t.numbers and n is None:
        return None
    if "unmute" in words:
        return "set_volume", {"mute": False}
    if "mute" in words:
        return "set_volume", {"mute": True}
    up = words & {"volume_up", "louder", "increase", "raise"}
    down = words & {"volume_down", "quieter", "decrease", "lower"}
    if up and down:
        return None
    if n is not None and 0 <= n <= 100 and ({"volume", "sound"} & words or up or down):
        return "set_volume", {"level": n}
    if up:
        return "set_volume", {"change": 10}
    if down:
        return "set_volume", {"change": -10}
    if "question" in t.flags and {"volume", "sound"} & words and not t.numbers:
        return "set_volume", {}
    return None


def r_video(t):
    words = set(t.words)
    if t.slot or t.unknown or t.objects or t.params:
        return None
    if not ({"video", "videos"} & words) or not _only(t, {"video"}, {"volume", "sound", "turn_off", "turn_on"}):
        return None                  # "full screen" / "loop" alone: VS Code? the Spotify song?
    if "loop" in words or "repeat" in words:
        if "turn_off" in t.all_words:
            return "control_video", {"action": "loop_off"}
        return "control_video", {"action": "loop_on"}
    if "fullscreen" in words:
        return "control_video", {"action": "fullscreen"}
    if "unmute" in words:
        return "control_video", {"action": "unmute"}
    if "mute" in words:
        return "control_video", {"action": "mute"}
    n = _number(t)
    if n is not None and ("volume" in t.all_words or "percent" in t.all_words) and 0 <= n <= 100:
        return "control_video", {"action": "volume", "percent": n}
    return None


YT_PATTERNS = [
    re.compile(r"^(?:play|watch|search|find|open|put) (?P<q>.+?) (?:on|in|from) youtube(?: (?:in|on))?$"),
    re.compile(r"^(?:search|find) (?:on |in )?youtube for (?P<q>.+)$"),
    re.compile(r"^youtube (?:search|play) (?P<q>.+)$"),
]


def r_youtube(t):
    """Only the plain sentences: "play X on youtube (in opera gx)", "search youtube for X", "open youtube"."""
    if t.objects or t.params or t.numbers or t.unknown:
        return None
    if any(e.entities[0].name.lower().strip() != "opera gx browser" for e in t.locations):
        return None                  # search_youtube opens Opera GX: "in chrome" isn't that
    core = " ".join(w for w in t.all_words if w not in FILLERS or w == "for")
    if re.fullmatch(r"(?:open|start|launch) youtube", core):
        return "open_in_browser", {"url": "https://www.youtube.com"}
    for p in YT_PATTERNS:
        m = p.match(core)
        if m:
            q = m.group("q").split()
            while q and q[0] == "for":          # "search for X on youtube"
                q = q[1:]
            if 0 < len(q) <= 6 and not any(w in NOT_COUNTED or w in PREPS for w in q):
                return "search_youtube", {"query": " ".join(q)}
    return None


def r_browser(t):
    if _has_prep(t) or not _only(t, {"browser"}, OPEN_VERBS | {"go"}):
        return None
    if len(t.objects) == 1 and not t.slot and not t.unknown and not t.numbers:
        e = _one_entity(t.objects[0], ("site",))
        if e:
            return "open_in_browser", {"url": e.ref}
    return None


def r_app_launch(t):
    if len(t.objects) != 1 or t.slot or t.unknown or t.numbers or t.locations or t.params:
        return None
    # "open VS Code in Documents/Projects/Umay", "inside Yumei", "on the other monitor": not just launching it
    if _has_prep(t) or not _only(t, {"app_launch"}, {"play"}):
        return None
    e = _one_entity(t.objects[0], ("app", "game"))
    return ("launch_application", {"app_id": e.ref}) if e else None


def r_app_close(t):
    if len(t.objects) != 1 or t.slot or t.unknown or t.numbers or t.locations or t.params:
        return None
    if _has_prep(t) or not _only(t, {"app_close"}):
        return None
    e = _one_entity(t.objects[0], ("app", "game"))
    if not e:
        return None
    return "close_app", {"name": re.sub(r"\s+browser\s*$", "", e.name.strip(), flags=re.I)}


def r_files(t):
    if len(t.objects) != 1 or t.slot or t.unknown or t.numbers or t.verb not in OPEN_VERBS or t.params:
        return None
    if _has_prep(t) or t.locations or not _only(t, {"files"}):
        return None                      # "open umay in vs code", "the documents projects file"
    kinds = ("folder",) if {"folder", "folders", "directory"} & set(t.words) else \
        ("file",) if {"file", "files", "document", "txt", "pdf"} & set(t.words) else ("file", "folder")
    e = _one_entity(t.objects[0], kinds)
    return ("open_path", {"path": e.ref}) if e else None


def _duration_minutes(words):
    """'in 10 minutes', 'in 2 hours', 'in an hour', 'in half an hour' -> minutes, or None."""
    text = " ".join(words)
    m = re.search(r"\bin (\d+(?:\.\d+)?) (minute|minutes|min|mins|hour|hours|hr|hrs)\b", text)
    if m:
        n = float(m.group(1))
        return n * 60 if m.group(2).startswith("h") else n
    if re.search(r"\bin (an|one) hour\b", text):
        return 60.0
    if re.search(r"\bin half an hour\b", text):
        return 30.0
    return None


def r_shutdown(t):
    words = set(t.all_words)
    if t.objects or t.params or t.unknown or t.locations or not _only(t, {"shutdown"}):
        return None
    if {"cancel", "abort"} & words or ("stop" in words and "shut_down" in words):
        if t.numbers:
            return None
        return "cancel_shutdown", {}
    action = None
    if {"shut_down", "power_off"} & words or ("turn_off" in words and {"pc", "computer", "laptop"} & words):
        action = "shutdown"
    if {"restart", "reboot"} & words:
        action = "restart" if action is None else "conflict"
    if action in (None, "conflict"):
        return None
    if "question" in t.flags:
        return None
    minutes = _duration_minutes(t.all_words)
    if t.numbers and minutes is None:
        return None                                   # a number we can't read: never shut down now
    if {"when", "after", "finishes", "done", "run"} & words:
        return None
    if set(t.slot) - {"minutes", "minute", "min", "mins", "hour", "hours", "hr", "hrs", "half", "an"}:
        return None
    return "schedule_shutdown", {"action": action, "minutes": minutes or 0}


def r_lock(t):
    if t.verb != "lock" or t.objects or t.params or t.slot or t.unknown or t.numbers or t.locations:
        return None
    if not _only(t, {"lock"}, {"screen"}):
        return None
    return "lock_pc", {}


def _clock(words, now):
    """'at 7 pm', 'at 18:30', 'tomorrow at 9 am' -> datetime, None if no time, 'ambiguous' if unclear."""
    text = " ".join(words)
    m = re.search(r"\b(?:at|for) (\d{1,2})(?::(\d{2}))?(?: (am|pm|oclock))?\b", text)
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap == "pm" and h < 12:
        h += 12
    elif ap == "am" and h == 12:
        h = 0
    elif ap in (None, "oclock") and 1 <= h <= 12:
        if "morning" in words and h < 12:
            pass
        elif ({"evening", "tonight"} & set(words)) and h < 12:
            h += 12
        else:
            return "ambiguous"
    if h > 23 or mi > 59:
        return "ambiguous"
    when = now.replace(hour=h, minute=mi, second=0, microsecond=0)
    if "tomorrow" in words:
        when += timedelta(days=1)
    elif when <= now:
        when += timedelta(days=1)
    return when


REMIND_STOP = FILLERS | PREPS | NOT_COUNTED | TIME_WORDS | {"remind", "reminder", "alarm", "wake_up", "set", "add",
                                                           "create", "new", "and", "every", "that"}


def r_alarms(t, now=None):
    now = now or datetime.now()
    words = t.all_words
    ws = set(words)
    if t.objects:
        if t.verb in TOGGLE_VERBS and len(t.objects) == 1 and not t.unknown:
            e = _one_entity(t.objects[0], ("alarm",))
            if e:
                return "set_alarm_active", {"name": e.name, "active": t.verb in ("turn_on", "enable")}
        return None
    if not ({"remind", "wake_up"} & ws or ({"reminder", "alarm"} & ws and ws & {"set", "add", "create", "new"})):
        return None
    if t.unknown or t.params or t.locations or not _only(t, {"alarms"}):
        return None
    repeat = "none"
    if "daily" in ws or re.search(r"\bevery (day|morning|evening|night)\b", " ".join(words)):
        repeat = "daily"
    elif "weekly" in ws or re.search(r"\bevery week\b", " ".join(words)):
        repeat = "weekly"
    elif "hourly" in ws or re.search(r"\bevery hour\b", " ".join(words)):
        repeat = "hourly"
    minutes = _duration_minutes(words)
    when = None if minutes is not None else _clock(words, now)
    if when == "ambiguous" or (minutes is None and when is None):
        return None
    if len(t.numbers) > 1:
        return None
    # the text: what follows "to" / "about" (remind me to X ...), else "Wake up" / "Alarm"
    text_words = list(t.text)        # the words of "remind me to <text>", as said
    while text_words and text_words[-1] in FILLERS:
        text_words.pop()
    while text_words and text_words[0] in ("the", "a", "an", "please"):
        text_words.pop(0)
    if "remind" in ws or "reminder" in ws:
        if not text_words:
            return None
        text = " ".join(text_words)
        text = text[0].upper() + text[1:]
    else:
        text = "Wake up" if "wake_up" in ws else "Alarm"
    params = {"text": text, "repeat": repeat}
    if minutes is not None:
        params["in_minutes"] = minutes
    else:
        params["at"] = when.strftime("%Y-%m-%d %H:%M")
    return "add_reminder", params


RESOLVERS = {"media": r_media, "volume": r_volume, "video": r_video, "youtube": r_youtube, "browser": r_browser,
             "app_launch": r_app_launch, "app_close": r_app_close, "files": r_files, "shutdown": r_shutdown,
             "lock": r_lock, "alarms": r_alarms}
L1_ALLOWED = {"media_key", "set_volume", "control_video", "search_youtube", "open_in_browser", "launch_application",
              "close_app", "open_path", "schedule_shutdown", "cancel_shutdown", "lock_pc", "add_reminder",
              "set_alarm_active"}


READS = {("set_volume", ())}   # answers a question; every other layer-1 action changes something


def resolve(t):
    fn = RESOLVERS.get(t.top)
    if fn is None:
        return None
    try:
        out = fn(t)
    except Exception:
        return None
    if not out or out[0] not in L1_ALLOWED:
        return None
    if "question" in t.flags and (out[0], tuple(out[1])) not in READS:
        return None                  # "why did you close discord", "how do I open notepad"
    return out


# ---------------------------------------------------------------- decision
@dataclass
class TaskDecision:
    task: Task
    layer: int
    action: tuple = None
    tools: list = field(default_factory=list)


@dataclass
class Plan:
    text: str
    tasks: list
    layer: int

    @property
    def local_actions(self):
        return [d.action for d in self.tasks if d.layer == 1]

    @property
    def tools(self):
        out = []
        for d in self.tasks:
            if d.layer == 2:
                for name in d.tools:
                    if name not in out:
                        out.append(name)
        return out


class Router:
    def __init__(self, entities=(), **thresholds):
        self.entities = list(entities)
        self.by_first = defaultdict(list)
        for e in self.entities:
            self.by_first[e.tokens[0]].append(e)
        self.th = {**DEFAULTS, **thresholds}

    def analyse(self, text, w_verb=None):
        """The tasks with their scores (thresholds aside, so a grid search scores once per W_VERB)."""
        w_verb = self.th["W_VERB"] if w_verb is None else w_verb
        items = match_entities(tokenize(text), self.entities, self.by_first)
        tasks = [score(its, dep, w_verb) for its, dep in split_tasks(items)]
        for t in tasks:
            t.action = None if t.blocked else resolve(t)
        return tasks

    @staticmethod
    def decide(text, tasks, th):
        decisions = []
        for t in tasks:
            if t.action and t.G >= th["L1_GW"] and t.margin >= th["L1_MARGIN"]:
                decisions.append(TaskDecision(t, 1, t.action))
            elif t.S1 >= th["L2_S1"]:
                tools = [n for c, v in t.ranked() if v > 0 for n in CATEGORIES[c][0]]
                decisions.append(TaskDecision(t, 2, tools=tools))
            else:
                decisions.append(TaskDecision(t, 3))
        layers = [d.layer for d in decisions] or [3]
        layer = 3 if 3 in layers else (2 if 2 in layers else 1)
        if layer == 3:
            for d in decisions:   # one call with everything: nothing runs locally
                d.layer, d.action = 3, None
        return Plan(text, decisions, layer)

    def route(self, text):
        return self.decide(text, self.analyse(text), self.th)


# ---------------------------------------------------------------- Umay's router, shadow log
REFRESH_SECONDS = 600         # installed apps / folders / alarms are read again this often (in the background)
LOG_NAME = "router_shadow.jsonl"

_live = {"router": Router(), "loaded": 0.0, "loading": False}
_lock = threading.Lock()


def mode():
    """"shadow" (default): decide and log, run nothing. "off": nothing at all."""
    return "off" if os.environ.get("UMAY_ROUTER", "").strip().lower() == "off" else "shadow"


def log_path():
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Umay" / LOG_NAME


def _refresh():
    try:
        router = Router(load_entities(live_data()))
        with _lock:
            _live["router"], _live["loaded"] = router, time.time()
    except Exception as e:     # a broken Get-StartApps must never touch Umay's answers
        print(f"Router: couldn't read the local names: {e!r}")
    finally:
        _live["loading"] = False


def current():
    """The router with the latest local names; starts a background refresh when they are old (until the first one
    finishes, names aren't known and only name-free commands reach layer 1)."""
    with _lock:
        stale = time.time() - _live["loaded"] > REFRESH_SECONDS and not _live["loading"]
        if stale:
            _live["loading"] = True
    if stale:
        threading.Thread(target=_refresh, daemon=True).start()
    return _live["router"]


def warm_up():
    """Reads the local names now (main.py at start, off the main thread)."""
    if mode() != "off":
        current()


def route(text):
    """The plan for a message, or None (off, or the router failed: never in the way of the answer)."""
    if mode() == "off":
        return None
    try:
        return current().route(owner_text(text))
    except Exception as e:
        print(f"Router: {e!r}")
        return None


def _task_record(d):
    t = d.task
    return {"layer": d.layer, "action": list(d.action) if d.action else None,
            "would": list(t.action) if t.action else None,
            "scores": {c: round(v, 3) for c, v in t.ranked()}, "T_w": round(t.T, 3), "G_w": round(t.G, 3),
            "margin": round(t.margin, 3), "S1": round(t.S1, 3), "flags": sorted(t.flags), "slot": t.slot,
            "unknown": t.unknown}


def log(plan, source, tool_calls, seconds, answer=""):
    """One line per message the model handled: the router's decision next to what the model really did."""
    if plan is None:
        return
    record = {"at": datetime.now().isoformat(timespec="seconds"), "source": source, "text": plan.text,
              "layer": plan.layer, "local": [list(a) for a in plan.local_actions],
              "tasks": [_task_record(d) for d in plan.tasks],
              "model_tools": [[name, args] for name, args in tool_calls], "model_seconds": round(seconds, 2),
              "answer": (answer or "")[:200]}
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with _lock, open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- the review (python router.py review)
def _same(action, call):
    """Does the model's tool call do what the router's action would have?"""
    tool, params = action
    name, args = call
    if name != tool:
        return False
    args = args or {}
    keys = {"media_key": ["action"], "set_volume": ["level", "change", "mute"], "control_video": ["action", "percent"],
            "close_app": ["name"], "open_path": ["path"], "launch_application": ["app_id"], "lock_pc": [],
            "schedule_shutdown": ["action", "minutes"], "cancel_shutdown": [], "open_in_browser": ["url"]}.get(tool)
    if keys is None:
        return True            # search_youtube / add_reminder: the words differ, read them in the list
    norm = lambda v: re.sub(r"[^a-z0-9.]", "", str(v).lower()) if v is not None else None
    return all(norm(params.get(k)) == norm(args.get(k)) for k in keys)


def review(path=None, since=None):
    """What layer 1 would have done, message by message, against what the model did. Returns the text."""
    path = Path(path or log_path())
    if not path.exists():
        return f"No shadow log yet ({path})."
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if since is None or r["at"] >= since:
            records.append(r)
    local = [r for r in records if r["local"]]
    lines = [f"{len(records)} messages went to the model; layer 1 would have taken {len(local)} "
             f"({len(local) / max(1, len(records)):.0%})."]
    kinds = Counter()
    for r in local:
        calls = r["model_tools"]
        if all(any(_same(a, c) for c in calls) for a in r["local"]):
            kind = "same as the model"
        elif not calls:
            kind = "MODEL DID NOTHING"
        else:
            kind = "DIFFERENT from the model"
        kinds[kind] += 1
        lines.append(f"\n[{kind}] {r['at']} ({r['source']}) {r['text'][:120]!r}\n"
                     f"   router would: {r['local']}\n"
                     f"   model did:    {[c[0] + json.dumps(c[1] or {}, ensure_ascii=False) for c in calls][:5]}"
                     f" in {r['model_seconds']} s")
    lines.insert(1, "  " + ", ".join(f"{k}: {v}" for k, v in kinds.most_common()))
    lines.append("\nCheck every DIFFERENT / MODEL DID NOTHING line: which one was right? A wrong router action "
                 "there means a rule to fix before layer 1 is turned on.")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    if sys.argv[1:2] == ["review"]:
        print(review(since=sys.argv[2] if len(sys.argv) > 2 else None))
    else:
        print("python router.py review [YYYY-MM-DD]")
