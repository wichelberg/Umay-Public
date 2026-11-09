"""Umay's "heavy model": a sharper model than the local 4b, asked only where the 4b measurably fails (plan 21).

It plans multi-step requests (task_plan) and aims clicks on the screen (input_tools.click's `target`).
The 4b does everything else. Measured 2026-11-05 (development/planned.txt, item 21): on real screens Claude
Code Haiku at effort low hit 27/29 click targets (2 px median), as well as Sonnet at any effort; it wrote
sound plans where the 4b skipped steps.

One setting, UMAY_HEAVY_MODEL:
- `claude-code:haiku:low` (default): `claude -p` on the owner's Claude Code subscription. The owner's rule
  (2026-11-05): never OpenRouter for this, it must cost no money. A call is ~5-10 s and roughly 0.001% of
  the weekly plan usage (measured; Claude Code reports usage only in 1% steps, so that is an estimate).
- `claude-code:<model>:<effort>`: another Claude Code model / effort (sonnet costs ~15x more usage).
- `off`: no heavy model; plans are skipped and clicks use the 4b's own point.
(Later, with more VRAM, a local 27b model can take this slot; that path isn't written yet.)

Each call runs claude with no tools at all, or only Read when it has to see a screenshot (written to its own
folder, %LOCALAPPDATA%\\Umay\\heavy, and deleted after), a short system prompt of ours, no MCP servers, no
user settings, no saved session and --safe-mode: without it claude still read the owner's global CLAUDE.md, and
plans told Umay to ask about "another development plan" (2026-11-06). At most UMAY_HEAVY_MAX_CALLS (40) calls per request: after that, or when
claude is missing, offline or fails, ask() returns None and the caller does without it.
Every call is logged to %LOCALAPPDATA%\\Umay\\heavy_model.log with its time, list price and the plan usage
Claude Code reports (5-hour and weekly window).
"""
import json
import os
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import claude_bridge

DEFAULT = "claude-code:haiku:low"
MAX_CALLS = 40  # a plan, a check per step and an aim per click: a three-field mail used up 20 (2026-11-06)
TIMEOUT_S = 120
SYSTEM = ("You are a careful helper for Umay, a desktop assistant on a Windows PC. Answer exactly what you are "
          "asked, in the JSON form asked for, and nothing else.")

_local = threading.local()
_log_lock = threading.Lock()


def setting():
    """(model, effort) for Claude Code, or None when the heavy model is off or the setting is unknown."""
    raw = os.environ.get("UMAY_HEAVY_MODEL", "").strip().lower() or DEFAULT
    parts = raw.split(":")
    if len(parts) == 3 and parts[0] == "claude-code" and parts[1] and parts[2]:
        return parts[1], parts[2]
    return None


def on():
    return setting() is not None


def max_calls():
    try:
        return max(0, int(os.environ.get("UMAY_HEAVY_MAX_CALLS", "").strip() or MAX_CALLS))
    except ValueError:
        return MAX_CALLS


scrub = lambda prompt: prompt   # ai_assistant sets it: masks what the memory knows about the owner (memory_cortex.redact)


def new_request():
    """A new request from the owner (ai_assistant._send_message): the call budget starts over."""
    _local.calls = 0


def calls_used():
    return getattr(_local, "calls", 0)


def _home():
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Umay"


def command(exe, prompt, model, effort, system_file, schema=None, read=False):
    """The claude command line for one question (the system prompt goes through a file: the planner's, with
    the tool list, is ~12,000 characters and Windows command lines end at 32,767)."""
    cmd = [exe, "-p", prompt, "--model", model, "--effort", effort, "--tools", "Read" if read else "",
           "--system-prompt-file", str(system_file), "--strict-mcp-config", "--no-session-persistence",
           "--safe-mode", "--setting-sources", "project", "--output-format", "stream-json", "--verbose"]
    if schema is not None:
        cmd += ["--json-schema", json.dumps(schema)]
    return cmd


def parse(stdout):
    """What claude's stream-json output says: the answer (text or structured), the list price, the plan usage
    it reported (0..1, or None) and whether it ended in an error."""
    out = {"text": "", "structured": None, "cost": 0.0, "usage5h": None, "usage7d": None, "error": True}
    for line in (stdout or "").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "rate_limit_event":
            windows = (event.get("rate_limit_info") or {}).get("unifiedWindows") or {}
            out["usage5h"] = (windows.get("five_hour") or {}).get("utilization", out["usage5h"])
            out["usage7d"] = (windows.get("seven_day") or {}).get("utilization", out["usage7d"])
        elif event.get("type") == "result":
            out["text"] = (event.get("result") or "").strip()
            out["structured"] = event.get("structured_output")
            out["cost"] = float(event.get("total_cost_usd") or 0)
            out["error"] = bool(event.get("is_error"))
    return out


def _log(purpose, seconds, result, note=""):
    line = (f"{datetime.now():%Y-%m-%d %H:%M:%S} {purpose} {seconds:.1f}s ${result.get('cost', 0):.4f} "
            f"5h={result.get('usage5h')} 7d={result.get('usage7d')} calls={calls_used()}"
            + (f" {note}" if note else ""))
    try:
        with _log_lock:
            _home().mkdir(parents=True, exist_ok=True)
            with open(_home() / "heavy_model.log", "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError:
        pass


def ask(prompt, purpose, schema=None, image=None, system=SYSTEM):
    """One question to the heavy model. `image`: JPEG bytes it should look at (it reads them from a file named
    in the prompt's `{image}`). Returns the structured answer (with `schema`) or the text; None when there is
    no heavy model, the request's budget is used up, or the call fails."""
    chosen = setting()
    if chosen is None or calls_used() >= max_calls():
        return None
    exe = claude_bridge._claude_exe()
    if not exe:
        return None
    _local.calls = calls_used() + 1
    prompt = scrub(prompt)  # every way to this cloud model goes through here
    work = _home() / "heavy"
    key = uuid.uuid4().hex[:10]
    shot, system_file = work / f"shot_{key}.jpg", work / f"system_{key}.txt"
    started = time.monotonic()
    try:
        work.mkdir(parents=True, exist_ok=True)
        system_file.write_text(system, encoding="utf-8")
        if image is not None:
            shot.write_bytes(image)
            prompt = prompt.replace("{image}", shot.name)
        proc = subprocess.run(command(exe, prompt, *chosen, system_file, schema, read=image is not None),
                              cwd=work, env=claude_bridge.clean_env(), capture_output=True,
                              stdin=subprocess.DEVNULL, timeout=TIMEOUT_S, creationflags=subprocess.CREATE_NO_WINDOW)
        result = parse(proc.stdout.decode("utf-8", errors="replace"))
    except (OSError, subprocess.SubprocessError) as e:
        _log(purpose, time.monotonic() - started, {}, f"FAILED {type(e).__name__}: {e}"[:200])
        return None
    finally:
        for leftover in (shot, system_file):
            try:
                leftover.unlink(missing_ok=True)
            except OSError:
                pass
    if result["error"]:
        _log(purpose, time.monotonic() - started, result, f"ERROR {result['text'][:150]}")
        return None
    _log(purpose, time.monotonic() - started, result)
    if schema is not None:
        if isinstance(result["structured"], dict):
            return result["structured"]
        try:  # an older claude without structured output: the JSON is in the text
            found = json.loads(result["text"][result["text"].index("{"):result["text"].rindex("}") + 1])
            return found if isinstance(found, dict) else None
        except ValueError:
            return None
    return result["text"] or None
