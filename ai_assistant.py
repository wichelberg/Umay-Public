import base64
import json
import os
import re
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests
from pynput import keyboard
from tavily import TavilyClient

import i18n
import paths
import audio_devices
import browser_tools
import calendar_tools
import chat_box
import credit_log
import day_plan
import disk_watch
import calls
import claude_terminal
import eyes
import face
import heavy_model
import hud
import input_tools
import local_llm
import mail_tools
import npu
import memory_cortex
import memory_editor
import market_outlook
import markets_follow
import media_tools
import news_follow
import pc_tools
import price_alerts
import reminder_tools
import router
import screen_tools
import sleep_mode
import spotify_tools
import steam_tools
import system_tools
import task_plan
import tool_pick
import voice
import voice_confirm
import web_control
import web_find
import whatsapp_tools
import window_tools

AI_NAME = "Umay"
USER_NAME = os.environ.get("UMAY_USER_NAME", "").strip() or "You"

CHAT_LOG_DIR = paths.app_dir() / "chat_logs"
CHAT_LOG_DIR.mkdir(exist_ok=True)

MAX_TOOL_ROUNDS = 40  # a task done with the mouse and keyboard takes a round per click
STEP_ROUNDS = 12      # one step of a plan (task_plan)
CONFIRM_TIMEOUT_SECONDS = 300


def _call_with_retry(fn, retries=2, delay=2.0):
    """Retries on transient connection/server errors before giving up.
    Raises immediately on 4xx errors (bad key, rate limit, etc.) since retrying won't help."""
    last_error = None
    for attempt in range(retries + 1):
        try:
            return fn()
        except requests.exceptions.ConnectionError as e:
            last_error = e
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code < 500:
                raise
            last_error = e
        if attempt < retries:
            time.sleep(delay)
    raise last_error


_tavily_client = TavilyClient(api_key=os.environ.get("TAVILY_API_KEY"))  # .env is loaded by paths


def _load_personality():
    """Umay's persona: personality.md, bundled next to the code (paths.resource_dir), and how she talks (STYLE)."""
    return (paths.resource_dir() / "personality.md").read_text(encoding="utf-8").strip() + "\n\n" + STYLE


# How she talks (the owner, 2026-11-04: shorter, discreet, Jarvis-like lines, "dear" instead of "sir"; she built
# paragraphs for everything). Not in personality.md: that file is also the owner's Telegram persona for Claude.
STYLE = (
    "How you talk: like Jarvis, a composed and discreet assistant, but warm. You are speaking to the owner "
    "directly: say \"you\", never \"the owner\" or \"the user\". Short, plain, goal-focused "
    "lines: usually one sentence, two or three only when the content needs it; longer only when the user asks "
    "for an explanation or details. Lead with the result. No filler: don't restate the request, don't describe "
    "the screen or a tool's output unless asked, no \"Let me know if you need anything else\", no \"I hope\", "
    "no \"Great question\". Call the user \"dear\" the way Jarvis says \"sir\" (never \"sir\"): often, at most "
    "once in a reply. After a tool did what was asked, a few words are enough (\"Done, dear.\"); when it "
    "couldn't, say what is missing in one line (\"I couldn't find that folder. Another name, perhaps?\"). Never "
    "say that something is done, found or open when no tool did it for this message: to thanks, a remark or "
    "small talk, just answer the person."
)
# Until 2026-11-10 the tone was shown with four lines to copy ("Done, dear." "On it." "Found it, dear. It's open
# now." ...), and the 4b copied them: "not really" got "Found it, dear. It's open now. On it.", "Thank you love" got
# "Done, dear.", and her own news lines opened with "Found it, dear." (companion.py uses this text too).


# The last two rules are for the small local model: it added details no tool had returned ("you were
# stuck on input validation" for "the user talked about a Python homework") and emoji (2026-11-02).
TOOL_HINTS = (
    "Always reply in English, whatever language the user writes in. For WhatsApp use whatsapp_chats, "
    "whatsapp_messages and send_whatsapp_file, which work through the WhatsApp Desktop app; never WhatsApp "
    "Web in the browser. After a tool runs, say only what its result says: never add details it didn't "
    "return. No emoji. Use the mouse and keyboard (click, scroll, type_text, press_keys) only when no other "
    "tool can do the task: look_at_screen first, then one step at a time, calling the next tool right away "
    "without describing it first; answer only when the task is done or you are stuck. To work on another "
    "monitor (or when asked to move the cursor there), call move_mouse first. To find a page inside a website "
    "(a course, a setting, a product) use web_find_in_site; for Opera GX itself the web_* tools come before "
    "the mouse and keyboard. To move, minimize, maximize or bring forward an app's window use window_control. "
    "You can't remove anything from the phone calendar: say so, and never add an event instead. For any mail use "
    "draft_mail, never the mouse: you can't send mail, the user presses Send. You have no way to save to memory "
    "yourself: never say you added, saved, noted or recorded something. Say instead that you will learn it when "
    "the conversation ends (saying goodbye makes it now)."
)

# The small local model, partway through a task on the screen, often answers "Let me click the field..."
# and stops there (measured 2026-11-02). Such an answer gets GO_ON, at most NUDGES times per request.
SCREEN_TOOLS = {"look_at_screen", "click", "scroll", "type_text", "press_keys", "move_mouse"}
WORKING_TOOLS = SCREEN_TOOLS | {"window_control", "list_open_apps"}  # "I'll click it now" after these stops short too
ANNOUNCES_STEP = re.compile(r"\b(let me|let's|i'll|i will|i'm going to|now i|next,? i)\b", re.IGNORECASE)
GO_ON = "Go on with the task: call the tool for that step now."
NUDGES = 3
CHECK_WAIT_S = 1.0    # before the screenshot for a plan step's check
RESULT_SHOWN = 900    # of a tool's answer, for that check: at 300 a list of 17 tabs ended before the new one

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_KEY_ENV = "OPENROUTER_API_KEY"

# Umay's one mode: a model + her persona, served through an OpenAI-compatible endpoint: the local model
# (local_llm.py, Ollama) unless UMAY_LLM=openrouter. Chat history is stored in OpenAI chat format;
# chat["mode"] is always "umay".
if local_llm.on():
    _provider = {"provider": "Ollama", "url": local_llm.CHAT_URL, "key_env": None, "model": local_llm.model(),
                 "local": True, "picks": local_llm.picks(), "sees": local_llm.sees()}
else:
    _provider = {"provider": "OpenRouter", "url": OPENROUTER_URL, "key_env": OPENROUTER_KEY_ENV,
                 "model": os.environ.get("OPENROUTER_MODEL", "").strip() or "google/gemini-3.7-flash",
                 "local": False, "picks": False, "sees": True}
# "picks" (UMAY_TOOL_CHOICE=npu, local_llm.picks): the model never sees the tool list. The NPU's decision model
# names each tool, this model writes its arguments and speaks (_picked_loop), so its prompt is about answering
# from results, not about which tool to call.
PICKED_HINTS = (
    "Always reply in English, whatever language the user writes in. You don't run tools yourself: when the user's "
    "message needed one, it has already run, and its result is in the conversation in [square brackets]. Say what "
    "that result says in your own voice: never add details it didn't return, and never say something is done, "
    "open, sent, saved or found unless a result says so. When no result is there for something the user asked "
    "you to do, say plainly that you couldn't do it. What you know about the user is only what this conversation "
    "and the notes in it say: asked something about them that isn't there, say you don't know it yet, never "
    "guess. The same for the world: the weather, the news, a price, what someone did lately are only known when "
    "a result holds them; without one, say you haven't looked it up. Don't name your tools or their settings. No "
    "emoji. You can't send mail: you open a draft and the user "
    "presses Send. You have no way to save to memory yourself: never say you added, saved, noted or recorded "
    "something. Say instead that you will learn it when the conversation ends (saying goodbye makes it now)."
)
MODES = {"umay": {**_provider,
                  "system": f"{_load_personality()}\n\n{PICKED_HINTS if _provider['picks'] else TOOL_HINTS}"}}

_state = {"listener": None, "chat": None, "log_path": None}  # chat/log_path: the open text box
_lock = threading.Lock()
_memory = threading.local()  # .note: this request's memories (memory_cortex.context_for), for its requests only
_fill = threading.local()    # .used: tokens the last request took; .cut: its reply hit the window (_request_fitting)
# Tokens left free in the local model's window before the history is summarized. Not more: the 69 tools and the
# prompt alone are ~13,500 of 16,384. At 3500 every request was over the line, so 64 of 87 turns on 2026-11-08 had the
# whole conversation squeezed into six bullets, and "yes, please" lost what it answered. The longest reply: 1105.
CONTEXT_ROOM = 1500
SHRINK_TO = 500              # chars a tool's answer keeps when the current turn alone doesn't fit (_shrink_turn)
OUTWARD_SEARCHES = {"web_search", "search_youtube", "web_find_in_site"}  # what they're asked goes to the internet
# "There's a short on my screen, open it": the 4b searched YouTube for it instead (2026-11-08). Also only these tools.
SCREEN_RULE = ("\n(The owner means something that is already on the screen. Call look_at_screen first, then click it, "
               "with `target` naming it. Never search for it and never open a new page.)")
# Last in every request, where the 4b heeds it: in TOOL_HINTS alone it still said "my memory resets when we end the
# chat" and, another time, "I've added them to your memory" (2026-11-08).
MEMORY_RULE = ("\n(Your memory: what the owner tells you is learnt on its own once this conversation ends. Never say "
               "you forget it or can't remember it, and never say you already saved it.)")
# Only on a message that is about her memory or tells her something about the owner. On every message (until
# 2026-11-10) the 4b answered the rule: "thank you love" got "I'll keep that in mind for our conversations", a
# goodbye "I'll make sure to remember that", and the reply to a news story was about her own memory.
MEMORY_ASKED = re.compile(
    r"\b(?:remember\w*|memor\w+|forget\w*|forgot\w*|sav(?:e|ed|ing)|not(?:e|ed)\s+(?:it|that|this)|learn\w*|"
    r"hat[ıi]rla\w*|unut\w*|kaydet\w*|akl[ıi]nda)\b", re.IGNORECASE)
_NOT_A_FACT = r"(?!not\b|waiting|going|trying|looking|talking|just|here|back|done|sorry|about|great|fine|good|ok)"
OWNER_TELLS = re.compile(
    rf"\b(?:i\s+am\s+{_NOT_A_FACT}|i'm\s+{_NOT_A_FACT}|i\s+(?:also\s+|really\s+|just\s+)?(?:like|love|hate|live|use|"
    r"own|bought|study)\b|my\s+[\w']+(?:\s+[\w']+)?\s+(?:is|are|was)\b|my\s+name\b|"
    # how she should address them or behave is told to her memory as well: "Only use my middle name"
    r"call\s+me\b|from\s+now\s+on\b|only\s+use\b|always\b|never\b)", re.IGNORECASE)
# Thanks, a goodbye, "nothing", "not really": nothing to do, so the 4b gets no tools for it. With them it called
# search_memory, web_search and follow_news on "Nothing love. Thank you bye" and answered "I've removed WRC from your
# newsfeed" (2026-11-09; real 4b on five such lines from the logs, 5 runs each: a tool in 7 of 25). Not when the
# line starts by agreeing ("okay, thank you" may accept an offer) unless it also closes ("okay then", "bye").
SMALL_WORDS = {"thank", "thanks", "thx", "you", "u", "very", "much", "so", "a", "lot", "for", "informing", "telling",
               "that", "that's", "thats", "it", "the", "info", "help", "helping", "update", "bye", "goodbye", "see",
               "later", "night", "goodnight", "nothing", "no", "nope", "not", "really", "now", "else", "anything",
               "all", "then", "good", "great", "perfect", "nice", "cool", "lovely", "awesome", "well", "oh",
               "umay", "love", "dear", "darling", "honey", "my", "ok", "okay", "okey", "alright", "fine",
               "teşekkürler", "teşekkür", "tesekkur", "tesekkurler", "ederim", "sağol", "sagol", "sağ", "ol", "tamam",
               "peki", "yok", "hayır", "hayir", "görüşürüz", "gorusuruz", "hoşçakal", "iyi", "geceler", "eyvallah"}
SMALL_CORE = {"thank", "thanks", "thx", "bye", "goodbye", "goodnight", "night", "later", "nothing", "no", "nope", "not",
              "teşekkürler", "teşekkür", "tesekkur", "tesekkurler", "sağol", "sagol", "yok", "hayır", "hayir",
              "görüşürüz", "gorusuruz", "hoşçakal", "geceler", "eyvallah"}
SMALL_HINTS = "Always reply in English, whatever language the user writes in. No emoji."
# Today's date goes after the owner's message only when the message or its memories are about a time. After every
# message (until 2026-11-10) the 4b read it back: "Who am I?" got "Today is Monday, November 9th. Tomorrow will be
# Tuesday", a thank-you "Happy Tuesday morning". The clock stays in the system prompt for everything else.
TIME_ASKED = re.compile(
    r"\d|\b(?:today|tonight|tomorrow|yesterday|now|date|days?|weeks?|weekend|months?|years?|time|clock|hours?|"
    r"minutes?|morning|afternoon|evening|night|noon|midnight|when|alarms?|remind\w*|calendar|events?|schedul\w*|"
    r"plans?|meetings?|appointments?|deadline|birthday|old|age|born|early|late|later|soon|ago|since|until|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|january|february|march|april|may|june|july|august|"
    r"september|october|november|december|bug[üu]n|yar[ıi]n|d[üu]n|saat\w*|tarih\w*|g[üu]n\w*|hafta\w*|ay|"
    r"y[ıi]l\w*|zaman|hat[ıi]rlat\w*|takvim\w*|ka[cç])\b", re.IGNORECASE)
SMALL_AGREES = {"ok", "okay", "okey", "alright", "fine", "tamam", "peki"}
SMALL_CLOSES = {"then", "bye", "goodbye", "nothing", "no", "nope", "not", "görüşürüz", "gorusuruz", "hoşçakal", "yok",
                "hayır", "hayir"}
SMALL_WORDS_MOST = 9


def small_talk(text):
    """True for a message that only thanks, says goodbye or declines."""
    words = re.findall(r"[^\W\d_]+(?:'[^\W\d_]+)?", memory_cortex.HINT.sub("", text or "").lower().replace("’", "'"))
    if not words or len(words) > SMALL_WORDS_MOST or not all(w in SMALL_WORDS for w in words):
        return False
    if words[0] in SMALL_AGREES and not SMALL_CLOSES & set(words):
        return False
    return bool(SMALL_CORE & set(words))
# The text box, yes/no box and the pet's lines (no window toggle any more): UMAY_LANGUAGE, English by default.
_language = os.environ.get("UMAY_LANGUAGE", "").strip().lower() or "en"
if _language not in i18n.STRINGS:
    _language = i18n.DEFAULT_LANGUAGE


# A query asking for something recent searches only the last month. The local model doesn't put the year
# in its queries (told to, it wrote "2024 2025"): "most recent Formula 1 race winner" got 2024's Monaco GP,
# with time_range "month" the October 2026 race (2026-11-02).
RECENT_QUERY = re.compile(r"\b(latest|recent|newest|current|news|today|tonight|yesterday|now|this (week|month))\b",
                          re.IGNORECASE)


NEWS_ASKED = re.compile(r"\b(?:news|headlines?|breaking|haber\w*|g[üu]ndem\w*|son\s+dakika)\b", re.IGNORECASE)
NEWS_HOURS = 24


def _todays(results, now=None):
    """Only what was published in the last NEWS_HOURS, each with its date ("day"): the 4b sees when, and a result
    with no date isn't trusted to be new."""
    now = now or datetime.now(timezone.utc)
    kept = []
    for r in results:
        try:
            at = parsedate_to_datetime(r.get("published_date") or "")
        except (TypeError, ValueError):
            continue
        at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
        if timedelta(0) <= now - at <= timedelta(hours=NEWS_HOURS) or timedelta(hours=-3) <= now - at < timedelta(0):
            kept.append({**r, "day": f"{at.astimezone():%d %B %Y, %H:%M}"})
    return kept


def web_search(query: str) -> str:
    """Searches the web for current, up-to-date, or real-world information.

    Use this whenever the answer depends on recent, specific, or verifiable
    facts (news, prices, specs, dates, current events) rather than general
    knowledge you're already confident about.

    Args:
        query: The search query to look up on the web.
    """
    credit_log.before_search()
    news = bool(NEWS_ASKED.search(query) or NEWS_ASKED.search(getattr(_memory, "owner_said", "")))
    filters = {"topic": "news", "time_range": "day"} if news else \
        ({"time_range": "month"} if RECENT_QUERY.search(query) else {})
    try:
        response = _tavily_client.search(query, max_results=5, **filters)
    except Exception as e:
        return i18n.get(_language, "web_search_failed", error=e)

    results = response.get("results", [])
    if news:  # the owner's rule, 2026-11-08: news is today's or it isn't said at all
        results = _todays(results)
        if not results:
            return "No news from the last 24 hours about that. Say so; don't use older news."
        return "\n".join(f"- {r.get('title', '')} ({r.get('url', '')}) [published {r['day']}]\n  {r.get('content', '')}"
                         for r in results)
    if not results:
        return i18n.get(_language, "web_search_no_results")

    return "\n".join(
        f"- {r.get('title', '')} ({r.get('url', '')})\n  {r.get('content', '')}"
        for r in results
    )


MEMORY_STOP = {"user", "owner", "the", "and", "what", "where", "when", "who", "how", "you", "your", "about",
               "with", "that", "this", "did", "does", "said", "told"}


def search_memory(query: str) -> str:
    """Searches past saved conversations for something the user refers to.

    Use this when the user asks whether you remember something from an
    earlier conversation, or references something discussed before.

    Args:
        query: Keywords describing what to look for in past conversations.
    """
    keywords = [w.lower() for w in re.findall(r"\w+", query) if len(w) > 2 and w.lower() not in MEMORY_STOP]
    if not keywords:
        return i18n.get(_language, "memory_no_keywords")
    # Word stems, so "lives" finds "live". Newest logs first: oldest-first with a cut at 10 never reached
    # the conversation a minute earlier (2026-11-07, "where do I live?").
    stems = {w[:max(4, len(w) - 2)] for w in keywords}
    known = memory_cortex.search_text(query)

    current_log = _state.get("log_path")
    matches = []
    newest_first = sorted(CHAT_LOG_DIR.glob("*.txt"), reverse=True,
                          key=lambda p: memory_cortex.said_at(p.name) or p.stat().st_mtime * 1000)
    for log_file in newest_first:
        if log_file == current_log:
            continue  # skip the still-open current session
        try:
            lines = log_file.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        seen_contexts = set()
        for i, line in enumerate(lines):
            if any(s in line.lower() for s in stems):
                context = "\n".join(lines[max(0, i - 1): i + 2])
                if context in seen_contexts:
                    continue
                seen_contexts.add(context)
                matches.append(f"[{log_file.stem}]\n{context}")
        if len(matches) >= 10:
            break

    parts = ([f"From long-term memory:\n{known}"] if known else []) \
        + ([f"From past conversation logs, newest first:\n" + "\n\n".join(matches[:10])] if matches else [])
    if not parts:
        return i18n.get(_language, "memory_no_matches")
    return "\n\n".join(parts)


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Searches the web for current, up-to-date, or real-world information. "
                "Use this whenever the answer depends on recent, specific, or verifiable "
                "facts (news, prices, specs, dates, current events) rather than general "
                "knowledge you're already confident about."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query to look up on the web."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_memory",
            "description": (
                "Searches past saved conversations for something the user refers to. "
                "Use this when the user asks whether you remember something from an "
                "earlier conversation, or references something discussed before."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keywords describing what to look for in past conversations."},
                },
                "required": ["query"],
            },
        },
    },
] + (pc_tools.TOOLS + browser_tools.TOOLS + system_tools.TOOLS + steam_tools.TOOLS
     + screen_tools.TOOLS + input_tools.TOOLS + eyes.TOOLS + face.TOOLS + claude_terminal.TOOLS + calls.TOOLS + media_tools.TOOLS + spotify_tools.TOOLS + reminder_tools.TOOLS + calendar_tools.TOOLS + whatsapp_tools.TOOLS
     + web_control.TOOLS + web_find.TOOLS + sleep_mode.TOOLS + news_follow.TOOLS + markets_follow.TOOLS + market_outlook.TOOLS
     + price_alerts.TOOLS
     + day_plan.TOOLS + window_tools.TOOLS + mail_tools.TOOLS)

TOOL_FUNCTIONS = {
    "web_search": web_search,
    "search_memory": search_memory,
    **pc_tools.TOOL_FUNCTIONS,
    **browser_tools.TOOL_FUNCTIONS,
    **system_tools.TOOL_FUNCTIONS,
    **window_tools.TOOL_FUNCTIONS,
    **mail_tools.TOOL_FUNCTIONS,
    **steam_tools.TOOL_FUNCTIONS,
    **screen_tools.TOOL_FUNCTIONS,
    **input_tools.TOOL_FUNCTIONS,
    **eyes.TOOL_FUNCTIONS,
    **face.TOOL_FUNCTIONS,
    **claude_terminal.TOOL_FUNCTIONS,
    **calls.TOOL_FUNCTIONS,
    **media_tools.TOOL_FUNCTIONS,
    **spotify_tools.TOOL_FUNCTIONS,
    **reminder_tools.TOOL_FUNCTIONS,
    **calendar_tools.TOOL_FUNCTIONS,
    **whatsapp_tools.TOOL_FUNCTIONS,
    **web_control.TOOL_FUNCTIONS,
    **web_find.TOOL_FUNCTIONS,
    **sleep_mode.TOOL_FUNCTIONS,
    **news_follow.TOOL_FUNCTIONS,
    **markets_follow.TOOL_FUNCTIONS,
    **market_outlook.TOOL_FUNCTIONS,
    **price_alerts.TOOL_FUNCTIONS,
    **day_plan.TOOL_FUNCTIONS,
}


# While these run the HUD's orb shows "researching"; while any other tool runs, "working".
RESEARCH_TOOLS = {"web_search", "web_read_page", "search_youtube", "search_memory", "web_find_in_site",
                  "web_browser_data"}
# Their results come from outside (web pages, search results, other people's messages): after one of them,
# every confirm in that request carries a warning and every screen step asks (plan 20b, strict mode;
# pc_tools.mark_untrusted). web_control's tools also mark it themselves, with the links they saw.
UNTRUSTED_TOOLS = {"web_search", "web_read_page", "web_act", "web_tabs", "web_find_in_site", "web_browser_data",
                   "search_youtube", "whatsapp_messages", "whatsapp_chats"}


def _ask_confirmation(key, **kwargs):
    """Called from the worker thread by pc_tools before any change on the PC (a Telegram request asks on
    Telegram instead): she asks aloud and listens for yes or no (voice_confirm). Only when her voice
    can't (a game, no microphone) a yes/no box in the text box's neon look shows; blocks until it's
    answered (no after CONFIRM_TIMEOUT_SECONDS)."""
    question = i18n.render(_language, key, **kwargs)
    answer = voice_confirm.ask(question)
    if answer is not None:
        return answer
    return chat_box.confirm(i18n.get(_language, "confirm_title"), question,
                            i18n.get(_language, "confirm_yes"), i18n.get(_language, "confirm_no"),
                            CONFIRM_TIMEOUT_SECONDS)


pc_tools.confirm_handler = _ask_confirmation


def _notify(key, **kwargs):
    """Reports a finished background job started from the PC: in the text box if it's open,
    otherwise wherever pc_tools.fallback_notify sends it (Telegram)."""
    text = i18n.render(_language, key, **kwargs)
    if chat_box.is_open():
        chat_box.add(AI_NAME, text, "her")
    else:
        pc_tools.fallback_notify(text)


pc_tools.notify_handler = _notify


def answer_message(chat, text, language=None, source="other"):
    """Like _send_message, but turns any failure into a user-facing message instead of raising.
    `language` overrides the window's UI language for these messages.
    `source` (box, telegram, voice) is where the request came from, for the credit log."""
    lang = language or _language
    mode = MODES[chat["mode"]]
    if mode["key_env"] and not os.environ.get(mode["key_env"], "").strip():
        return i18n.get(lang, "invalid_api_key", provider=mode["provider"], env=mode["key_env"])
    try:
        with credit_log.activity(source, text):
            return _send_message(chat, text, source)
    except requests.exceptions.ConnectionError:
        return i18n.get(lang, "connection_error", provider=mode["provider"])
    except requests.exceptions.HTTPError as e:
        if e.response is None:
            return i18n.get(lang, "generic_error", error=e)
        status, body = e.response.status_code, e.response.text
    except Exception as e:
        return i18n.get(lang, "generic_error", error=e)

    if status in (401, 403):
        return i18n.get(lang, "invalid_api_key", provider=mode["provider"], env=mode["key_env"])
    if status == 404:
        return i18n.get(lang, "model_not_found", provider=mode["provider"], model=mode["model"])
    if status == 429:
        return i18n.get(lang, "rate_limited")
    if status >= 500:
        return i18n.get(lang, "server_overloaded", provider=mode["provider"])
    return i18n.get(lang, "generic_error", error=body)


def new_chat():
    """A chat session: its mode plus the history in OpenAI chat format (system
    prompt excluded; it's added per request)."""
    return {"mode": "umay", "messages": []}


# Last in the prompt, where the small local model heeds it: with the rule only in TOOL_HINTS it answered a
# Turkish message in (broken) Turkish (2026-11-02). After the clock, it costs ~20 uncached tokens a request.
# Its answers after a tool still came in Turkish (the prompt is ~8,000 tokens back by then), so the local
# model also gets LANGUAGE_REMINDER after the latest user message (in the request only, not the history).
LANGUAGE_RULE = "Reply in English only, even when the user writes in another language."
LANGUAGE_REMINDER = "\n\n(Reply in English.)"


def _system_prompt(mode):
    now = datetime.now()
    clock = f"Current local date and time: {now:%Y-%m-%d %H:%M} ({now:%A})."
    return f"{mode['system']}\n\n{clock}\n{LANGUAGE_RULE}" if mode["system"] else f"{clock}\n{LANGUAGE_RULE}"


def _tools(mode, tools=None):
    return TOOLS if tools is None else tools


def _headers(mode):
    headers = {"Content-Type": "application/json", "X-Title": "Umay"}
    if mode["key_env"]:
        headers["Authorization"] = f"Bearer {os.environ.get(mode['key_env'], '').strip()}"
    return headers


def _request_extras(mode):
    """OpenRouter reports each request's cost; the local model is told not to think first (Qwen3.5
    reasons before every answer by default, which is seconds of waiting in a spoken conversation)."""
    return {"reasoning_effort": "none"} if mode["local"] else {"usage": {"include": True}}


def _on_npu(mode):
    """A game has the GPU and the NPU serves a chat model (UMAY_NPU_CHAT): a picking mode's requests go there
    meanwhile. Weaker and slower (qwen3.5:4b: ~8 tokens a second), but it takes none of the game's VRAM. Only a
    picking mode: its requests are a few hundred tokens, the 71-tool prompt would take the NPU 30 s to read."""
    return bool(mode.get("picks")) and voice.gaming["on"] and npu.chat_ready()


def _post(mode, body, timeout, background=False):
    """One request to the mode's endpoint (the local model counts as busy meanwhile, for voice_guard). background:
    not a reply to the owner (ask_flash), so it doesn't keep the local model from resting (local_llm.dormant)."""
    if not mode["local"]:
        return requests.post(mode["url"], headers=_headers(mode), json=body, timeout=timeout)
    if _on_npu(mode):
        return npu.post_chat({k: v for k, v in body.items() if k != "reasoning_effort"}, timeout=max(timeout, 180))
    local_llm.ensure_model()
    with local_llm.working(background=background):
        # The first request after a game or a restart also loads the model (~5-10 s from disk).
        return requests.post(mode["url"], headers=_headers(mode), json=body, timeout=max(timeout, 120))


def _with_reminder(mode, messages):
    """messages for the request: the latest user text gets this request's memories (memory_cortex, never kept
    in the history) and, for the local model, LANGUAGE_REMINDER, which stays last."""
    extra = getattr(_memory, "note", "") + (
        (PICKED_REMINDER if mode.get("picks") else LANGUAGE_REMINDER) if mode["local"] else "")
    if not extra:
        return messages
    last = max((i for i, m in enumerate(messages) if m["role"] == "user" and isinstance(m.get("content"), str)),
               default=None)
    if last is None:
        return messages
    return messages[:last] + [{**messages[last], "content": messages[last]["content"] + extra}] \
        + messages[last + 1:]


# After the last result of a turn. Short on purpose: a longer one ("Now tell the user ... a sentence or two ...") came
# back in the answer as "(Note: I kept my response short and to the point, as per the guidelines.)" (Stheno, 2026-11-11).
ANSWER_NOW = "\n\n(Reply in English: one or two short sentences. No greeting, no offer of more help.)"
# The same words after the owner's message, where LANGUAGE_REMINDER goes for the other modes. Without them the
# roleplay model opened every reply with "Hello <name>," and closed with "Is there anything else I can help with
# today?", whatever STYLE says a thousand tokens earlier (2026-11-11).
PICKED_REMINDER = ANSWER_NOW
# A roleplay model's out-of-character note at the end of an answer: never the owner's to read.
ASIDE = re.compile(r"\s*[(\[](?:note|ooc|p\.?s\.?)\b[^()\[\]]*[)\]]\s*\Z", re.IGNORECASE)
RESULT_OPEN = "[Result of {name}]\n"
NO_PICTURE = "[A screenshot was here; you can't see pictures. Go by what the results say about it.]"


def _flat(messages, sees=True):
    """The conversation for a model with no tool calling of its own (a picking mode): a tool's answer becomes a
    user-side note in square brackets, the call itself is left out, and the last note of the turn asks for the
    answer. A copy: the history keeps the OpenAI tool format everything else reads (_transcript, the plan's check)."""
    names, out = {}, []
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            names.update({tc["id"]: tc["function"]["name"] for tc in m["tool_calls"]})
            if (m.get("content") or "").strip():
                out.append({"role": "assistant", "content": m["content"]})
        elif m["role"] == "tool":
            note = RESULT_OPEN.format(name=names.get(m.get("tool_call_id"), "the tool")) + str(m.get("content"))
            if out and out[-1]["role"] == "user" and out[-1].get("_result"):
                out[-1]["content"] += "\n\n" + note   # several results in a row: one note
            else:
                out.append({"role": "user", "content": note, "_result": True})
        elif isinstance(m.get("content"), list) and not sees:
            out.append({"role": "user", "content": NO_PICTURE, "_result": True})
        else:
            out.append(m)
    if out and out[-1].get("_result") and isinstance(out[-1]["content"], str):
        out[-1]["content"] += ANSWER_NOW
    return [{k: v for k, v in m.items() if k != "_result"} for m in out]


def _openai_compat_request(mode, messages, tools=None):
    """Returns (content, tool_calls, cost_in_usd) for the next assistant turn. `tools`: the tool schemas the
    model may use (all of them by default; one step of a plan gets fewer, an answer-only step none)."""
    schemas = [] if mode.get("picks") else _tools(mode, tools)  # a picking mode's model is never shown tools
    sent = _with_reminder(mode, messages)
    response = _post(mode, {
        "model": mode["model"],
        "messages": [{"role": "system", "content": _system_prompt(mode)}]
        + (_flat(sent, mode.get("sees", True)) if mode.get("picks") else sent),
        **({"tools": schemas} if schemas else {}),
        **_request_extras(mode),
    }, timeout=60)
    response.raise_for_status()
    data = response.json()
    credit_log.record(mode["model"], data.get("usage"))
    usage = data.get("usage") or {}
    _fill.used = int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)
    _fill.cut = data["choices"][0].get("finish_reason") == "length"
    message = data["choices"][0]["message"]
    tool_calls = message.get("tool_calls") or []
    for tc in tool_calls:
        tc["id"] = tc.get("id") or f"call_{uuid.uuid4().hex[:12]}"  # some models leave ids empty
    return message.get("content") or "", tool_calls, float((data.get("usage") or {}).get("cost") or 0)


# Ollama: "request (16445 tokens) exceeds the available context size (16384 tokens)"; OpenRouter's providers:
# "maximum context length is ...". The history had no limit: on Telegram (kept for an hour between messages) a
# few screen tasks filled the local model's 16k window, and every later message failed, each a little longer
# than the last, since the failed message stayed in it (2026-11-03).
TOO_LONG = re.compile(r"exceeds the available context|context length|context size|too many tokens", re.IGNORECASE)


def _too_long(error):
    r = error.response
    return r is not None and r.status_code == 400 and bool(TOO_LONG.search(r.text or ""))


SUMMARY_PREFIX = "[Summary of our conversation so far]\n"
SUMMARY_PROMPT = ("Summarize this conversation between the user and Umay, their assistant, as briefly as you can, "
                  "in at most 6 short bullet points in English: what the user asked for, what Umay actually did, "
                  "facts and decisions that still matter, anything left open. Only what the conversation says: "
                  "if something wasn't done, don't say it was. Only the bullet points.\n\n")
SUMMARY_SOURCE_CHARS = 24000  # ~7,000 tokens of the conversation, its newest part; the request has no tools
SUMMARY_TOOL_CHARS = 300


def _turn_starts(messages):
    """Where the user's turns start: their own messages (not a screenshot, not GO_ON). A cut there leaves no
    tool result without its call."""
    return [i for i, m in enumerate(messages)
            if m["role"] == "user" and isinstance(m.get("content"), str) and m["content"] != GO_ON]


def _drop_oldest_turn(messages):
    """Drops (in place) everything before the second turn. False when only the current turn is left."""
    cut = next((i for i in _turn_starts(messages) if i > 0), None)
    if cut is None:
        return False
    del messages[:cut]
    return True


def _transcript(messages):
    lines = []
    for m in messages:
        content = m.get("content")
        if not isinstance(content, str) or content == GO_ON:
            continue  # screenshots
        if m["role"] == "tool":
            lines.append(f"(tool result: {content[:SUMMARY_TOOL_CHARS]})")
        elif m["role"] == "user":
            lines.append(content if content.startswith(SUMMARY_PREFIX) else f"User: {content}")
        elif content or m.get("tool_calls"):
            called = ", ".join(tc["function"]["name"] for tc in m.get("tool_calls") or [])
            lines.append(f"Umay: {content}" + (f" (called {called})" if called else ""))
    return "\n".join(lines)[-SUMMARY_SOURCE_CHARS:]


def _summarize(mode, messages):
    response = _post(mode, {"model": mode["model"],
                            "messages": [{"role": "user", "content": SUMMARY_PROMPT + _transcript(messages)}],
                            **_request_extras(mode)}, timeout=90)
    response.raise_for_status()
    data = response.json()
    credit_log.record(mode["model"], data.get("usage"))
    return (data["choices"][0]["message"].get("content") or "").strip()


def ask_plain(prompt, timeout=90):
    """One question to Umay's model, no tools and no history (web_find picks its next step with it)."""
    mode = MODES["umay"]
    response = _post(mode, {"model": mode["model"], "messages": [{"role": "user", "content": prompt}],
                            **_request_extras(mode)}, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    credit_log.record(mode["model"], data.get("usage"))
    return (data["choices"][0]["message"].get("content") or "").strip()


web_find.ask_model = ask_plain
heavy_model.scrub = lambda prompt: memory_cortex.redact(prompt, getattr(_memory, "owner_said", ""))


def _compact(mode, messages):
    """Replaces (in place) everything before the current turn with a short summary of it, an earlier summary
    included. False when there is nothing left to summarize or the summary fails."""
    starts = _turn_starts(messages)
    cut = starts[-1] if starts else 0
    old = messages[:cut]
    if not old or (len(old) == 1 and str(old[0].get("content")).startswith(SUMMARY_PREFIX)):
        return False
    try:
        summary = _summarize(mode, old)
    except Exception:
        return False
    if not summary:
        return False
    messages[:cut] = [{"role": "user", "content": SUMMARY_PREFIX + summary}]
    return True


def _shrink_turn(messages):
    """The current turn alone is too long: one step smaller, in place. False when nothing is left to take. Older
    tool answers are cut first, then the newest, and the screenshot last: the 4b needs it to click (2026-11-08: a
    screenshot and two tab lists came to 16,762 tokens and the owner got the raw error)."""
    starts = _turn_starts(messages)
    answers = [m for m in messages[starts[-1] if starts else 0:] if m["role"] == "tool"]
    for group in (answers[:-1], answers[-1:]):
        long = [m for m in group if len(str(m.get("content"))) > SHRINK_TO * 2]
        for m in long:
            text = str(m["content"])
            m["content"] = f"{text[:SHRINK_TO]}\n[cut by Umay to fit, {len(text) - SHRINK_TO} more characters]"
        if long:
            return True
    if any(isinstance(m.get("content"), list) for m in messages):
        _drop_images(messages)
        return True
    return False


def _request_fitting(mode, messages, tools=None):
    """The next assistant turn. When the history doesn't fit the model's context, what came before the current
    turn becomes a short summary; if that still doesn't fit (or can't be made), the oldest turns go, and then the
    current turn is shrunk (_shrink_turn).
    The local model doesn't always say so: past 16k Ollama just cut the reply ("You're right, dear. There's a
    distinction Umay needs to clarify: the", Telegram 2026-11-07, 16,367 of 16,384 tokens). So once a request
    comes within CONTEXT_ROOM of the window, the history is summarized right away, and a reply cut by it
    (finish_reason "length") is asked for again."""
    again = True
    while True:
        _fill.used, _fill.cut = 0, False
        try:
            result = _call_with_retry(lambda: _openai_compat_request(mode, messages, tools))
        except requests.exceptions.HTTPError as e:
            if not _too_long(e) or not (_compact(mode, messages) or _drop_oldest_turn(messages)
                                        or _shrink_turn(messages)):
                raise
            continue
        if mode["local"] and _fill.used > local_llm.context() - CONTEXT_ROOM:
            smaller = _compact(mode, messages) or (_fill.cut and _shrink_turn(messages))
            if smaller and _fill.cut and again:
                again = False
                continue
        return result


def _drop_images(messages):
    """Replaces screenshots already answered from with a placeholder, so they aren't
    re-sent (and paid for) with every later message."""
    for m in messages:
        if isinstance(m.get("content"), list):
            m["content"] = "[screenshot from look_at_screen, no longer attached]"


def _turn(chat, user_text):
    """What one message's requests get besides the conversation: (note, tools). `note` goes after the owner's text
    in every request of the turn (_with_reminder): the memories the message is about (memory_cortex.context_for:
    the owner's name and only what it names, no embedding, a few ms; never to a cloud model), today's date, which
    goes last too (with it only at the top of the prompt the 4b said "Take care in Paris" ten days before the trip,
    2026-11-07), and MEMORY_RULE where the message is about that. `tools`: None for all of them, [] for a message
    that only thanks or says goodbye (small_talk; not while a plan waits for an answer)."""
    known = memory_cortex.context_for(user_text) if MODES[chat["mode"]]["local"] else ""
    said = memory_cortex.HINT.sub("", user_text or "").replace("’", "'")
    about_memory = MEMORY_ASKED.search(said) or OWNER_TELLS.search(said)
    timed = TIME_ASKED.search(said) or memory_cortex.DAY_DATE.search(known)
    note = known + (f"\n\n(Today is {datetime.now():%A, %d %B %Y, %H:%M}.)" if timed else "") \
        + (MEMORY_RULE if about_memory else "")
    return note, ([] if small_talk(user_text) and not chat.get("pending_plan") else None)


def _mode_for(chat, tools):
    """The mode a turn's requests use: a message with no tools to choose from (small_talk) gets her persona
    without TOOL_HINTS, which are about tools. With them, and no tool list after them, the 4b answered a goodbye
    with their memory sentence: "it will be saved when we say goodnight" (real 4b, 2026-11-10)."""
    mode = MODES[chat["mode"]]
    return {**mode, "system": f"{_load_personality()}\n\n{SMALL_HINTS}"} if tools == [] else mode


def _send_message(chat, user_text, source="other"):
    """Sends a message, resolving any tool calls, and returns the final answer text.
    chat["messages"] is mutated in place so the conversation history keeps growing.
    A tool may return {"text": ..., "image": data_uri} or {"text": ..., "images": [data_uri, ...]};
    the images are shown to the model in a message right after the tool results."""
    messages = chat["messages"]
    messages.append({"role": "user", "content": user_text})
    pc_tools.new_request()  # strict mode (plan 20b) starts over: nothing untrusted read yet
    browser_tools.new_request(user_text)
    window_tools.new_request()
    look = eyes.command(user_text, max_words=6)
    if look:  # "eyes on / off": no model needed, and the small one might not call it right
        answer = eyes.switch(look)
        messages.append({"role": "assistant", "content": answer})
        return answer
    if calls.asked(user_text):  # "call me", "beni ara": the same
        answer = calls.call_owner("You asked me to call. I'm here.")
        messages.append({"role": "assistant", "content": answer})
        return answer
    task = claude_terminal.asked(user_text)
    if task is not None:  # "open Claude Code and ...": the same
        answer = claude_terminal.open_session(task)
        messages.append({"role": "assistant", "content": answer})
        return answer
    monitor = input_tools.asked(user_text)
    if monitor is not None:  # "move the mouse to the other monitor": the same
        input_tools.new_turn()
        _, answer = input_tools.move_to_monitor(monitor)
        answer = answer.rstrip(".") + ", dear."
        messages.append({"role": "assistant", "content": answer})
        return answer
    answer = window_tools.shortcut(user_text)
    if answer is not None:  # "move Opera GX to the other monitor", "run Documents\x\setup.exe": the same
        credit_log.note_tool("window_control")
        messages.append({"role": "assistant", "content": answer})
        return answer
    alarm = price_alerts.asked(user_text)
    answer = price_alerts.shortcut(alarm) if alarm else None
    if answer is not None:  # "BTC 80 bini geçerse haber ver": the 4b model mixed up "80 bin" and the direction
        credit_log.note_tool("price_alert")
        messages.append({"role": "assistant", "content": answer})
        return answer
    # "add Apple to the markets", then "follow Nvidia in the news" (whose "unfollow X" needs no "news"): the same
    market, follow = markets_follow.asked(user_text), None
    if market is None:
        follow = news_follow.asked(user_text)
    if market is not None or follow is not None:
        answer = markets_follow.run(*market) if market is not None else news_follow.run(*follow)
        messages.append({"role": "assistant", "content": answer})
        return answer
    if memory_editor.asked(user_text):  # "open the memory editor": a window on the PC, no model needed
        memory_editor.open_editor()
        answer = "The memory editor is open on the PC, dear."
        messages.append({"role": "assistant", "content": answer})
        return answer
    answer = memory_cortex.date_answer(user_text)
    if answer is not None:  # "When do I go to Paris?": the 4b said "tomorrow" beside the right date
        credit_log.note_tool("memory_date")
        messages.append({"role": "assistant", "content": answer})
        return answer
    music = spotify_tools.asked(user_text)
    if music is not None:  # "Spotify'da Tarkan çal", "turn Spotify down to 40": the 4b model got these wrong
        credit_log.note_tool("spotify")
        answer = spotify_tools.spoken(spotify_tools.spotify(**music))
        messages.append({"role": "assistant", "content": answer})
        return answer
    # Shadow only (router.py): what layer 1 would have done is logged next to what the model does; nothing runs.
    route = router.route(user_text)
    note = window_tools.open_app_note(user_text)
    if note:  # "in the Umay Test App ...": the 4b made up a website for it (2026-11-06)
        user_text += note
        messages[-1]["content"] = user_text
    called, started, answer = [], time.monotonic(), ""
    _memory.owner_said = user_text  # what may stay in anything that leaves the PC
    _memory.note, tools = _turn(chat, user_text)
    on_screen = tools is None and input_tools.points_at_screen(user_text) and not chat.get("pending_plan")
    if on_screen:
        _memory.note += SCREEN_RULE
        tools = [t for t in TOOLS if t["function"]["name"] in SCREEN_TOOLS]
    input_tools.new_turn()
    heavy_model.new_request()  # its call budget (plan 21) starts over
    run = {"cost": 0.0}
    before = hud.current()  # the box, a spoken turn or nothing (Telegram): back to it at the end
    try:
        planned = None if tools is not None else _planned(chat, user_text, source, called, run)
        if planned is not None:  # a multi-step request, done as a plan (task_plan)
            answer = planned
            messages.append({"role": "assistant", "content": answer})
            return answer
        content = _tool_loop(_mode_for(chat, tools), messages, MAX_TOOL_ROUNDS, called, run, tools)
        if content is None:
            return i18n.get(_language, "too_many_tool_calls")
        answer = content
        return content
    finally:
        _memory.note = ""
        if hud.current() in ("research", "work", "think") and before not in ("research", "work"):
            hud.activity(before)
        _drop_images(messages)
        if run["cost"]:
            _message_costs.append(run["cost"])
        router.log(route, source, called, time.monotonic() - started, answer)
        # Shadow only too (tool_pick.py): what the NPU's decision model would have called first. For the messages
        # the router logs (so UMAY_ROUTER=off, and a test that mocks the router, stop both) that had every tool.
        if route is not None and tools is None and not MODES[chat["mode"]].get("picks"):  # not when it chooses for real
            tool_pick.shadow(router.owner_text(user_text), source, called, TOOLS)


def _tool_loop(mode, messages, rounds, called, run, tools=None):
    """Model turns and their tool calls until the model answers without one; returns that answer, or None when
    `rounds` ran out. `tools`: the tool schemas it may use (all by default); a call to any other is refused.
    Appends to `messages` and `called` in place and adds the cost to run["cost"]."""
    if mode.get("picks"):
        return _picked_loop(mode, messages, rounds, called, run, tools)
    allowed = None if tools is None else {t["function"]["name"] for t in tools}
    on_screen, nudges, compacted = False, 0, False
    for _ in range(rounds):
        content, tool_calls, round_cost = _request_fitting(mode, messages, tools)
        run["cost"] += round_cost

        if not tool_calls:
            # A full context gives an empty answer, not an error (16,352 of 16,384 tokens, twice on 2026-11-06).
            if not (content or "").strip() and not compacted and _compact(mode, messages):
                compacted = True
                continue
            messages.append({"role": "assistant", "content": content})
            if on_screen and nudges < NUDGES and ANNOUNCES_STEP.search(content[-200:]):
                nudges += 1
                messages.append({"role": "user", "content": GO_ON})
                continue
            return content

        messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})
        on_screen = on_screen or bool({tc["function"]["name"] for tc in tool_calls} & WORKING_TOOLS)
        images, _ = _run_calls(messages, tool_calls, called, allowed)
        if images:
            _show_images(messages, images)
    return None


def _run_calls(messages, tool_calls, called, allowed=None):
    """Runs a model turn's tool calls and appends each answer to `messages` (and the call to `called`). Returns
    (the images the tools returned, the answers' texts). A call to a tool outside `allowed` is refused."""
    images, texts = [], []
    names = {tc["function"]["name"] for tc in tool_calls}
    hud.activity("research" if names & RESEARCH_TOOLS else "work")
    for tc in tool_calls:
        fn_info = tc["function"]
        credit_log.note_tool(fn_info["name"])
        func = TOOL_FUNCTIONS.get(fn_info["name"])
        try:
            args = json.loads(fn_info.get("arguments") or "{}")
            called.append((fn_info["name"], args))
            leak = fn_info["name"] in OUTWARD_SEARCHES and memory_cortex.private_in(
                json.dumps(args, ensure_ascii=False), getattr(_memory, "owner_said", ""))
            if allowed is not None and fn_info["name"] not in allowed:
                result = (f"{fn_info['name']} isn't one of this step's tools; use these: "
                          f"{', '.join(sorted(allowed))}.")
            elif leak:  # 2026-11-08: told "there is no such channel", it searched for the owner's own name
                result = "Not searched: that is the owner's private information, and it doesn't go to the web."
            else:
                result = func(**args) if func else i18n.get(_language, "unknown_tool", name=fn_info["name"])
        except Exception as e:
            result = i18n.get(_language, "tool_error", error=e)
        if fn_info["name"] in UNTRUSTED_TOOLS:
            pc_tools.mark_untrusted()
        memory_cortex.note_tool(fn_info["name"], result)  # a folder seen: its path goes on its neuron
        if isinstance(result, dict) and ("image" in result or "images" in result):
            images += result.get("images") or [result["image"]]
            result = result["text"]
        texts.append(str(result))
        messages.append({"role": "tool", "tool_call_id": tc["id"], "content": str(result)})
    hud.activity("think")
    return images, texts


def _show_images(messages, images):
    """The tools' screenshots as one user message, the only one kept: a task with many clicks fills the context."""
    _drop_images(messages)
    messages.append({
        "role": "user",
        "content": [{"type": "text", "text": "[Screenshot taken by look_at_screen]"}]
        + [{"type": "image_url", "image_url": {"url": url}} for url in images],
    })


# --- A picking mode's turn: the NPU's decision model names the tool, the model writes its arguments ------

ARGS_SYSTEM = ("You fill in the arguments of one tool call for Umay, a desktop assistant on the owner's Windows PC. "
               "The tool is already chosen. Answer with one JSON object: the arguments, taken from the owner's "
               "message, the conversation and the results so far. Leave out an optional argument nothing speaks "
               "about. Never make up a path, a name, an address or a time: use what is written, as it is written.")
ARGS_TOKENS = 700        # write_file's text and a mail's body are arguments too
ARGS_RESULT_CHARS = 1500  # of each earlier answer of the turn: the tab or the file the next call names is in it
BEFORE_CHARS = 900
# Nothing chose a tool (the NPU's server is down): the model must not act as if one had run.
TOOLS_DOWN = ("\n(Your tools can't be reached right now. If this message asks you to do or look up something, say "
              "you can't right now and that they should try again in a moment. Otherwise just answer.)")
SEE_PROMPT = ("Read the image file {image}. It is a screenshot of the owner's PC. An assistant that can't see it has "
              "to work from your words: say which app and window is in front, the text on it that matters, and the "
              "buttons, fields, tabs and links by their labels, in at most 150 words of plain text. What the "
              "assistant is doing: ")
NOT_SEEN = "(The screenshot couldn't be put into words just now: you can't see the screen.)"


def _before(messages, chars=BEFORE_CHARS):
    """The last lines of the conversation in `messages`, in short ("Owner: ...", "Umay: ...")."""
    lines = []
    for m in messages[-6:]:
        content = m.get("content")
        if isinstance(content, str) and content and content != GO_ON and m["role"] in ("user", "assistant") \
                and not content.startswith(SUMMARY_PREFIX):
            lines.append(f"{'Owner' if m['role'] == 'user' else 'Umay'}: {' '.join(content.split())[:400]}")
    return "\n".join(lines)[-chars:]


def _arguments(mode, tool, request, before, done, run):
    """The arguments for `tool`, written by the model as JSON under the tool's own schema (the server holds it
    to it: 40 of 40 fitted for every model tried, 2026-11-11). `done`: this turn's calls so far. A tool that
    takes nothing costs no request. Raises ValueError when the answer isn't a JSON object."""
    fn = tool["function"]
    schema = fn.get("parameters") or {}
    properties = schema.get("properties") or {}
    if not properties:
        return {}
    now = datetime.now()
    parts = [f"Now: {now:%Y-%m-%d %H:%M} ({now:%A})."]
    if before:
        parts.append(f"The conversation before:\n{before}")
    parts.append(f"The owner's message: {request}{getattr(_memory, 'note', '')}")
    if done:
        parts.append("Already done for it:\n" + "\n".join(
            f"{n}. {name}({json.dumps(args, ensure_ascii=False)[:300]}) answered: {str(result)[:ARGS_RESULT_CHARS]}"
            for n, (name, args, result) in enumerate(done, 1)))
    parts.append(f"The tool to call now: {fn['name']}\nWhat it does: {fn.get('description', '')}\n"
                 f"Its parameters: {json.dumps(schema, ensure_ascii=False)}")
    body = {"model": mode["model"], "temperature": 0, "max_tokens": ARGS_TOKENS,
            "messages": [{"role": "system", "content": ARGS_SYSTEM}, {"role": "user", "content": "\n\n".join(parts)}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "arguments", "schema": schema}},
            **_request_extras(mode)}
    response = _call_with_retry(lambda: _post(mode, body, timeout=60))
    response.raise_for_status()
    data = response.json()
    credit_log.record(mode["model"], data.get("usage"))
    run["cost"] += float((data.get("usage") or {}).get("cost") or 0)
    text = data["choices"][0]["message"].get("content") or ""
    try:
        args = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except ValueError:
        raise ValueError(f"no arguments in the answer: {text[:120]!r}") from None
    if not isinstance(args, dict):
        raise ValueError(f"the arguments aren't an object: {text[:120]!r}")
    return {k: v for k, v in args.items() if k in properties}


def _seen(images, request):
    """A screenshot in words, for a model that takes no images (mode["sees"] is False): the heavy model reads
    it. That is a cloud model: the request goes through heavy_model.scrub, the picture as it is (the owner's
    rule for screenshots, 2026-11-08). NOT_SEEN when there is no heavy model or it fails."""
    try:
        head, _, data = str(images[-1]).partition(",")
        picture = base64.b64decode(data) if head.startswith("data:") else None
    except (ValueError, IndexError):
        picture = None
    said = heavy_model.ask(SEE_PROMPT + request[:300], "see", image=picture) if picture else None
    return f"What the screenshot shows: {said.strip()}" if isinstance(said, str) and said.strip() else NOT_SEEN


def _say(mode, messages, run, extra=""):
    """The model's answer to the conversation as it stands, with no tool to call; appended to `messages`.
    `extra` goes after the owner's message for this request only."""
    kept = getattr(_memory, "note", "")
    _memory.note = kept + extra
    try:
        content, _, cost = _request_fitting(mode, messages, [])
        run["cost"] += cost
        if not (content or "").strip() and _compact(mode, messages):  # a full context gives an empty answer
            content, _, cost = _request_fitting(mode, messages, [])
            run["cost"] += cost
    finally:
        _memory.note = kept
    # A result note of its own making after the answer ('... on the desktop, dear.   [Result of search_file] No file
    # named YouTube links found.', 2026-11-11): the model copies the notes' form. Only what stands before it is kept.
    content = ASIDE.sub("", (content or "").split(RESULT_OPEN.split("{")[0])[0].rstrip() or content or "")
    messages.append({"role": "assistant", "content": content})
    return content


def _picked_loop(mode, messages, rounds, called, run, tools=None):
    """_tool_loop for a picking mode (UMAY_TOOL_CHOICE=npu). Each round the NPU's decision model names the next
    tool from the owner's message and what the tools answered so far (tool_pick.choose), the model writes that
    tool's arguments (_arguments) and the tool runs; when the choice is none, the model answers from the
    conversation (_say). Measured 2026-11-10: d3:mini named the right first tool for 96% of the commands, the 4b
    for 86%. The same call twice in a turn ends it, and so does anything that fails on the way: the model then
    answers with what is there. Every choice is a line in tool_picks.jsonl."""
    offered = TOOLS if tools is None else tools
    by_name = {t["function"]["name"]: t for t in offered}
    starts = _turn_starts(messages)
    start = starts[-1] if starts else 0
    request = messages[start]["content"] if starts else ""
    said = router.owner_text(request) or request
    before = _before(messages[:start])
    done = []
    for n in range(1, rounds + 1):
        name, likely, args, error, repeat = None, None, None, "", False
        began = time.monotonic()
        if offered:
            try:
                name, likely, _ = tool_pick.choose(said, offered, done, before)
            except Exception as e:  # the NPU's server is down or answered nonsense: no tool this round
                error = f"{type(e).__name__}: {e}"[:160]
        chose = time.monotonic()
        if name:
            try:
                args = _arguments(mode, by_name[name], request, before, done, run)
                repeat = any(d[0] == name and d[1] == args for d in done)
            except Exception as e:
                error = f"arguments: {type(e).__name__}: {e}"[:160]
        tool_pick.note_pick(text=said[:300], round=n, tool=name, p=None if likely is None else round(float(likely), 3),
                            seconds=round(chose - began, 2), args_seconds=round(time.monotonic() - chose, 2),
                            **({"error": error} if error else {}), **({"repeat": True} if repeat else {}))
        if args is None or repeat:
            return _say(mode, messages, run, TOOLS_DOWN if error and not done else "")
        call = {"id": f"call_{uuid.uuid4().hex[:12]}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}
        messages.append({"role": "assistant", "content": "", "tool_calls": [call]})
        images, texts = _run_calls(messages, [call], called)
        result = texts[0]
        if images and mode.get("sees", True) and not _on_npu(mode):
            _show_images(messages, images)
        elif images:
            result += "\n" + _seen(images, said)
            messages[-1]["content"] = result
        done.append((name, args, result))
    return None


# --- Plans for multi-step requests (plan 21, task_plan) -----------------------------------------

RECENT_CHARS = 1500


def _recent_text(chat):
    """The conversation just before the current message, in short, for the planner (a follow-up like "now send
    it to Ali" needs it)."""
    lines = []
    for m in chat["messages"][:-1][-6:]:
        content = m.get("content")
        if isinstance(content, str) and content and content != GO_ON and m["role"] in ("user", "assistant"):
            lines.append(f"{'Owner' if m['role'] == 'user' else 'Umay'}: {content}")
    return "\n".join(lines)[-RECENT_CHARS:]


def _show(source, text):
    """A line for the owner while the request still runs: in the text box (box) or on Telegram; a spoken
    request just goes on (its answer is spoken at the end)."""
    if source == "box" and chat_box.is_open():
        chat_box.add(AI_NAME, text, "her")
    elif source == "telegram":
        pc_tools.current_notifier()("plan_note", text=text)


def _what_was_done(messages):
    """The tools a step called and the start of what they answered, for the heavy model's fix and check. That
    model is in the cloud, so what search_memory found stays here (the owner's rule, 2026-11-08)."""
    lines, private = [], set()
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            lines += [f"called {tc['function']['name']}({tc['function'].get('arguments') or ''})"[:300]
                      for tc in m["tool_calls"]]
            private |= {tc["id"] for tc in m["tool_calls"] if tc["function"]["name"] == "search_memory"}
        elif m["role"] == "tool":
            shown = "(found in the memory, not shown)" if m.get("tool_call_id") in private else str(m.get("content"))
            lines.append(f"  -> {shown[:RESULT_SHOWN]}")
    return "\n".join(lines[-16:])


def _needs_plan(chat, user_text):
    """task_plan.needs_plan. In a picking mode the NPU's decision model answers it, from the owner's own words:
    of 65 requests the 4b had sent to the planner it called 18 a plan, and the others read as one step ("Okay."
    with the spoken hint after it, "Could you open a QDA project?"); 1 of 45 single-tool commands (2026-11-11,
    development/model_tests/run_plan_gate.py)."""
    if MODES[chat["mode"]].get("picks") and npu.ready():
        return task_plan.needs_plan(router.owner_text(user_text), ask_plain, decide=npu.decide)
    return task_plan.needs_plan(user_text, ask_plain)


def _planned(chat, user_text, source, called, run):
    """A multi-step request becomes a plan the heavy model writes and the local model does step by step (plan
    21; the owner's choice 2026-11-05: show the plan and start at once). Returns the answer, or None when
    the request isn't multi-step or there's no plan (no heavy model, offline): then the usual tool loop
    answers. When the planner has questions, they are the answer, and the next message is taken as their
    answer (chat["pending_plan"])."""
    pending = chat.pop("pending_plan", None)
    if not heavy_model.on():
        return None
    if pending and not task_plan.NOT_A_REPLY.match(user_text.strip()):
        request = f"{pending}\n{task_plan.ANSWER_MARK} {user_text})"
    elif _needs_plan(chat, user_text):
        request = user_text
    else:
        return None
    hud.activity("think")
    plan = task_plan.make(request, _recent_text(chat), TOOLS, f"{datetime.now():%Y-%m-%d %A %H:%M}")
    if plan is None:
        task_plan.note("no plan", request=request)
        return None
    task_plan.note("plan", request=request, source=source, steps=plan["steps"], ask_user=plan["ask_user"])
    if plan["ask_user"]:
        if task_plan.waiting_on_reply(request):
            chat["pending_plan"] = request
        return i18n.get(_language, "plan_questions", questions="\n".join(f"- {q}" for q in plan["ask_user"]))
    _show(source, i18n.get(_language, "plan_shown", count=len(plan["steps"]),
                           steps=task_plan.plan_text(plan["steps"])))
    return _run_plan(request, plan["steps"], called, run)


def _checked(request, step, answer, messages, calls):
    """Did this step really happen? (True / False / None for "can't tell", why). Only a step that changed
    something is checked, and the screen goes along only when the step worked on the screen."""
    if not task_plan.needs_check(calls):
        return True, ""
    image = None
    if {name for name, _ in calls} & SCREEN_TOOLS:
        time.sleep(CHECK_WAIT_S)  # what a key or a click started has to reach the screen
        try:
            image = screen_tools.desktop_jpeg()
        except OSError:
            image = None
    return task_plan.verify(request, step, answer, _what_was_done(messages), image)


def _run_plan(request, steps, called, run):
    """Each step in a fresh conversation with only its tools (task_plan.step_tools). A step that changed something
    is then checked (_checked): the 4b's "done" isn't enough. A step that fails or doesn't pass gets one new attempt
    written by the heavy model; if that fails too, or it gives up, the plan stops there."""
    mode = MODES["umay"]
    names = [t["function"]["name"] for t in TOOLS]
    results, retried, answer, unchecked = [], set(), "", 0
    i = 0
    while i < len(steps):
        allowed = task_plan.step_tools(steps[i], names, last=i == len(steps) - 1)
        tools = [t for t in TOOLS if t["function"]["name"] in allowed]
        messages = [{"role": "user", "content": task_plan.step_prompt(request, steps, i, results)}]
        before = len(called)
        answer = _tool_loop(mode, messages, STEP_ROUNDS, called, run, tools)
        if not task_plan.failed(answer) and task_plan.skipped(steps[i], called[before:]):
            messages.append({"role": "user", "content": task_plan.not_done(steps[i])})
            answer = _tool_loop(mode, messages, STEP_ROUNDS, called, run, tools)
            if task_plan.skipped(steps[i], called[before:]):
                answer = ""  # still nothing done: a failed step
        verdict, why = None, ""
        problem = (answer or "") if task_plan.failed(answer) else None  # no answer at all is a failed step too
        if problem is None:
            verdict, why = _checked(request, steps[i], answer, messages, called[before:])
            if verdict is False:
                problem = why or i18n.get(_language, "plan_no_answer")
        task_plan.note("step", number=i + 1, do=steps[i]["do"], tools=[name for name, _ in called[before:]],
                       answer=answer or "", verdict=verdict, why=why, failed=problem is not None)
        if problem is not None:
            new, gave_up = (None, None) if i in retried else task_plan.fix(
                request, steps, i, results, f"{problem}\n{_what_was_done(messages)}".strip(), TOOLS)
            if new is None:
                task_plan.note("stopped", number=i + 1, why=gave_up or problem)
                return i18n.get(_language, "plan_stopped", step=i + 1, count=len(steps),
                                why=gave_up or problem or i18n.get(_language, "plan_no_answer"),
                                done=task_plan.results_text(results))
            retried.add(i)
            steps = steps[:i] + [new] + steps[i + 1:]
            continue
        unchecked += verdict is None
        results.append(task_plan.short(answer) + (" (not checked)" if verdict is None else ""))
        i += 1
    return f"{answer}\n{i18n.get(_language, 'plan_unchecked', count=unchecked)}" if unchecked else answer


# --- OpenRouter credits ---------------------------------------------------------------

_message_costs = deque(maxlen=50)  # cost of each recent user message, all tool rounds included


def openrouter_balance(timeout=10):
    """Remaining OpenRouter credit in USD, or None if it can't be read."""
    try:
        resp = requests.get(
            "https://openrouter.ai/api/v1/credits",
            headers={"Authorization": f"Bearer {os.environ.get(OPENROUTER_KEY_ENV, '').strip()}"},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        return float(data["total_credits"]) - float(data["total_usage"])
    except Exception:
        return None


def credits_text(lang):
    remaining = openrouter_balance()
    if remaining is None:
        return i18n.get(lang, "openrouter_credits_failed")
    text = i18n.get(lang, "openrouter_credits", remaining=remaining)
    if _message_costs:
        average = sum(_message_costs) / len(_message_costs)
        if average > 0:
            text += "\n" + i18n.get(lang, "openrouter_estimate", messages=int(remaining / average),
                                    average=average, count=len(_message_costs))
    return text


def get_openrouter_credits() -> str:
    return credits_text("en")


if not MODES["umay"]["local"]:  # the local model's replies cost nothing
    TOOLS.append({
        "type": "function",
        "function": {
            "name": "get_openrouter_credits",
            "description": "Shows the user's remaining OpenRouter credit (what Umay's replies cost) and roughly "
                           "how many more messages it covers at the recent average cost.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    })
    TOOL_FUNCTIONS["get_openrouter_credits"] = get_openrouter_credits


def tavily_search(query, max_results=3):
    """Raw Tavily results [{title, url, content}] for background work (memory_cortex's SELF research)."""
    return (_tavily_client.search(query, max_results=max_results) or {}).get("results", [])


def _get_tavily_credits():
    """Returns (kind, data) where kind is 'ok' | 'unknown' | 'error', for re-rendering on language toggle."""
    try:
        resp = requests.get(
            "https://api.tavily.com/usage",
            headers={"Authorization": f"Bearer {os.environ.get('TAVILY_API_KEY')}"},
            timeout=5,
        )
        resp.raise_for_status()
        account = resp.json().get("account", {})
        usage, limit = account.get("plan_usage"), account.get("plan_limit")
        if usage is None or limit is None:
            return ("unknown", None)
        return ("ok", {"remaining": limit - usage, "limit": limit})
    except Exception:
        return ("error", None)


def _tavily_remaining():
    kind, data = _get_tavily_credits()
    return data["remaining"] if kind == "ok" else None


# Before / after balances for the credit log; "before" holds the request up, so it gets a short timeout.
# The local model costs nothing: no balance to read around its replies.
credit_log.configure(balance=None if MODES["umay"]["local"] else lambda: openrouter_balance(timeout=4),
                     tavily=_tavily_remaining)


def _new_log_path():
    return CHAT_LOG_DIR / f"{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.txt"


def _log_line(who, text):
    path = _state.get("log_path")
    if path is None:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{who}: {text}\n")


def _box_open():
    with _lock:
        _state["chat"] = new_chat()
        _state["log_path"] = _new_log_path()
    hud.activity("listen")


def _box_close():
    with _lock:
        chat = _state["chat"]
        log_path = _state["log_path"]
        _state["chat"] = None
        _state["log_path"] = None
    memory_cortex.conversation_ended(log_path)
    import voice_session

    if not voice_session.active():  # a spoken conversation keeps her busy
        hud.activity(None)


def _box_send(text):
    """Enter in the text box: Umay thinks (the orb too), then her answer shows in the box while the
    orb talks for about as long as it takes to read. Silent: the owner chose text for the box."""
    chat = _state["chat"]
    if chat is None:
        return
    chat_box.add(USER_NAME, text, "you")
    _log_line(USER_NAME, text)
    chat_box.thinking(True)
    hud.activity("think")

    hint = chat.pop("hint", "")  # reach_out(hint=...): for this answer only

    def work():
        answer = answer_message(chat, text + hint, source="box")
        _log_line(AI_NAME, answer)
        chat_box.thinking(False)
        chat_box.add(AI_NAME, answer, "her")
        hud.activity("talk")
        time.sleep(min(10.0, max(2.0, len(answer) / 18)))
        if chat_box.is_open() and _state["chat"] is chat:
            hud.activity("listen")

    threading.Thread(target=work, daemon=True).start()


def toggle_chat_box():
    """Thread-safe: opens the text box beside the HUD's orb (bottom right without the HUD), or closes it:
    Ctrl+F1, or a click on the orb."""
    chat_box.toggle(_box_open, _box_send, _box_close, i18n.get(_language, "box_hint"),
                    i18n.get(_language, "box_thinking"))


def reach_out(text, hint=""):
    """Umay starts a written conversation herself (companion.py: the room is loud, a meeting): the text box
    opens beside the orb without taking the keyboard, her line in it, and the owner answers there. `hint` goes
    to the model with that answer only."""
    if not chat_box.is_open():
        chat_box.toggle(_box_open, _box_send, _box_close, i18n.get(_language, "box_hint"),
                        i18n.get(_language, "box_thinking"), only_open=True)
        for _ in range(60):  # _box_open runs on the box's thread
            if _state["chat"] is not None:
                break
            time.sleep(0.05)
    chat = _state["chat"]
    if chat is None:
        pc_tools.fallback_notify(text)
        return
    chat["messages"].append({"role": "assistant", "content": text})
    if hint:
        chat["hint"] = hint
    _log_line(AI_NAME, text)
    chat_box.add(AI_NAME, text, "her")


def persona():
    """Her persona (personality.md), for what she says on her own (companion.py)."""
    return _load_personality()


def toggle_hud():
    """Ctrl+F2: hides the HUD, or shows it again (starting it if needed); off the hotkey's thread."""
    threading.Thread(target=hud.toggle, daemon=True).start()


def _toggle_mic():
    """Ctrl+F3: the audio panel (hud_audio.py: stop / start listening, the microphone and the speakers, read again
    at every press), or closes it. Without the HUD it is the old Ctrl+F3: she stops listening or listens again."""
    import voice_session

    if hud.open_audio():
        return
    hud.mic_muted(voice_session.toggle_mute())


def toggle_mic():
    """Ctrl+F3 (_toggle_mic), off the hotkey's thread."""
    threading.Thread(target=_toggle_mic, daemon=True).start()


def open_disk_panel():
    """Thread-safe: the storage panel's detail view, the disk scan (a click on the HUD's "Disk: new findings"
    line)."""
    hud.open_detail("storage")


# --- what the HUD's detail views ask of Umay's process (hud.on) ------------------------------------------

def _hud_balances(_event):
    """The credits panel's OpenRouter and Tavily credit left."""
    hud.tell({"balances": {"openrouter": openrouter_balance(timeout=6), "tavily": list(_get_tavily_credits())}})


def _hud_audio_state(event=None):
    """The audio panel (Ctrl+F3) wants the devices: read again from Windows (a headset plugged in since), then
    what is chosen, what the wake phrase is listened on and whether she is muted. The panel's every open asks."""
    import wake

    if (event or {}).get("scan"):
        wake.reopen(refresh=True)
    try:
        found = audio_devices.listing()
    except Exception as e:  # no sounddevice, PortAudio broken: the panel says so
        found = {"inputs": [], "outputs": [], "default_input": None, "default_output": None,
                 "error": f"{type(e).__name__}: {e}"}
    hud.tell({"audio": {**found, **audio_devices.load(), "muted": voice.muted["on"], "listening": wake.status()}})


def _hud_audio_set(event):
    """A choice in the audio panel: {"input": name or ""} (the microphone: the wake listener opens on it now, a
    recording from its next turn), {"output": name or ""} (her voice, from its next sentence), {"muted": bool}."""
    import voice_session
    import wake

    if "muted" in event:
        hud.mic_muted(voice_session.set_mute(bool(event["muted"])))
    for kind in (audio_devices.INPUT, audio_devices.OUTPUT):
        if kind in event:
            audio_devices.choose(kind, str(event[kind] or "") or None)
    if audio_devices.INPUT in event:
        wake.reopen()
    _hud_audio_state()


def _hud_disk_state(_event=None):
    hud.tell({"disk": {"running": disk_watch.running(), "progress": disk_watch.progress()}})


def _hud_disk_scan(_event):
    disk_watch.scan_now()
    _hud_disk_state()


def _hud_disk_delete(event):
    """Deletes a scanned item the owner confirmed in the storage detail view (it asked, listing what goes
    with it and what breaks), the way its kind is deleted (disk_actions.plan)."""
    import disk_actions
    import disk_tools
    import panel_text

    scan = disk_watch.latest()
    item = scan.by_id().get(event.get("id")) if scan is not None else None
    p = disk_actions.plan(item) if item is not None else None
    if p is None:
        hud.tell({"disk_result": {"id": event.get("id"), "ok": False,
                                  "text": i18n.get(_language, "disk_failed", error="item not found")}})
        return
    result = disk_actions.execute(p, disk_tools.Places.real())
    if result.ok and not result.started:
        disk_watch.after_delete(scan, item)
    text, ok = panel_text.result_text(_language, item, result)
    hud.tell({"disk_result": {"id": item.id, "ok": ok, "text": text}})


def wire_hud():
    """What the HUD's detail views can ask for, and the disk scan's progress pushed to them."""
    hud.on("balances", _hud_balances)
    hud.on("audio_state", _hud_audio_state)
    hud.on("audio_set", _hud_audio_set)
    hud.on("disk_state", _hud_disk_state)
    hud.on("disk_scan", _hud_disk_scan)
    hud.on("disk_delete", _hud_disk_delete)
    disk_watch.listen(_hud_disk_state)


def ask_flash(prompt, what="", source="disk"):
    """One question to Umay's model with no tools and no history; returns
    the answer's text. The disk scanner uses it for folders no rule knows (disk_memory.describe), companion.py to
    rate news and word what she brings up. Never a reply: background work (_post), which lets the model rest
    (2026-11-11: the news rating every 10 minutes kept the talker loaded all day)."""
    mode = MODES["umay"]
    if mode["key_env"] and not os.environ.get(mode["key_env"], "").strip():
        raise RuntimeError(f"{mode['key_env']} is not set")

    def request():
        response = _post(mode, {"model": mode["model"], "messages": [{"role": "user", "content": prompt}],
                                **_request_extras(mode)}, timeout=90, background=True)
        response.raise_for_status()
        return response.json()

    with credit_log.activity(source, what):
        data = _call_with_retry(request)
        credit_log.record(mode["model"], data.get("usage"))
    return data["choices"][0]["message"].get("content") or ""


HOTKEY = "<ctrl>+<f1>"       # the text box
HUD_HOTKEY = "<ctrl>+<f2>"   # hide / show the HUD
MIC_HOTKEY = "<ctrl>+<f3>"   # the audio panel: stop / start listening, microphone, speakers


def current_language():
    return _language


def start_ai_assistant():
    """Starts the global hotkeys: Ctrl+F1 the text box, Ctrl+F2 hides / shows the HUD, Ctrl+F3 the audio panel."""
    if _state["listener"] is not None:
        return  # already running
    listener = keyboard.GlobalHotKeys({HOTKEY: toggle_chat_box, HUD_HOTKEY: toggle_hud, MIC_HOTKEY: toggle_mic})
    listener.daemon = True
    listener.start()
    _state["listener"] = listener


def stop_ai_assistant():
    listener = _state["listener"]
    if listener is not None:
        listener.stop()
        _state["listener"] = None
