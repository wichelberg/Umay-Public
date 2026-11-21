"""Topic research (the owner's wish, 2026-11-20): what the owner asks Umay to look up is kept, studied properly while they
are away, written into the memory as many small dated neurons, and looked at again now and then for anything new.

1. The ledger (table `topics` in the cortex file). `note_search` is called by the tool loop for every web_search: the
   owner's own question (the search query) and the addresses the search returned, never Umay's answer (she made things up
   before: the cortex learns the owner's words and what tools returned, not her sentences). A query that nearly repeats a
   topic is the same topic (asks + 1). Weather, prices and times are not topics, and nothing private (memory_cortex.private_in).
2. Study (`study_next`, called by the cortex learner when the owner is away, never in a game, one job at a time, at most
   TOPICS_PER_DAY a day): research.py's local job (gpt-oss on the CPU, claims kept only with a quote found in the page,
   no cloud model) in its quiet form (source "topic": shorter, tells nobody). A topic is studied again after REFRESH_DAYS.
   The job stops when the owner comes back (`pause_if_back`).
3. Memory (`ingest`, called by research.py when a job is done, the owner's own "research X" too): SELF > Knowledge > <topic>,
   one neuron per verified claim, each with the date its source gave (or the day it was learnt), the address and the quote.
   A claim that says the same as a neuron is not added again (it refreshes it: newer date, better label); one that says the
   same thing with other numbers replaces it (the old one stays as history: valid_to). The topic neuron holds the latest summary.
   This is separate from UMAY_SELF_GROW (the old newsfeed research stays as it was).
4. Recall (`knowledge_for`, added to the reply's note by ai_assistant._turn; `memory_cortex.search_text` finds them too): the
   neurons a message shares words with, newest first, each with its date and "can be out of date". Words, not embeddings:
   _turn must stay a few milliseconds.

Settings: UMAY_TOPIC_RESEARCH (on), UMAY_TOPIC_REFRESH_DAYS (3), UMAY_TOPIC_PER_DAY (2), UMAY_TOPIC_MAX (40 topics kept),
UMAY_TOPIC_PAGES (10) and UMAY_TOPIC_MINUTES (30) for one job.
"""
import os
import re
import time
import uuid
from datetime import datetime

import memory_cortex

TOPICS_PER_DAY = 2
REFRESH_DAYS = 3
MAX_TOPICS = 40
MAX_NODES = 60            # current neurons per topic: the weakest, oldest go to history beyond this
TRY_GAP_SECONDS = 30 * 60  # a job that was stopped (the owner came back) is not restarted at once
# Measured with the cortex's embedding model (2026-11-21): the same fact reworded 0.81-0.92, one number changed 0.97, two events
# at one place 0.80, related other facts 0.65-0.71, unrelated 0.34. A wrongly merged fact is lost, a duplicate only costs a line.
SAME_SIM = 0.85           # an embedding this close (or SAME_WORDS shared), with the same numbers, is the same claim
REPLACE_SIM = 0.9         # this close with other numbers: the new claim replaces the old one
SAME_WORDS = 0.75
SAME_TOPIC = 0.6
RECALL_LIMIT = 6
CONFIDENCE = {"verified": 0.7, "single source": 0.4, "contested": 0.2}

# Not worth studying: they change by the hour and a note about them is wrong tomorrow.
EPHEMERAL = re.compile(r"\b(weather|forecast|temperature|what time|time in|exchange rate|stock price|price of|score|"
                       r"hava durumu|saat kaç|kur)\b", re.I)
STOP = frozenset("the and for with from about that this what which who whom whose are was were has have had not you your "
                 "can could would should will does did how why when where into than then them they their its there here "
                 "some any more most over under also just only very much many latest news new today now please tell "
                 "show find look search open".split())
ADDRESS = re.compile(r"https?://[^\s)\]>\"']+")
MONTHS = {m: i for i, m in enumerate(("january february march april may june july august september october november "
                                      "december").split(), 1)}
DATE_ISO = re.compile(r"\b((?:19|20)\d\d)-(\d{1,2})-(\d{1,2})\b")
DATE_DMY = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+((?:19|20)\d\d)\b")
DATE_MDY = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+((?:19|20)\d\d)\b")


def enabled():
    return os.environ.get("UMAY_TOPIC_RESEARCH", "on").strip().lower() not in ("off", "0", "false", "no")


def _int(name, default, low=1):
    try:
        return max(low, int(os.environ.get(name, "").strip() or default))
    except ValueError:
        return default


def words(text):
    """The words that say what a text is about: lower case, no filler."""
    return {w for w in re.findall(r"[^\W_]+", (text or "").lower()) if len(w) > 2 and w not in STOP}


def _stems(text):
    return {w[:5] for w in words(text)}


def _jaccard(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


def _month(name):
    """1-12 for "November", "Nov", "Sept"; None for what is no month."""
    name = name.lower()
    return next((i for m, i in MONTHS.items() if len(name) >= 3 and m.startswith(name)), None)


def parse_date(text):
    """The epoch milliseconds (noon) of the first date written in `text` ("20 November 2026", "2026-11-18", "November 18,
    2026"), else None. A date nobody wrote is never made up."""
    text = str(text or "")
    found = None
    for pattern, order in ((DATE_ISO, "ymd"), (DATE_DMY, "dmy"), (DATE_MDY, "mdy")):
        m = pattern.search(text)
        if not m:
            continue
        try:
            if order == "ymd":
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            elif order == "dmy":
                d, mo, y = int(m.group(1)), _month(m.group(2)), int(m.group(3))
            else:
                mo, d, y = _month(m.group(1)), int(m.group(2)), int(m.group(3))
            if mo:
                found = int(datetime(y, mo, d, 12).timestamp() * 1000)
                break
        except (ValueError, OverflowError):
            continue
    return found


def _store(store=None):
    return store or memory_cortex.store()


def _now(now=None):
    return float(now) if now is not None else time.time()


# --- the ledger -----------------------------------------------------------------------------------------------------

def _same_topic(a, b):
    wa, wb = words(a), words(b)
    if not wa or not wb:
        return False
    small, big = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    return _jaccard(wa, wb) >= SAME_TOPIC or (len(small) >= 3 and small <= big)


def note_search(owner_text, query, result, now=None, store=None):
    """A web_search the owner asked for: its query is a topic of theirs. The id of the topic row, or None when it isn't one
    (switched off, a weather or price lookup, private, nothing came back)."""
    if not enabled():
        return None
    query = " ".join(str(query or "").split())[:120]
    result = str(result or "")
    if not words(query) or EPHEMERAL.search(query) or len(result) < 60 or result.startswith(("Not searched", "No results")):
        return None
    if memory_cortex.private_in(query, owner_text or ""):
        return None
    urls = list(dict.fromkeys(u.rstrip(".,;:") for u in ADDRESS.findall(result)))[:8]
    t = int(_now(now) * 1000)
    s = _store(store)
    with s._lock, s._db() as c:
        rows = c.execute("SELECT * FROM topics WHERE status='open'").fetchall()
        for r in rows:
            if _same_topic(r["topic"], query):
                merged = " ".join(dict.fromkeys(((r["urls"] or "").split() + urls)))[:1500]
                c.execute("UPDATE topics SET asks=asks+1, last_asked=?, urls=? WHERE id=?", (t, merged, r["id"]))
                return r["id"]
        tid = f"t{uuid.uuid4().hex[:10]}"
        c.execute("INSERT INTO topics(id,topic,first_asked,last_asked,asks,studied,urls,status) VALUES(?,?,?,?,1,0,?,'open')",
                  (tid, query, t, t, " ".join(urls)))
        extra = c.execute("SELECT id FROM topics WHERE status='open' AND studied=0 ORDER BY first_asked DESC LIMIT -1 OFFSET ?",
                          (_int("UMAY_TOPIC_MAX", MAX_TOPICS),)).fetchall()
        for r in extra:  # beyond the cap the oldest never-studied topics are dropped
            c.execute("DELETE FROM topics WHERE id=?", (r["id"],))
    return tid


def topics(store=None):
    with _store(store)._db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM topics WHERE status='open' ORDER BY first_asked")]


def due(now=None, store=None):
    """The topics to study now, best first: never studied (asked most, then newest), then those studied longest ago."""
    t = _now(now)
    limit = (t - _int("UMAY_TOPIC_REFRESH_DAYS", REFRESH_DAYS) * 86400) * 1000
    rows = [r for r in topics(store) if not r["last_studied"] or r["last_studied"] < limit]
    rows.sort(key=lambda r: (bool(r["last_studied"]), r["last_studied"] or 0, -r["asks"], -r["first_asked"]))
    return rows


def studied_today(now=None, store=None):
    since = (_now(now) - 86400) * 1000
    return sum(1 for r in topics(store) if (r["last_studied"] or 0) > since)


def study_next(away, now=None, store=None, start=None, ready=None):
    """Starts the next due topic's quiet research when the owner is away and nothing else is being researched. The topic
    started, or None. `start(topic, source=...)` and `ready()` are research.start and "the local planner can answer" (tests
    give their own)."""
    if not enabled() or not away:
        return None
    import research
    import llama_server
    import planner_server
    ready = ready or (lambda: planner_server.available() and not llama_server.hold["on"])
    if research.status() or not ready():
        return None
    t = _now(now)
    s = _store(store)
    if studied_today(t, s) >= _int("UMAY_TOPIC_PER_DAY", TOPICS_PER_DAY):
        return None
    if t - float(s.meta("topic_try_at", 0)) < TRY_GAP_SECONDS:
        return None
    rows = due(t, s)
    if not rows:
        return None
    s.set_meta("topic_try_at", t)
    topic = rows[0]["topic"]
    (start or research.start)(topic, source="topic")
    memory_cortex._log(f"topic research: started {topic!r} (asked {rows[0]['asks']}x)")
    return topic


def pause_if_back(away):
    """The owner is back: a quiet job ends after its current request, so the PC is theirs (it is tried again later)."""
    if away:
        return False
    import research
    return research.topic_running() and research.stop()


# --- the memory -----------------------------------------------------------------------------------------------------

def _holder(s, topic, vec_of):
    for n in s.children("s:knowledge"):
        if n["label"] == topic[:60]:
            return n["id"]
    return s.add_node(topic[:60], "s:knowledge", branch="self", importance=0.4, confidence=0.6, source="research",
                      said=memory_cortex.now_ms(), embedding=vec_of(topic))


def _numbers(text):
    return set(re.findall(r"\d[\d.,]*\d|\d", text or ""))


def _group_date(group):
    dates = [d for d in (parse_date(m.get("published")) for m in group.get("members") or []) if d]
    return min(dates) if dates else None


def ingest(topic, findings, summary=None, now=None, store=None, embed=None):
    """What a finished research job verified, into SELF > Knowledge > <topic> (see the module's text). The number of neurons
    added or replaced; a claim already known only refreshes its neuron. `findings`: research.py's [{"question", "groups":
    [{"claim", "members", "sources", "against", "label"}]}]."""
    groups = [g for item in findings or [] for g in item.get("groups") or [] if str(g.get("claim") or "").strip()]
    s = _store(store)
    t = int(_now(now) * 1000)
    embed = embed or memory_cortex.embed
    topic = " ".join(str(topic).split())

    def vectors(texts):
        try:
            return embed(texts) or [None] * len(texts)
        except Exception:
            return [None] * len(texts)

    holder = _holder(s, topic, lambda text: (vectors([text]) or [None])[0])
    claims = [" ".join(str(g["claim"]).split())[:200] for g in groups]
    vecs = vectors(claims) if claims else []
    current = s.children(holder)
    changed = 0
    for g, claim, vec in zip(groups, claims, vecs):
        label = g.get("label") or "single source"
        said = _group_date(g) or t
        text = ("Disputed: " if label == "contested" else "") + claim
        best, best_sim = None, 0.0
        for n in current:
            sim = 0.0
            if vec is not None and n.get("embedding"):
                sim = float(memory_cortex.np.frombuffer(n["embedding"], dtype=memory_cortex.np.float32) @ vec) \
                    if len(n["embedding"]) == 4 * len(vec) else 0.0
            sim = max(sim, 0.9 if _jaccard(words(n["label"]), words(text)) >= SAME_WORDS else 0.0)
            if sim > best_sim:
                best, best_sim = n, sim
        urls = " ".join(dict.fromkeys(m.get("url", "") for m in g.get("members") or []))[:400]
        quote = str((g.get("members") or [{}])[0].get("quote") or "")[:300]
        source = f"web ({label}, {g.get('sources', 1)} source(s)): {urls}"[:500]
        confidence = CONFIDENCE.get(label, 0.4)
        same_numbers = best is not None and _numbers(best["label"]) == _numbers(text)
        if best is not None and best_sim >= SAME_SIM and same_numbers:
            # the same claim: it is seen again, maybe by a better label or on a newer date
            fields = {}
            if confidence > (best["confidence"] or 0):
                fields["confidence"] = confidence
            if said > (best["said"] or 0):
                fields["said"] = said
            if urls and urls.split()[0] not in (best["source"] or ""):
                fields["source"] = (f"{best['source'] or ''} {urls}")[:600]
            if fields:
                s.update(best["id"], **fields)
            continue
        new = s.add_node(text[:200], holder, branch="self", importance=0.3, confidence=confidence, source=source,
                         said=said, detail=quote or None, embedding=vec)
        if best is not None and best_sim >= REPLACE_SIM:  # other numbers: the new one replaces the old, which stays as history
            s.supersede(best["id"])
            s.link(new, best["id"], "updates")
            current = [n for n in current if n["id"] != best["id"]]
        current.append(s.node(new))
        changed += 1
    current.sort(key=lambda n: ((n["confidence"] or 0), n["said"] or 0))
    for old in current[:max(0, len(current) - MAX_NODES)]:
        s.supersede(old["id"])
    summary_text = " ".join(str((summary or {}).get("summary") or "").split())
    fields = {"said": t}
    if summary_text:
        fields["detail"] = summary_text[:700]
    s.update(holder, **fields)
    _mark_studied(s, topic, t, holder)
    memory_cortex._log(f"topic research: {topic!r} -> {changed} new or replaced neurons ({len(groups)} claims)")
    return changed


def _mark_studied(s, topic, t, holder):
    """The ledger row of `topic` (made when the owner asked for the research directly) is studied now."""
    with s._lock, s._db() as c:
        for r in c.execute("SELECT id, topic FROM topics WHERE status='open'").fetchall():
            if _same_topic(r["topic"], topic):
                c.execute("UPDATE topics SET last_studied=?, studied=studied+1, holder=? WHERE id=?", (t, holder, r["id"]))
                return
        c.execute("INSERT INTO topics(id,topic,first_asked,last_asked,asks,last_studied,studied,urls,holder,status) "
                  "VALUES(?,?,?,?,1,?,1,'',?,'open')", (f"t{uuid.uuid4().hex[:10]}", topic[:120], t, t, t, holder))


# --- recall ---------------------------------------------------------------------------------------------------------

def _date(ms):
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d") if ms else "undated"


def recall(text, store=None, limit=RECALL_LIMIT):
    """[(neuron, holder)] the studied neurons `text` shares words with, best first (words, never an embedding)."""
    want = _stems(text)
    if not want:
        return []
    s = _store(store)
    with s._db() as c:
        holders = [r["holder"] for r in c.execute("SELECT holder FROM topics WHERE studied>0 AND holder IS NOT NULL")]
    found = []
    for hid in dict.fromkeys(holders):
        h = s.node(hid)
        if not h or h["valid_to"] is not None:
            continue
        topic_hit = len(want & _stems(h["label"])) / (len(_stems(h["label"])) or 1)
        for n in s.children(hid):
            hit = len(want & _stems(n["label"]))
            if hit >= 2 or (hit and topic_hit >= 0.5):
                found.append((hit + topic_hit, n["said"] or 0, n, h))
    found.sort(key=lambda f: (-f[0], -f[1]))
    return [(n, h) for _, _, n, h in found[:limit]]


def knowledge_for(text, store=None, limit=RECALL_LIMIT):
    """A note for the reply: what Umay researched earlier about what the message names, with dates ("" when nothing)."""
    if not enabled() or not text:
        return ""
    try:
        got = recall(memory_cortex.HINT.sub("", text), store, limit)
        if not got:
            return ""
        lines = [f"- {n['label']} [{_date(n['said'])}]" for n, _ in got]
        return ("\n\n(What you researched earlier on this, each with the date its source gave. Say it as of that date: "
                "it may be out of date, and if the owner wants the latest, search again. Use it only if it fits:\n"
                + "\n".join(lines) + ")")
    except Exception as e:  # memory must never break a reply
        memory_cortex._log(f"topic recall failed: {type(e).__name__}: {e}")
        return ""
