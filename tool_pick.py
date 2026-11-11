"""Tool choice by the NPU's decision model (npu.py, d3:mini): the tools nearest to the message by embedding,
then one choice among them and "none", with a probability.

SHADOW ONLY for now: nothing is run on its word. After the model has answered a message, shadow() asks d3 what
it would have called first and writes that next to what the model really called, to
%LOCALAPPDATA%\\Umay\\tool_shadow.jsonl. `python tool_pick.py review` sums it up. UMAY_TOOL_SHADOW=off stops it.

Why: on the test set (development/model_tests, 2026-11-10) d3:mini named the right first tool for 96% of the
commands (the 4b 86%) in ~2 s with 15 candidates, and its probability tells a doubtful choice from a sure one
(median 0.91 when right, 0.48 when wrong). The set's labels are one person's, so real messages are logged first.
A d3 model can't write a tool's arguments: those stay the talking model's job.

Since 2026-11-11 it also runs for real when UMAY_TOOL_CHOICE=npu (ai_assistant._picked_loop): choose() names the
next tool of a turn, first from the message alone, then from the message and what the tools called so far
answered, until it says none. Each of those choices is a line in tool_picks.jsonl (`python tool_pick.py picks`).
"""
import json
import os
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np

import npu
import task_plan

LOG_NAME = "tool_shadow.jsonl"
TOP = 15                # candidates d3 chooses among: all 71 took 5.6 s, 15 take 1.9 s, and lose nothing measured
DESCRIPTION_CHARS = 220
NONE = "none"
NONE_MEANS = ("No tool: the message is thanks, a goodbye, a remark, something the user tells about themselves, or a "
              "question the assistant can answer from what it knows.")
# Don't add to it: with "Not for the weather, the news, a price ...: those need a search." the weather question was
# still none (0.97) and two chat lines of 24 got a tool ("What are you talking about?" -> search_memory), 2026-11-11.
PICKS_NAME = "tool_picks.jsonl"
SHORT_WORDS = 5         # a message shorter than this ("yes please") is shortlisted with the line before it
QUERY_BEFORE = 300
RESULT_CHARS = 400      # of a tool's answer, in what d3 reads (d3:mini: 1.3 s at 1k tokens, 5.3 s at 4k)
ARGS_CHARS = 160
DONE_SHOWN = 4
# d3 calls a question about the world "none" ("What's the weather in London tomorrow?": none at 0.97) and the model
# then makes the answer up. So a first "none" for a message that asks something gets one more yes/no (0.7 s).
# 2026-11-11: 0.64-0.98 for nine questions that need a search, 0.50 at most for 24 chat lines, 8 questions about the
# owner and 4 a model knows ("What is the capital of Australia?" 0.04).
SEARCH = "web_search"
LOOKUP = 0.6
ASK_LOOKUP = ("The state is a message a user sent to their desktop assistant. To answer it truthfully, does the "
              "assistant have to look something up on the web first: today's weather, the news, a price, a score, a "
              "release, what a person or company did lately, or another fact about the world it can't be sure of?")
ASKS = re.compile(r"\?|^\W*(?:what|why|who|whom|whose|when|where|which|how|is|are|was|were|do|does|did|has|have|can|"
                  r"could|will|would|should|tell me|explain|ne|neden|niye|kim|nerede|nas[ıi]l|ka[cç]|hangi)\b|"
                  r"\bm[ıiuü]\b", re.IGNORECASE)
# Under this a first choice is no choice: the model just answers. Of d3's 99 tool choices on the test set none was
# under 0.35 (3 of the 5 under 0.45 were right); "What was the task? I couldn't hear you." got call_owner at 0.32.
DOUBT = 0.35
# Asked before every choice after the first. 14 hand-written states, 2026-11-11: 0.93-1.00 for the 8 finished ones
# (0.63 for a tool that failed for good), 0.11-0.30 for the 6 with a step left ("Close the music app." after
# list_open_apps).
ASK_DONE = ("The state is a message a user sent to their desktop assistant and what the assistant already did for "
            "it. Is everything the message asks for now done or answered (yes), or is another action still needed "
            "(no)?")
ASK_NEXT = ("The state is a message the user sent to Umay, their desktop assistant, and what Umay has already done "
            "for it. Choose the one tool Umay should call next to finish what the message asks, or none when it is "
            "done or the answers already hold what the user asked for.")
NONE_NEXT = ("Nothing more to call: what the message asks is done, or the answers above already hold what was asked, "
             "or it failed in a way another call won't fix. Never call the same tool again for the same thing.")
ASK = ("The state is a message the user sent to Umay, their desktop assistant. Choose the one tool Umay should call "
       "first to do what the message asks, or none.")

_vectors = {"names": None, "matrix": None}
_lock = threading.Lock()


def _describe(tool):
    fn = tool["function"]
    params = ", ".join((fn.get("parameters") or {}).get("properties", {}))
    return f"({params}) {task_plan._first_sentence(fn.get('description', ''), DESCRIPTION_CHARS)}"


def _tool_vectors(tools):
    """One unit vector per tool, made once for a tool list (its names)."""
    names = tuple(t["function"]["name"] for t in tools)
    with _lock:
        if _vectors["names"] != names:
            _vectors["matrix"] = npu.embed([f"{n}: {_describe(t)}" for n, t in zip(names, tools)])
            _vectors["names"] = names
        return _vectors["matrix"]


def _shortlist(query, tools, top, called=()):
    """Indexes of the tools d3 chooses among: all of a short list, else the `top` nearest to `query` by
    embedding plus the group mates (task_plan.GROUPS) of what this turn already called: after list_open_apps the
    next tool is close_app, whatever the message's words are near to."""
    names = [t["function"]["name"] for t in tools]
    if len(tools) <= top:
        return list(range(len(tools)))
    near = [int(i) for i in np.argsort(-(_tool_vectors(tools) @ npu.embed([query])[0]))[:top]]
    mates = set().union(*[g for g in task_plan.GROUPS if g & set(called)] or [set()])
    return sorted(set(near) | {i for i, n in enumerate(names) if n in mates or n in called})


def _state(text, done, before):
    """What d3 reads: the message alone (what the test set measured), or with the lines before it and what was
    already done for it."""
    if not done and not before:
        return text
    parts = [f"Earlier in the conversation (already handled, context only):\n{before}"] if before else []
    parts.append(f"The user's message now: {text}")
    if done:
        shown = [f"{n}. {name}({json.dumps(args, ensure_ascii=False)[:ARGS_CHARS]}) answered: "
                 f"{' '.join(str(result).split())[:RESULT_CHARS]}"
                 for n, (name, args, result) in enumerate(done, 1)][-DONE_SHOWN:]
        parts.append("Umay already did this for it:\n" + "\n".join(shown))
    return "\n\n".join(parts)


def choose(text, tools, done=(), before="", top=TOP):
    """(the tool to call now or None, how likely d3 finds that, the candidates' names). `done`: this turn's calls
    so far, [(name, args, result)]: with them the question is what to call NEXT, and none means finished.
    `before`: the last lines of the conversation, for a message like "yes please" (they also name the shortlist
    when the message is under SHORT_WORDS words). Raises what npu raises when the server doesn't answer."""
    names = [t["function"]["name"] for t in tools]
    state = _state(text, done, before)
    if done:  # a yes/no first: 0.7 s where the choice among 15 takes 2.2 s, and most turns end after one tool
        over = (npu.decide(state, {"done": {"type": "noul", "instructions": ASK_DONE}}).get("done") or {}).get("noul")
        if isinstance(over, (int, float)) and over >= 0.5:
            return None, over, []
    query = f"{before[-QUERY_BEFORE:]} {text}" if before and len(text.split()) < SHORT_WORDS else text
    criteria = {names[i]: _describe(tools[i]) for i in _shortlist(query, tools, top, [d[0] for d in done])}
    criteria[NONE] = NONE_NEXT if done else NONE_MEANS
    answer = npu.decide(state, {"tool": {
        "type": "choice", "instructions": ASK_NEXT if done else ASK, "criteria": criteria}}).get("tool") or {}
    choice = answer.get("choice")
    if choice not in criteria:
        raise ValueError(f"no choice: {answer}")
    likely = (answer.get("probabilities") or {}).get(choice, answer.get("confidence"))
    if choice != NONE and not done and isinstance(likely, (int, float)) and likely < DOUBT:
        return None, likely, [n for n in criteria if n != NONE]
    if choice == NONE and not done and SEARCH in names and ASKS.search(text):
        needed = (npu.decide(text, {"lookup": {"type": "noul", "instructions": ASK_LOOKUP}}).get("lookup") or {}).get("noul")
        if isinstance(needed, (int, float)) and needed >= LOOKUP:
            return SEARCH, needed, [n for n in criteria if n != NONE]
    return (None if choice == NONE else choice), likely, [n for n in criteria if n != NONE]


def warm(tools):
    """Embeds the tool list ahead (main.py, once the NPU's server answers): the first choice of a session took
    17 s with it, 2 s without (2026-11-11). Quiet when the server isn't there."""
    try:
        if npu.ready() and npu.embed_model() and len(tools) > TOP:
            _tool_vectors(tools)
    except Exception:
        pass


def pick(text, tools, top=TOP):
    """choose() for a message alone: what d3 would call first (the shadow's question)."""
    return choose(text, tools, top=top)


def note_pick(**fields):
    """A line of tool_picks.jsonl: one real choice (ai_assistant._picked_loop). Never raises."""
    try:
        path = log_path().with_name(PICKS_NAME)
        path.parent.mkdir(parents=True, exist_ok=True)
        with _lock, open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), **fields},
                               ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


def log_path():
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Umay" / LOG_NAME


def shadow(text, source, called, tools, wait=False):
    """After a message the model handled: what d3 would have called first, next to the model's own calls
    (`called`: [(name, args), ...]). In a thread of its own, so it never holds an answer up; silent when the NPU
    isn't there or fails."""
    if os.environ.get("UMAY_TOOL_SHADOW", "").strip().lower() == "off" or not text or not npu.ready():
        return None

    def work():
        started = time.monotonic()
        try:
            name, likely, candidates = pick(text, tools)
        except Exception as e:  # the shadow must never touch Umay's answers
            name, likely, candidates = f"{type(e).__name__}: {e}"[:120], None, []
        record = {"at": datetime.now().isoformat(timespec="seconds"), "source": source, "text": text[:400],
                  "d3": name, "p": None if likely is None else round(float(likely), 3),
                  "model": [c[0] for c in called], "in_candidates": bool(called) and called[0][0] in candidates,
                  "seconds": round(time.monotonic() - started, 2)}
        try:
            path = log_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with _lock, open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass
    thread = threading.Thread(target=work, name="tool-shadow", daemon=True)
    thread.start()
    if wait:
        thread.join()
    return thread


def review(path=None):
    """What the log says, as text: how often d3 and the model named the same first tool, and where they differ."""
    path = Path(path or log_path())
    if not path.is_file():
        return f"No log yet: {path}"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = [r for r in rows if r.get("p") is not None]
    if not rows:
        return "No answered rows yet."
    first = lambda r: r["model"][0] if r["model"] else None  # noqa: E731
    same = [r for r in rows if r["d3"] == first(r)]
    lines = [f"{len(rows)} messages; d3 and the model agree on the first tool (or on none) in {len(same)} "
             f"({100 * len(same) / len(rows):.0f}%)",
             f"d3 median {sorted(r['seconds'] for r in rows)[len(rows) // 2]:.1f} s"]
    differ = [r for r in rows if r not in same]
    for label, part in (("the model called a tool, d3 none", [r for r in differ if first(r) and not r["d3"]]),
                        ("d3 a tool, the model none", [r for r in differ if r["d3"] and not first(r)]),
                        ("different tools", [r for r in differ if r["d3"] and first(r)])):
        lines.append(f"\n{label}: {len(part)}")
        lines += [f"  {r['text'][:70]!r}: model {first(r)}, d3 {r['d3']} ({r['p']:.2f})" for r in part[-15:]]
    return "\n".join(lines)


def picks(path=None, last=40):
    """The last real choices, as text: a turn's message, then each round's tool with d3's probability."""
    path = Path(path or log_path().with_name(PICKS_NAME))
    if not path.is_file():
        return f"No log yet: {path}"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()][-int(last):]
    lines = []
    for r in rows:
        if r.get("round") == 1:
            lines.append(f"\n{r['at']} {r.get('text', '')[:90]!r}")
        p = "" if r.get("p") is None else f" ({r['p']:.2f})"
        lines.append(f"  {r.get('round')}. {r.get('error') or r.get('tool') or 'none'}{p}  {r.get('seconds', 0):.1f} s"
                     + (f"  args {r['args_seconds']:.1f} s" if r.get("args_seconds") else ""))
    return "\n".join(lines).strip() or "No rows."


if __name__ == "__main__":
    if sys.argv[1:2] == ["review"]:
        print(review(*sys.argv[2:3]))
    elif sys.argv[1:2] == ["picks"]:
        print(picks(*sys.argv[2:4]))
