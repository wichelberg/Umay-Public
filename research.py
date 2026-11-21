"""Research mode (the owner's idea, 2026-11-19): "research <topic>" (or "araştır ...") makes Umay look into a topic from
several sides, check the claims against each other and hand back a report file (.md) in Documents\\Umay Research, with a
summary and the file on Telegram. Fully local: the planner's gpt-oss (planner_server, CPU, 8k window), the keyless search
(local_search). Nothing goes to a cloud model, and when the planner can't answer the job stops and says so: no Haiku.

How it checks (the model reads, the code decides):
1. The model splits the topic into 3-6 questions with 1-2 search queries each. Every query goes through the private-name
   guard (memory_cortex.private_in) and local_search.unsaid_years (a year nobody wrote is cut).
2. Each question's best new pages are read (local_search.read_page) and the model, one page at a time, writes claims, each
   with a quote it copied from the page.
3. CODE keeps a claim only when its quote is really in that page's text (`quote_found`): a model that makes a claim up
   can't also make its quote appear in the page. The count of dropped ones is in the report.
4. The model groups the claims of one question (which say the same, which contradict); CODE counts how many independent
   sources stand behind each group (`origins`: one per registrable domain, wire copies with near-equal quotes count once) and
   gives the label: contested > verified (2+ independent sources) > single source. The model never labels.
5. The model writes a short summary from the kept claims only; the report body is built by code with every quote and link.

Limits that are stated in every report: pages behind a paywall or built by scripts can't be read; "independent" is a
guess from the address and the wording; a claim whose quote is in the page can still be wrong or misread.

A job lives on the disk (%LOCALAPPDATA%\\Umay\\research\\<id>.json: the plan and the notes per page), so a restart or a game
doesn't lose it: in a game (llama_server.hold) it waits, after a restart `resume` finishes it. One job at a time. Settings:
UMAY_RESEARCH_PAGES (18), UMAY_RESEARCH_MINUTES (45, of work, not counting a game's pause), UMAY_RESEARCH_DIR, UMAY_RESEARCH_HOME.
"""
import hashlib
import json
import os
import re
import threading
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests

import llama_server
import local_search
import memory_cortex
import planner_server

SUBQUESTIONS = 6
QUERIES = 2
PER_QUESTION = 3        # new pages read per question
PAGE_CHARS = 6000       # ~1,500 tokens: the window is 8,192 with the prompt and the answer
MIN_QUOTE = 20          # characters (normalised) of a quote that counts as one
DUPLICATE = 0.5         # shared share of 4-word runs: the same wire story under two names
MAX_AGE_HOURS = 12      # a job older than this is not resumed after a restart
SECOND_LEVEL = {"co", "com", "org", "net", "gov", "edu", "ac"}   # co.uk, com.tr, ...

ASK_EN = (
    re.compile(r"^\W*(?:(?:hey\s+)?umay\W+)?(?:(?:can|could|would|will)\s+you\s+|please\s+)?(?:do\s+(?:a\s+|some\s+)?)?"
               r"(?:(?:deep|detailed|full|proper|fact[- ]?check(?:ed)?)\s+)?research\b"
               r"(?!\s*(?:shows?|says?|is|are|was|has|have|suggests?|found|indicates?|mode\W*$)\b)"
               r"\s*(?:mode)?\s*(?:on|about|into|for|:|-)?\s*(.{8,})", re.I | re.S),
)
ASK_TR = (
    re.compile(r"^\W*(?:derin\s+|detayl[ıi]\s+)?ara[sş]t[ıi]rma\s+yap\w*\s*[:,-]?\s*(.{8,})", re.I | re.S),
    re.compile(r"^\W*(.{8,}?)\s+(?:hakk[ıi]nda|konusunda|ile\s+ilgili)\s+(?:derin\s+|detayl[ıi]\s+)?ara[sş]t[ıi]rma\s+yap\w*", re.I | re.S),
    re.compile(r"^\W*(.{8,}?)\s+(?:konusunu\s+)?(?:derin(?:ce)?\s+|detayl[ıi]\s+)?ara[sş]t[ıi]r\w*\W*$", re.I | re.S),
)

DEICTIC = {"bunu", "şunu", "onu", "bu", "şu", "o", "konuyu", "konusunu", "this", "that", "it", "the", "topic", "thing",
           "subject", "konu", "şey", "sey", "da", "de", "bir", "biraz", "please", "lütfen", "lutfen"}

PLAN_SCHEMA = {"type": "object", "required": ["subquestions"], "properties": {"subquestions": {
    "type": "array", "minItems": 3, "maxItems": SUBQUESTIONS, "items": {
        "type": "object", "required": ["question", "queries"], "properties": {
            "question": {"type": "string"},
            "queries": {"type": "array", "minItems": 1, "maxItems": QUERIES, "items": {"type": "string"}}}}}}}
PLAN_SYSTEM = (
    "You plan a fact-checked research report. Split the topic into 3 to 6 separate questions that together cover it: the "
    "basic facts, the main claims people make, what is disputed, what is recent, the numbers. For each question give 1 or 2 "
    "web search queries in different words: short, plain, no quote marks, and no year unless the topic names one.")
READ_SCHEMA = {"type": "object", "required": ["relevant", "claims"], "properties": {
    "relevant": {"type": "boolean"}, "published": {"type": "string"},
    "claims": {"type": "array", "maxItems": 4, "items": {
        "type": "object", "required": ["claim", "quote"], "properties": {
            "claim": {"type": "string"}, "quote": {"type": "string"}}}}}}
READ_SYSTEM = (
    "You read one web page for a research question and take out the claims that help answer it: only about the question "
    "itself, never other things the page happens to say. A claim is one plain "
    "sentence of fact. Its quote is copied WORD FOR WORD from the page, under 40 words, and must prove the claim. Never add "
    "anything the page does not say. If the page does not help with the question, set relevant to false and give no claims. "
    "published: the page's date if it names one, else an empty string.")
CLASH_SCHEMA = {"type": "object", "required": ["clash"], "properties": {"clash": {"type": "boolean"}}}
CLASH_SYSTEM = (
    "You compare two claims. clash is true only when they cannot both be true at the same time: they give different numbers, "
    "dates, places or outcomes for the same thing, or one denies what the other says. Claims that differ in detail, scope or "
    "wording but can both be true (a city and a building in it, a part and the whole, two aspects) do not clash.")
SUPPORT_SCHEMA = {"type": "object", "required": ["supports"], "properties": {"supports": {"type": "boolean"}}}
SUPPORT_SYSTEM = (
    "You check one quote against one claim. supports is true when the quote states the main point of the claim, in the same "
    "direction and about the same thing, even if it leaves out minor detail. It is false when the quote says something "
    "different (another number, another group of people, the opposite) or is only about the same subject.")
GROUP_SCHEMA = {"type": "object", "required": ["same", "contradict"], "properties": {
    "same": {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}},
    "contradict": {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}}}}
GROUP_SYSTEM = (
    "You compare numbered claims about one question. `same`: lists of numbers whose claims state the same fact. "
    "`contradict`: pairs of numbers whose claims cannot both be true. Use only the numbers given; leave a list out when "
    "there is nothing to say.")
SUMMARY_SCHEMA = {"type": "object", "required": ["summary", "open_questions"], "properties": {
    "summary": {"type": "string"}, "open_questions": {"type": "array", "maxItems": 5, "items": {"type": "string"}}}}
SUMMARY_SYSTEM = (
    "You write the summary of a research report for its owner, 120 to 200 words of plain English, from the findings given "
    "and NOTHING else. Each finding is labelled verified (2 or more independent sources), single source or contested. Write "
    "flowing prose, no list. Lead with what is verified; then, briefly, what rests on a single source (say \"single-source\" "
    "once for the group, not before every sentence) and what is contested (\"flagged as conflicting\"). Copy numbers exactly as the findings give them; never add a fact, a name, a number or a date that is not there. "
    "If the findings do not answer the topic, say so. open_questions: what the findings leave open.")

_lock = threading.Lock()
_job = {"thread": None, "stop": False, "topic": "", "stage": "", "id": ""}


class Unavailable(Exception):
    """The planner can't answer (no files, a game, an error twice): the job stops, no cloud model takes over."""


# --- asking -------------------------------------------------------------------------------------------------------

def asked(text):
    """The topic when `text` asks for research ("research the 2026 Trump Xi Putin meeting", "X hakkında araştırma yap"),
    else None. The spoken hint of a voice message is not part of it."""
    text = " ".join((text or "").split())
    try:
        import voice
        text = text.split(voice.SPOKEN_HINT.strip()[:20])[0].strip()
    except Exception:
        pass
    for pattern in ASK_EN + ASK_TR:
        found = pattern.match(text)
        if found:
            topic = found.group(1).strip(" .:-\"'")
            words = re.findall(r"[^\W\d_]+", topic.lower())
            if len(topic.split()) >= 2 and not set(words) <= DEICTIC:   # "bunu araştır" has no topic of its own
                return topic[:400]
    return None


def home():
    custom = os.environ.get("UMAY_RESEARCH_HOME", "").strip()
    return Path(custom) if custom else Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Umay" / "research"


def out_dir():
    custom = os.environ.get("UMAY_RESEARCH_DIR", "").strip()
    return Path(custom) if custom else Path.home() / "Documents" / "Umay Research"


def _int(name, default, low=1):
    try:
        return max(low, int(os.environ.get(name, "").strip() or default))
    except ValueError:
        return default


def quiet(job):
    """A topic job (source "topic": topic_research studies a topic the owner asked about while they are away): shorter,
    and it tells nobody; what it finds goes into the memory and its report into the "auto" folder."""
    return (job or {}).get("source") == "topic"


def max_pages(job=None):
    return _int("UMAY_TOPIC_PAGES", 10) if quiet(job) else _int("UMAY_RESEARCH_PAGES", 18)


def max_minutes(job=None):
    return _int("UMAY_TOPIC_MINUTES", 30) if quiet(job) else _int("UMAY_RESEARCH_MINUTES", 45)


# --- the checks that are code, not the model ----------------------------------------------------------------------

def norm(text):
    """Letters and digits only, lower case, single spaces: what two copies of a sentence share however they are dressed
    (curly quotes, dashes, spacing)."""
    text = unicodedata.normalize("NFKC", text or "").lower()
    return " ".join(re.sub(r"[^\w]+", " ", text, flags=re.UNICODE).replace("_", " ").split())


def quote_found(quote, page):
    """True when the quote stands in the page's text. "..." inside a quote splits it: every piece of 12+ characters must be
    there. A quote shorter than MIN_QUOTE (normalised) proves nothing."""
    pieces = [norm(p) for p in re.split(r"\.{3}|…", quote or "")]
    pieces = [p for p in pieces if len(p) >= 12] or ([norm(quote)] if len(norm(quote)) >= MIN_QUOTE else [])
    if not pieces:
        return False
    body = norm(page)
    return all(p in body for p in pieces)


def domain(url):
    """The registrable domain of an address (bbc.co.uk, not news.bbc.co.uk), "" when there is none."""
    host = (urlparse(url or "").hostname or "").lower().removeprefix("www.")
    labels = [p for p in host.split(".") if p]
    if len(labels) <= 2:
        return host
    return ".".join(labels[-3:] if labels[-2] in SECOND_LEVEL and len(labels[-1]) == 2 else labels[-2:])


def _shingles(text, n=4):
    words = norm(text).split()
    return {" ".join(words[i:i + n]) for i in range(max(1, len(words) - n + 1))}


def near_duplicate(a, b):
    sa, sb = _shingles(a), _shingles(b)
    return bool(sa and sb) and len(sa & sb) / min(len(sa), len(sb)) >= DUPLICATE


def origins(members):
    """How many independent sources stand behind `members` (each {"url", "quote"}): one per domain, and two members of
    different domains whose quotes are near copies of each other (a wire story) count as one."""
    parent = list(range(len(members)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i in range(len(members)):
        for j in range(i):
            if (domain(members[i]["url"]) == domain(members[j]["url"])
                    or near_duplicate(members[i]["quote"], members[j]["quote"])):
                parent[find(i)] = find(j)
    return len({find(i) for i in range(len(members))})


def cluster(claims, same, contradict):
    """Groups of `claims` (indexes into the list) from the model's `same` lists (union of lists sharing a number; numbers out
    of range are ignored), each as {"members": [...], "contested": bool}. A group that holds either end of a `contradict`
    pair is contested."""
    count = len(claims)
    parent = list(range(count))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for group in same or []:
        valid = [i for i in group if isinstance(i, int) and 0 <= i < count]
        for i in valid[1:]:
            parent[find(i)] = find(valid[0])
    groups = {}
    for i in range(count):
        groups.setdefault(find(i), []).append(i)
    clashed = {i for pair in contradict or [] for i in pair if isinstance(i, int) and 0 <= i < count}
    return [{"members": m, "contested": bool(clashed & set(m))} for m in groups.values()]


STOP = {"that", "this", "with", "from", "have", "been", "were", "which", "their", "they", "them", "than", "when", "also", "such",
        "into", "more", "most", "some", "only", "other", "these", "those", "will", "would", "could", "about", "there", "where",
        "often", "many", "much", "can", "are"}


def content_words(text):
    return {w for w in norm(text).split() if (len(w) >= 4 or w.isdigit()) and w not in STOP}


def related(a, b):
    """Two claims share at least two content words (4+ letters, or a number)."""
    return len(content_words(a) & content_words(b)) >= 2


def split_unrelated(members):
    """The model groups loosely ("Heat is a major hazard" with "thermal runaway after an internal short circuit"). Of a group
    keep the claim that has the most related claims as the anchor with those; every other member stands alone: [[member...], ...]
    (first the anchor's part). A model's grouping can only add sources when the wording really overlaps."""
    if len(members) < 2:
        return [members]
    best = max(range(len(members)), key=lambda i: sum(related(members[i]["claim"], m["claim"]) for m in members))
    anchor = [members[best]] + [m for i, m in enumerate(members) if i != best and related(members[best]["claim"], m["claim"])]
    return [anchor] + [[m] for m in members if m not in anchor]


NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def numbers(text):
    return {n.replace(",", "") for n in NUMBER.findall(text or "")}


def unsupported_numbers(summary, findings):
    """Numbers (years too) of `summary` that no kept claim or quote holds: the model wrote them from somewhere else."""
    held = set()
    for item in findings:
        for g in item["groups"]:
            held |= numbers(g["claim"])
            for m in g["members"]:
                held |= numbers(m["quote"])
    return sorted(n for n in numbers(summary) - held if len(n) > 1 or "." in n)   # a lone digit is a word ("2 sources")


def label(found):
    """contested > verified (2+ independent sources) > single source."""
    if found["contested"]:
        return "contested"
    return "verified" if found["sources"] >= 2 else "single source"


# --- the job ------------------------------------------------------------------------------------------------------

def _wait_free(job):
    """In a game the planner is off (llama_server.hold): wait for the game to end, the work time stops meanwhile."""
    paused = time.monotonic()
    while llama_server.hold["on"] and not _job["stop"]:
        time.sleep(15)
    job["paused"] = job.get("paused", 0.0) + time.monotonic() - paused


def _ask(job, prompt, schema, system, max_tokens):
    """The planner's answer under `schema`; once more after a failure; Unavailable when it still can't."""
    last = None
    for _ in range(2):
        _wait_free(job)
        if _job["stop"]:
            raise Unavailable("stopped")
        try:
            return planner_server.ask_json(prompt, schema, system=system, max_tokens=max_tokens, timeout=300)
        except (requests.RequestException, ValueError, KeyError, IndexError) as e:
            last = e
    raise Unavailable(f"{type(last).__name__}: {last}"[:200])


def _out_of_time(job):
    return time.monotonic() - job["began"] - job.get("paused", 0.0) > max_minutes(job) * 60


def _stage(job, text):
    _job["stage"] = text
    _save(job)


def _file(job):
    return home() / f"{job['id']}.json"


def _save(job):
    try:
        home().mkdir(parents=True, exist_ok=True)
        keep = {k: v for k, v in job.items() if k in ("id", "topic", "created", "plan", "notes", "claims", "done", "report",
                                                      "searched", "stats", "source", "mode", "files")}
        _file(job).write_text(json.dumps(keep, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def make_plan(job):
    today = datetime.now()
    prompt = f"Topic: {job['topic']}\nToday is {today:%Y-%m-%d}."
    plan = _ask(job, prompt, PLAN_SCHEMA, PLAN_SYSTEM, 1200).get("subquestions") or []
    clean = []
    for item in plan[:SUBQUESTIONS]:
        question = str(item.get("question") or "").strip()
        queries = []
        for q in item.get("queries") or []:
            q = local_search.unsaid_years(" ".join(str(q).split()).strip(), job["topic"])
            if q and not memory_cortex.private_in(q, job["topic"]) and q.lower() not in [x.lower() for x in queries]:
                queries.append(q)
        if question and queries:
            clean.append({"question": question, "queries": queries[:QUERIES]})
    if not clean:
        raise Unavailable("the planner wrote no usable questions")
    return clean


def find_pages(job, index, taken):
    """The first PER_QUESTION addresses of question `index`'s queries that no question has read yet. Search results are not
    kept apart from the search: [{url, title}]."""
    found = []
    for query in job["plan"][index]["queries"]:
        for row in local_search.search(query, 6):
            url = row.get("url") or ""
            if url and url not in taken and url not in [f["url"] for f in found] and local_search.public_address(url):
                found.append({"url": url, "title": str(row.get("title") or "")[:120]})
        if len(found) >= PER_QUESTION * 2:
            break
    return found[:PER_QUESTION]


def read_one(job, index, page):
    """Reads one page for question `index`: the kept claims (quote verified in the page), how many were dropped."""
    text = local_search.read_page(page["url"], PAGE_CHARS)
    if not text:
        return {"url": page["url"], "title": page["title"], "read": False, "claims": [], "dropped": 0}
    question = job["plan"][index]["question"]
    prompt = f"Research question: {question}\nPage: {page['title']} ({page['url']})\n\nPage text:\n{text}"
    answer = _ask(job, prompt, READ_SCHEMA, READ_SYSTEM, 1200)
    published = str(answer.get("published") or "").strip()
    years = re.findall(r"(?:19|20)\d\d", published)
    if not years or not any(y in text or y in page["url"] for y in years):
        published = ""        # a date the model wrote from memory ("2023" for a page of 2026) is not shown
    kept, dropped = [], 0
    for item in (answer.get("claims") or []) if answer.get("relevant") else []:
        claim, quote = str(item.get("claim") or "").strip(), str(item.get("quote") or "").strip()
        if claim and quote_found(quote, text):
            kept.append({"claim": claim, "quote": quote, "url": page["url"], "published": published})
        else:
            dropped += 1
    return {"url": page["url"], "title": page["title"], "read": True, "claims": kept, "dropped": dropped}


def supported(job, claim, member):
    """True when the member's quote itself states `claim` (a yes/no of the model; a quote about the same subject is not
    enough). An answer that doesn't come counts as no: a source is only added on a yes."""
    try:
        answer = _ask(job, f"Claim: {claim}\nQuote: {member['quote']}", SUPPORT_SCHEMA, SUPPORT_SYSTEM, 300)
    except Unavailable:
        if _job["stop"]:
            raise
        return False
    return answer.get("supports") is True


def clashes(job, a, b):
    """True when the model, asked about this pair alone, says the two claims can't both be true. The grouping call flagged
    pairs too freely ("in Shenzhen" against "at a villa in southern China", 2026-11-19, twice)."""
    try:
        answer = _ask(job, f"Claim 1: {a}\nClaim 2: {b}", CLASH_SCHEMA, CLASH_SYSTEM, 300)
    except Unavailable:
        if _job["stop"]:
            raise
        return False
    return answer.get("clash") is True


def group_claims(job, index, claims):
    """The question's claims in groups with their labels: [{"claim", "members", "sources", "label"}], best first."""
    if not claims:
        return []
    if len(claims) == 1:
        same, contradict = [], []
    else:
        lines = "\n".join(f"{i}. {c['claim']}" for i, c in enumerate(claims))
        answer = _ask(job, f"Question: {job['plan'][index]['question']}\nClaims:\n{lines}", GROUP_SCHEMA, GROUP_SYSTEM, 900)
        same, contradict = answer.get("same"), answer.get("contradict")
    claims = [dict(c, n=i) for i, c in enumerate(claims)]
    pairs = [tuple(p) for p in contradict or [] if len(p) == 2 and all(isinstance(i, int) and 0 <= i < len(claims) for i in p)]
    pairs = [p for p in pairs[:4] if clashes(job, claims[p[0]]["claim"], claims[p[1]]["claim"])]
    contradict = [list(p) for p in pairs]
    out = []
    for found in cluster(claims, same, contradict):
        for part in split_unrelated([claims[i] for i in found["members"]]):
            rest = []
            if len(part) > 1:     # each source added to the anchor has to say the same thing, not only be about it
                kept = [part[0]] + [m for m in part[1:] if supported(job, part[0]["claim"], m)]
                part, rest = kept, [[m] for m in part if m not in kept]
            for one in [part] + rest:
                sources = origins(one)
                ns = {m["n"] for m in one}
                against = [claims[b if a in ns else a]["claim"] for a, b in pairs if (a in ns) != (b in ns)]
                out.append({"claim": one[0]["claim"], "members": one, "sources": sources, "against": against[:2],
                            "label": label({"contested": found["contested"], "sources": sources})})
    rank = {"contested": 0, "verified": 1, "single source": 2}
    return sorted(out, key=lambda g: (rank[g["label"]], -g["sources"]))


def summarize(job, findings):
    lines = []
    for number, item in enumerate(findings, 1):
        lines.append(f"Question {number}: {item['question']}")
        for g in item["groups"][:6]:
            lines.append(f"- [{g['label']}, {g['sources']} source(s)] {g['claim']}")
        if not item["groups"]:
            lines.append("- nothing found")
    text = "\n".join(lines)[:7000]
    # The length goes last, where the model heeds it: with 120-200 words only in the system prompt a summary came back at ~500
    return _ask(job, f"Topic: {job['topic']}\n\nFindings:\n{text}\n\nWrite the summary now: at most 180 words, flowing prose.",
                SUMMARY_SCHEMA, SUMMARY_SYSTEM, 1200)


def _slug(topic):
    slug = re.sub(r"[^\w]+", "-", unicodedata.normalize("NFKD", topic).encode("ascii", "ignore").decode().lower()).strip("-")
    return (slug[:60].strip("-") or "topic")


def render(job, findings, summary, stats):
    """The report as markdown, built by code from the kept claims."""
    now = datetime.now()
    out = [f"# Research: {job['topic']}", "",
           f"*{now:%Y-%m-%d %H:%M} · {stats['minutes']:.0f} min · {stats['pages_read']} of {stats['pages_tried']} pages read · "
           f"{stats['claims_kept']} claims kept, {stats['claims_dropped']} dropped (quote not found in the page)*", "",
           "## Summary", "", (summary.get("summary") or "(no summary: the planner did not write one)").strip(), ""]
    if summary.get("unsupported"):
        out += ["**Check:** the summary names numbers that no kept claim holds: " + ", ".join(summary["unsupported"]) +
                ". Do not rely on them.", ""]
    questions = summary.get("open_questions") or []
    if questions:
        out += ["**Open:** " + "; ".join(str(q) for q in questions), ""]
    out += ["## Findings", ""]
    for number, item in enumerate(findings, 1):
        out += [f"### {number}. {item['question']}", ""]
        if not item["groups"]:
            out += ["Nothing verifiable was found for this question.", ""]
        for g in item["groups"]:
            head = {"contested": "CONTESTED (flagged by the model)", "verified": f"VERIFIED ({g['sources']} independent sources)",
                    "single source": "SINGLE SOURCE"}[g["label"]]
            out.append(f"- **{head}** {g['claim']}")
            for other in g.get("against") or []:
                out.append(f"  - flagged as conflicting with: {other}")
            for m in g["members"][:4]:
                date = f" ({m['published']})" if m.get("published") else ""
                out.append(f"  - {domain(m['url'])}{date}: \"{m['quote']}\" <{m['url']}>")
        out.append("")
    out += ["## Method and limits", "",
            "Questions and searches were made by a local model (gpt-oss) and the pages read on this PC; nothing was sent to a "
            "cloud model. A claim is kept only when its quote is found word for word in the page's text. \"Verified\" means "
            "two or more independent sources (a source is a web domain; copies of one story count once). Labels come from the "
            "code, the grouping of claims and the summary from the model. Pages behind a paywall or built by scripts could "
            "not be read, and a quote that is in a page can still be wrong: check what matters at the link.", ""]
    return "\n".join(out)


def _run(job):
    began = time.monotonic()
    job["began"] = began
    stats = {"pages_tried": 0, "pages_read": 0, "claims_kept": 0, "claims_dropped": 0}
    try:
        if not job.get("plan"):
            _stage(job, "planning")
            job["plan"] = make_plan(job)
            _save(job)
        notes = job.setdefault("notes", {})
        for index, item in enumerate(job["plan"]):
            if _job["stop"] or _out_of_time(job) or sum(1 for n in notes.values() if n.get("tried")) >= max_pages(job):
                break
            _stage(job, f"question {index + 1} of {len(job['plan'])}: reading")
            if not any(n["index"] == index for n in notes.values()):
                for page in find_pages(job, index, {n["url"] for n in notes.values()}):
                    notes[page["url"]] = {"index": index, "url": page["url"], "title": page["title"], "tried": True}
                _save(job)
            for page in [n for n in notes.values() if n["index"] == index and not n.get("done")]:
                if _job["stop"] or _out_of_time(job):
                    break
                try:
                    page.update(read_one(job, index, page), done=True)
                    job["fails"] = 0
                except Unavailable:
                    job["fails"] = job.get("fails", 0) + 1     # one page that gets no answer is skipped, two in a row end it
                    if _job["stop"] or job["fails"] >= 2:
                        raise
                    page.update(read=False, claims=[], dropped=0, done=True)
                _save(job)
        findings = []
        for index, item in enumerate(job["plan"]):
            claims = [c for n in notes.values() if n["index"] == index for c in n.get("claims", [])]
            _stage(job, f"question {index + 1}: comparing")
            findings.append({"question": item["question"], "groups": group_claims(job, index, claims)})
        stats.update(pages_tried=len(notes), pages_read=sum(1 for n in notes.values() if n.get("read")),
                     claims_kept=sum(len(n.get("claims", [])) for n in notes.values()),
                     claims_dropped=sum(n.get("dropped", 0) for n in notes.values()),
                     minutes=(time.monotonic() - began - job.get("paused", 0.0)) / 60)
        _stage(job, "writing")
        summary = summarize(job, findings) if stats["claims_kept"] else {
            "summary": "Nothing could be verified: no page gave a claim with a quote that stands in its text.",
            "open_questions": []}
        summary["unsupported"] = unsupported_numbers(summary.get("summary") or "", findings)
        report = render(job, findings, summary, stats)
        folder = out_dir() / "auto" if quiet(job) else out_dir()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{datetime.now():%Y-%m-%d_%H-%M}_{_slug(job['topic'])}.md"
        path.write_text(report, encoding="utf-8")
        job.update(done=True, report=str(path), stats=stats)
        _save(job)
        _remember(job, findings, summary)
        _deliver(job, path, summary, stats)
    except Unavailable as e:
        _say(job, f"Research on \"{job['topic'][:80]}\" was stopped." if _job["stop"] else
              f"Research on \"{job['topic'][:80]}\" stopped: {e}. The pages it had read are kept on the disk.")
    except Exception as e:  # a bug must not leave the owner waiting for a report
        _say(job, f"Research on \"{job['topic'][:80]}\" failed: {type(e).__name__}: {e}"[:400])
    finally:
        _job.update(thread=None, stage="", stop=False)
        planner_server.forget_restore()   # our prompts took the slot: the next plan puts its own prefix back


def _remember(job, findings, summary):
    """What the job verified goes into the memory (topic_research.ingest): the owner's own "research X" too. A bug there
    must not lose the report."""
    try:
        import topic_research
        n = topic_research.ingest(job["topic"], findings, summary)
        if quiet(job) and n:
            import companion
            companion.tell(f"While you were away I read up on \"{job['topic'][:60]}\", dear: {n} new notes in my memory.",
                           "notable", "research")
    except Exception as e:
        memory_cortex._log(f"topic research: ingest failed: {type(e).__name__}: {e}")


def _say(job, text):
    """What a job reports about itself: to the owner (_tell), except a topic job, which only logs."""
    if quiet(job):
        memory_cortex._log(f"topic research: {text[:200]}")
    else:
        _tell(text)


def _tell(text):
    """To the owner: Telegram when it is set up, and the HUD's companion at the PC."""
    try:
        import telegram_listener
        telegram_listener.send(text)
    except Exception:
        pass
    try:
        import companion
        companion.tell(text, "important", "research")
    except Exception:
        pass


def _deliver(job, path, summary, stats):
    if quiet(job):
        return
    text =(f"Research done: {job['topic'][:100]}\n\n{(summary.get('summary') or '').strip()}\n\n"
            f"{stats['pages_read']} pages, {stats['claims_kept']} claims kept. Report: {path}")
    _tell(text[:3500])
    try:
        import telegram_listener
        telegram_listener.send_document(path.read_bytes(), path.name, f"Research: {job['topic'][:80]}")
    except Exception:
        pass


def start(topic, source="other"):
    """Starts the job on a thread and answers at once. One at a time."""
    with _lock:
        if _job["thread"] is not None and _job["thread"].is_alive():
            return f"I'm already researching \"{_job['topic'][:60]}\", dear. It's at: {_job['stage'] or 'starting'}."
        if not planner_server.available():
            return "I can't research right now, dear: the local planner's model isn't there."
        if llama_server.hold["on"]:
            note = " A game has the machine, so it waits until the game ends."
        else:
            note = ""
        stamp = datetime.now()
        job = {"id": f"{stamp:%Y%m%d-%H%M%S}-{hashlib.sha1(topic.encode('utf-8', 'replace')).hexdigest()[:6]}",
               "topic": topic, "created": stamp.isoformat(timespec="seconds"), "source": source}
        _launch(job)
    return (f"On it, dear. I'll research \"{topic[:80]}\" and check the claims against each other. It takes 15 to 40 minutes. "
            f"The report goes to Documents\\Umay Research and to Telegram.{note}")


def _launch(job):
    _job.update(topic=job["topic"], id=job["id"], stage="starting", stop=False, source=job.get("source", ""))
    target = _run
    if job.get("mode") == "pdf":      # a PDF study (pdf_study.py) shares this slot, its stop / status and its restart
        import pdf_study
        target = pdf_study.run
    thread = threading.Thread(target=target, args=(job,), name="research", daemon=True)
    _job["thread"] = thread
    _save(job)
    thread.start()


def topic_running():
    """A topic job (quiet, while the owner is away) is running: topic_research stops it when they come back."""
    return _job["thread"] is not None and _job["thread"].is_alive() and _job.get("source") == "topic"


def stop():
    """Ends the running job after its current request (the report is not written)."""
    running = _job["thread"] is not None and _job["thread"].is_alive()
    _job["stop"] = running
    return running


def status():
    if _job["thread"] is not None and _job["thread"].is_alive():
        return f"Researching \"{_job['topic'][:60]}\": {_job['stage'] or 'starting'}."
    return ""


STOP_ASKED = re.compile(r"^\W*(?:(?:please|umay)\W+)*(?:stop|cancel|abort|end)\s+(?:the\s+|that\s+|this\s+)?(?:deep\s+)?research\b|"
                        r"^\W*ara[sş]t[ıi]rma(?:y[ıi])?\s+(?:durdur|iptal)", re.I)
STATUS_ASKED = re.compile(r"\b(?:how(?:'s| is)\s+(?:the|that)\s+research|research\s+(?:status|progress)|"
                          r"ara[sş]t[ıi]rma\w*\s+(?:durum|ne\s+durumda|nas[ıi]l))", re.I)


def command(text):
    """The answer to "stop the research" / "how is the research going" while a job runs (and to "stop" when none does),
    else None: no model, a job can't wait for the planner to read it."""
    text = text or ""
    if STOP_ASKED.search(text):
        return ("Stopping the research, dear. It ends after the request it is on; the pages it read are kept."
                if stop() else "There is no research running, dear.")
    if STATUS_ASKED.search(text) and status():
        return f"{status()} It takes 15 to 40 minutes in all, dear."
    return None


def resume():
    """After a restart: finishes the newest unfinished job of the last MAX_AGE_HOURS (the plan and the pages already read
    are on the disk)."""
    try:
        files = sorted(home().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in files[:5]:
            job = json.loads(path.read_text(encoding="utf-8"))
            if job.get("done") or time.time() - path.stat().st_mtime > MAX_AGE_HOURS * 3600:
                continue
            with _lock:
                if _job["thread"] is not None and _job["thread"].is_alive():
                    return False
                if not planner_server.available():
                    return False
                _launch(job)
            _say(job, f"Umay restarted; I'm going on with the research \"{job['topic'][:80]}\".")
            return True
    except (OSError, ValueError, KeyError):
        pass
    return False
