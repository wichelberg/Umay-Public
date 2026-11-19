"""The local planner's own model server (the owner's choice, 2026-11-18): the plan that Haiku writes through `claude -p` is
written by gpt-oss-20b Heretic, in a `llama-server` process of its own, on the CPU and in RAM, with Haiku as the back-up.

Why this model and why the CPU: it is a Mixture-of-Experts model (21 B in all, ~3.6 B active per token), so RAM is enough: on
this PC (8 cores) it writes ~15-20 tokens/s, a plan takes 7 s at the median (Haiku ~7 s), 22 s at the 90th percentile. The
VRAM stays with the talker, Whisper and the voice. A dense 9B (Qwen3.5-9B Heretic) wrote 9 tokens/s: 23 s at the median.
Measured on 24 of the owner's own requests (development/model_tests/run_plans.py): 24/24 usable, 1 of 24 plans with an
invented tool (the schema's tool enum makes it 0), the same first tool as Haiku's plan in 10 of 22. Its plans are thinner
than Haiku's (median 1 step against 2); reading them side by side is the owner's.

- Started by the first plan that needs it (`ensure`; not ahead when she listens: 10 GB of RAM for a request that is
  mostly not a plan); ended after UMAY_PLANNER_IDLE_MINUTES (20; 0 = never) without a request. RAM while it runs, measured 2026-11-18: 17.3 GB working set,
  of which 10.3 GB private and 7 GB the model's mapped pages (the OS's file cache: health_monitor leaves them out,
  `llama_server.reclaimable_bytes` counts every llama-server). Cold start measured the same day: server up 14 s, the first
  plan ever 53 s (the 3,200-token prompt is read), after a restart 28 s in all (prompt state restored in 0.1 s), a plan
  with the server warm 14 s.
- A GAME (`llama_server.hold`, set by voice_guard) has the machine: `ensure` refuses and a running server is stopped, so the
  plan goes to Haiku, as before the local planner.
- The planner's prompt (system prompt + 71 tools, ~3,200 tokens) takes 45 s to read on the CPU: its saved state (320 MB) is
  put back into the slot after a restart (`restore_prefix`), like llama_server does for its model.
- It ends with Umay (a Windows job object, coding_server._bind_to_umay); `cleanup` ends one an earlier Umay left.
Settings: UMAY_PLANNER_MODEL (the .gguf), UMAY_PLANNER_EXE (else UMAY_CODING_EXE, UMAY_LLAMA_EXE), UMAY_PLANNER_PORT (52633),
UMAY_PLANNER_CONTEXT (8192), UMAY_PLANNER_THREADS (the physical cores), UMAY_PLANNER_REASONING (low), UMAY_PLANNER_IDLE_MINUTES.
"""
import hashlib
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import psutil
import requests

import coding_server
import llama_server

PORT_DEFAULT = 52633
CONTEXT_DEFAULT = 8192
IDLE_MINUTES_DEFAULT = 20
START_SECONDS = 240
SERVED_NAME = "umay-planner"
IDLE_CHECK_SECONDS = 30
PREFIX_MIN_CHARS = 1500
PREFIX_KEEP = 2

_state = {"pid": 0, "inflight": 0, "last": time.monotonic(), "watcher": False, "restored_pid": 0}
_lock = threading.RLock()


def _env(name, default=""):
    return os.environ.get(name, "").strip() or default


def _int(name, default, low=0):
    try:
        return max(low, int(_env(name, str(default))))
    except ValueError:
        return default


def exe():
    return Path(_env("UMAY_PLANNER_EXE") or _env("UMAY_CODING_EXE") or _env("UMAY_LLAMA_EXE") or str(coding_server.DEFAULT_EXE))


def model_path():
    return Path(_env("UMAY_PLANNER_MODEL"))


def available():
    """The settings name a model file and a llama-server.exe that exist."""
    return bool(_env("UMAY_PLANNER_MODEL")) and model_path().is_file() and exe().is_file()


def port():
    return _int("UMAY_PLANNER_PORT", PORT_DEFAULT, 1)


def context():
    return _int("UMAY_PLANNER_CONTEXT", CONTEXT_DEFAULT, 4096)


def threads():
    return _int("UMAY_PLANNER_THREADS", psutil.cpu_count(logical=False) or 4, 1)


def idle_minutes():
    try:
        return max(0.0, float(_env("UMAY_PLANNER_IDLE_MINUTES", str(IDLE_MINUTES_DEFAULT))))
    except ValueError:
        return float(IDLE_MINUTES_DEFAULT)


def base():
    return f"http://127.0.0.1:{port()}"


def chat_url():
    return f"{base()}/v1/chat/completions"


def _home():
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Umay"


def slots_dir():
    return _home() / "planner_slots"


def command():
    """The command line: CPU only (the flag here and CUDA_VISIBLE_DEVICES in `ensure`), one slot, local only, the reasoning
    effort of a gpt-oss model (UMAY_PLANNER_REASONING, low: more thinking is slower and wrote no better plans)."""
    reasoning = _env("UMAY_PLANNER_REASONING", "low")
    return [str(exe()), "-m", str(model_path()), "-c", str(context()), "--host", "127.0.0.1", "--port", str(port()),
            "-ngl", "0", "-t", str(threads()), "-np", "1", "--jinja", "--alias", SERVED_NAME, "-fa", "on",
            "--slot-save-path", str(slots_dir()),
            "--chat-template-kwargs", json.dumps({"reasoning_effort": reasoning})]


def answers(timeout=1.0):
    try:
        r = requests.get(f"{base()}/health", timeout=timeout)
        return r.ok and r.json().get("status") == "ok"
    except (requests.RequestException, ValueError):
        return False


def _processes():
    found = []
    for process in psutil.process_iter(["name", "cmdline"]):
        try:
            line = process.info["cmdline"] or []
            if "llama-server" in (process.info["name"] or "").lower() and str(port()) in line:
                found.append(process)
        except (psutil.Error, OSError):
            pass
    return found


def running():
    """Our server process is there (it may still be loading)."""
    pid = _state["pid"]
    if pid:
        try:
            if psutil.pid_exists(pid) and "llama-server" in psutil.Process(pid).name().lower():
                return True
        except psutil.Error:
            pass
        _state["pid"] = 0
    return False


def stop(force=False):
    """Ends the server THIS process started. False, nothing done, while a request is in flight unless `force`."""
    with _lock:
        if _state["inflight"] and not force:
            return False
        if _state["pid"]:
            try:
                psutil.Process(_state["pid"]).kill()
            except (psutil.Error, OSError):
                pass
        _state["pid"] = 0
        return True


def cleanup():
    """Ends a planner server nobody here started (an earlier Umay's): at Umay's start and before `ensure` starts its own."""
    with _lock:
        for process in _processes():
            if process.pid != _state["pid"]:
                try:
                    process.kill()
                except (psutil.Error, OSError):
                    pass


def ensure():
    """Makes the server answer (blocks up to START_SECONDS). Raises requests.ConnectionError when it can't: no files, a game
    has the machine, or it doesn't come up. The caller then uses Haiku."""
    if llama_server.hold["on"]:
        raise requests.ConnectionError("a game has the machine: the local planner stays off")
    with _lock:
        _state["last"] = time.monotonic()
        if running() and answers():
            return
        if not available():
            raise requests.ConnectionError("the planner's llama-server.exe or model isn't where the settings say")
        stop(force=True)
        cleanup()
        slots_dir().mkdir(parents=True, exist_ok=True)
        home = _home()
        home.mkdir(parents=True, exist_ok=True)
        try:
            with open(home / "planner_server.log", "ab") as log:
                log.write(f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} planner server starting\n".encode("utf-8"))
                log.flush()
                process = subprocess.Popen(
                    command(), cwd=str(exe().parent), stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                    env={**os.environ, "CUDA_VISIBLE_DEVICES": "-1"},      # no CUDA context: 0 VRAM, the card stays whole
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as e:
            raise requests.ConnectionError(f"the planner's llama-server didn't start: {e}") from None
        _state["pid"] = process.pid
        coding_server._bind_to_umay(process)
        deadline = time.monotonic() + START_SECONDS
        while time.monotonic() < deadline and process.poll() is None:
            if llama_server.hold["on"]:
                break
            if answers():
                _watch()
                return
            time.sleep(0.5)
        stop(force=True)
        raise requests.ConnectionError("the planner's llama-server didn't answer")


def _watch():
    """One thread that ends the server after idle_minutes without a request."""
    with _lock:
        if _state["watcher"]:
            return
        _state["watcher"] = True

    def loop():
        while True:
            time.sleep(IDLE_CHECK_SECONDS)
            with _lock:
                if not running():
                    _state["watcher"] = False
                    return
                limit = idle_minutes()
                if llama_server.hold["on"] and not _state["inflight"]:
                    stop()
                elif limit and not _state["inflight"] and time.monotonic() - _state["last"] > limit * 60:
                    stop()

    threading.Thread(target=loop, name="planner-idle", daemon=True).start()


# --- the saved state of the planner's long prompt ----------------------------------------------------------------

def _fingerprint():
    try:
        size = model_path().stat().st_size
    except OSError:
        size = 0
    return hashlib.sha1(f"{size}|{context()}|{' '.join(command())}".encode("utf-8", "replace")).hexdigest()[:8]


def _prefix_name(system):
    return f"prefix-{_fingerprint()}-{hashlib.sha1(system.encode('utf-8', 'replace')).hexdigest()[:10]}.bin"


def _slot_action(action, name, timeout=60):
    try:
        return requests.post(f"{base()}/slots/0?action={action}", json={"filename": name}, timeout=timeout).ok
    except requests.RequestException:
        return False


def restore_prefix(system):
    """Puts the saved state of the planner's prompt back into the slot, once per server process (the first plan then reads
    ~50 new tokens instead of ~3,200). True when a state was restored."""
    if len(system) < PREFIX_MIN_CHARS:
        return False
    with _lock:
        pid = _state["pid"]
        if not running() or _state["inflight"] or _state["restored_pid"] == pid:
            return False
        _state["restored_pid"] = pid
        if not (slots_dir() / _prefix_name(system)).exists():
            return False
        return _slot_action("restore", _prefix_name(system))


def forget_restore():
    """Lets `restore_prefix` act once more in this server process: another user of the server (research.py) has taken the
    slot with its own prompts, so the next plan's saved prefix has to go back in."""
    with _lock:
        _state["restored_pid"] = 0


def save_prefix(system):
    """Saves the slot's state under this prompt's name, once. Older states beyond PREFIX_KEEP go."""
    if len(system) < PREFIX_MIN_CHARS:
        return False
    with _lock:
        name = _prefix_name(system)
        if not running() or _state["inflight"] or (slots_dir() / name).exists():
            return False
        saved = _slot_action("save", name)
        if saved:
            files = sorted(slots_dir().glob("prefix-*.bin"), key=lambda p: p.stat().st_mtime, reverse=True)
            for old in files[PREFIX_KEEP:]:
                try:
                    old.unlink()
                except OSError:
                    pass
        return saved


def ask_json(prompt, schema, system="", max_tokens=1500, timeout=300):
    """An answer that fits `schema` (the server enforces it, enums included), as a dict. Starts the server first. Raises
    requests' errors / ValueError when it doesn't give one."""
    ensure()
    with _lock:
        _state["inflight"] += 1
    try:
        r = requests.post(chat_url(), timeout=timeout, json={
            "model": SERVED_NAME, "stream": False, "temperature": 0, "max_tokens": max_tokens,
            "messages": ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema}}})
        r.raise_for_status()
        text = r.json()["choices"][0]["message"].get("content") or "{}"
        found = json.loads(text[text.index("{"):text.rindex("}") + 1])
    finally:
        with _lock:
            _state["inflight"] -= 1
            _state["last"] = time.monotonic()
    if not isinstance(found, dict):
        raise ValueError("the answer is not an object")
    return found
