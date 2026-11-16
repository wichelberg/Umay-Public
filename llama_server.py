"""Umay's big local model through llama.cpp's `llama-server` (UMAY_LOCAL_BACKEND=llamacpp): a process of Umay's own.

The owner's choice, 2026-11-16: one model for what is not a tool choice (the NPU's decision model still chooses): the
talking, the arguments, the memory's text work and the plan. It is a Mixture-of-Experts model (Qwen3.6-35B-A3B, 3B
active), so with `-cmoe` its experts sit in RAM (the OS's file cache; 11-19 GB, given back under pressure) and only
the attention and the shared part, ~3 GB with the MTP head, are in VRAM. Numbers and the traps: the project memory
note and development/model_tests/README.md.

Like npu.py's chat server: started by the first request that needs it (`ensure`, blocks until /health answers),
never ended while a request is in flight, a leftover one from an earlier Umay is replaced. `hold["on"]` (set by
voice_guard in a game) makes `ensure` refuse, so nothing mid-game can start it again.

.env: UMAY_LLAMA_EXE (llama-server.exe), UMAY_LLAMA_MODEL (the .gguf), UMAY_LLAMA_PORT (52627),
UMAY_LLAMA_ARGS (DEFAULT_ARGS: where the experts go, MTP, the host prompt cache), UMAY_LLAMA_CONTEXT (24576: the
planner's prompt, the 71 tools, is ~13k tokens). No paths in the code: the repo is public.
"""
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path

import psutil
import requests

PORT_DEFAULT = 52627
CONTEXT_DEFAULT = 24576
START_SECONDS = 240
SERVED_NAME = "umay-llama"
# Measured 2026-11-15/16 (RTX 5070 Laptop 8 GB, 31 GB RAM): -cmoe keeps all experts on the CPU side, 3.0 GB of VRAM with
# the MTP head, 28-31 tokens/s with RAM to spare; --cache-ram 2048 caps the host cache of earlier prompts (default 8192).
DEFAULT_ARGS = "-cmoe --spec-type draft-mtp --spec-draft-n-max 3 --cache-ram 2048"

hold = {"on": False}          # a game has the machine: ensure() refuses
_lock = threading.RLock()
_state = {"pid": 0, "vram_mb": 0.0, "inflight": 0}


def _env(name, default=""):
    return os.environ.get(name, "").strip() or default


def exe():
    return Path(_env("UMAY_LLAMA_EXE", "llama-server.exe"))


def model_path():
    return Path(_env("UMAY_LLAMA_MODEL", "model.gguf"))


def available():
    """The settings name a llama-server.exe and a model file that exist."""
    return bool(_env("UMAY_LLAMA_EXE")) and bool(_env("UMAY_LLAMA_MODEL")) and exe().is_file() and model_path().is_file()


def port():
    try:
        return int(_env("UMAY_LLAMA_PORT", str(PORT_DEFAULT)))
    except ValueError:
        return PORT_DEFAULT


def context():
    """UMAY_LLAMA_CONTEXT: its own setting, since UMAY_LOCAL_CONTEXT (8192) is the Ollama talker's window and the
    planner's prompt alone is ~13k tokens."""
    try:
        return max(2048, int(_env("UMAY_LLAMA_CONTEXT", str(CONTEXT_DEFAULT))))
    except ValueError:
        return CONTEXT_DEFAULT


def base():
    return f"http://127.0.0.1:{port()}"   # not "localhost": Windows tries IPv6 first


def chat_url():
    return f"{base()}/v1/chat/completions"


def slots_on():
    """UMAY_LLAMA_SLOTS=off: no saving of the planner's prompt state (prefix_*)."""
    return _env("UMAY_LLAMA_SLOTS", "on").lower() not in ("off", "0", "false", "no")


def slots_dir():
    return _home() / "slots"


def command():
    """The command line: the pinned part (thinking off, no auto-fit of placement, one slot, local only) plus UMAY_LLAMA_ARGS."""
    return [str(exe()), "-m", str(model_path()), "-c", str(context()), "--fit", "off", "--reasoning", "off", "--jinja",
            "--host", "127.0.0.1", "--port", str(port()), "-ngl", "99", "-np", "1", "--alias", SERVED_NAME,
            *(["--slot-save-path", str(slots_dir())] if slots_on() else []),
            *shlex.split(_env("UMAY_LLAMA_ARGS", DEFAULT_ARGS), posix=False)]


def answers(timeout=1.0):
    try:
        r = requests.get(f"{base()}/health", timeout=timeout)
        return r.ok and r.json().get("status") == "ok"
    except (requests.RequestException, ValueError):
        return False


def _state_file():
    return _home() / "llama_server.json"


def _save():
    """The pid and the VRAM it took, for the HUD: it is a process of its own and reads them through vram_mb()."""
    try:
        _state_file().parent.mkdir(parents=True, exist_ok=True)
        _state_file().write_text(json.dumps({"pid": _state["pid"], "vram_mb": _state["vram_mb"]}), encoding="utf-8")
    except OSError:
        pass


def _load():
    try:
        data = json.loads(_state_file().read_text(encoding="utf-8"))
        _state["pid"], _state["vram_mb"] = int(data["pid"]), float(data["vram_mb"])
    except (OSError, ValueError, KeyError, TypeError):
        pass


def _alive():
    if not _state["pid"]:
        _load()          # another process of Umay (the HUD) didn't start it
    pid = _state["pid"]
    if not pid or not psutil.pid_exists(pid):
        return False
    try:
        return "llama-server" in psutil.Process(pid).name().lower()
    except psutil.Error:
        return False


def reclaimable_bytes():
    """The server's mapped model pages (working set minus private memory): the OS takes them back when another
    program needs the RAM, so they are not a shortage. Windows still counts them as used (measured 2026-11-15: 12-19
    GB). health_monitor and the HUD leave them out. Working set minus the private commit, from memory_info(), which
    costs nothing: memory_full_info() (USS) walks every page of the working set, 300-440 ms a call with 16 GB mapped,
    and the HUD asked twice every 2 s, so its orb stuttered (2026-11-16). The commit is never less than the private
    pages in RAM, so this never overstates what can be given back."""
    total = 0
    for process in psutil.process_iter(["name"]):
        try:
            if "llama-server" in (process.info["name"] or "").lower():
                info = process.memory_info()
                total += max(0, info.rss - getattr(info, "private", info.rss))
        except (psutil.Error, OSError, TypeError, AttributeError):
            pass
    return total


def running():
    """The server process is there (it may still be loading)."""
    return _alive()


def vram_mb():
    """The VRAM it took when it came up (the card's use before and after; nvidia-smi lists no per-process memory on
    Windows), 0 when it isn't running."""
    return _state["vram_mb"] if _alive() else 0.0


def _gpu_used_mb():
    smi = shutil.which("nvidia-smi")
    if not smi:
        return None
    try:
        out = subprocess.run([smi, "--query-gpu=memory.used", "--format=csv,noheader,nounits"], capture_output=True,
                             text=True, timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return float(out.strip().splitlines()[0])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def _home():
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Umay"


def _leftovers():
    """llama-server processes on our port: one an earlier Umay left (its RAM would stay taken)."""
    found = []
    for process in psutil.process_iter(["name", "cmdline"]):
        try:
            line = process.info["cmdline"] or []
            if "llama-server" in (process.info["name"] or "").lower() and str(port()) in line:
                found.append(process)
        except (psutil.Error, OSError):
            pass
    return found


def cleanup():
    """Stops a server an earlier Umay left, unless it is ours. Quiet when there is none."""
    for process in _leftovers():
        if process.pid != _state["pid"]:
            try:
                process.kill()
            except (psutil.Error, OSError):
                pass


def stop(force=False):
    """Ends the server, which gives its RAM and VRAM back. False (and nothing done) while a request is in flight,
    unless `force`. Quiet when there is none."""
    with _lock:
        if _state["inflight"] and not force:
            return False
        for process in _leftovers():
            try:
                process.kill()
            except (psutil.Error, OSError):
                pass
        _state["pid"], _state["vram_mb"] = 0, 0.0
        try:
            _state_file().unlink(missing_ok=True)
        except OSError:
            pass
        return True


def ensure():
    """Makes the server answer (blocks up to START_SECONDS; the model is mapped, so the process is up in seconds but
    its first replies page experts in from the disk). Raises requests.ConnectionError when it can't: no files, a
    game has the machine, or it doesn't come up."""
    if hold["on"]:
        raise requests.ConnectionError("a game has the machine: the local model stays off")
    with _lock:
        if _alive() and answers():
            return
        if not available():
            raise requests.ConnectionError("llama-server.exe or its model isn't where the settings say")
        stop(force=True)
        before = _gpu_used_mb()
        home = _home()
        home.mkdir(parents=True, exist_ok=True)
        if slots_on():
            slots_dir().mkdir(parents=True, exist_ok=True)
        try:
            with open(home / "llama_server.log", "ab") as log:    # the child keeps its own copy of the handle
                # its own lines carry only the seconds since it started: this line says when that was
                log.write(f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} llama-server starting, RAM cap {ram_cap_gb():g} GB, "
                          f"args: {_env('UMAY_LLAMA_ARGS', DEFAULT_ARGS)}\n".encode("utf-8"))
                log.flush()
                process = subprocess.Popen(
                    command(), cwd=str(exe().parent), stdout=log, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as e:
            raise requests.ConnectionError(f"llama-server didn't start: {e}") from None
        _state["pid"] = process.pid
        deadline = time.monotonic() + START_SECONDS
        while time.monotonic() < deadline and process.poll() is None:
            if hold["on"]:
                break
            if answers():
                after = _gpu_used_mb()
                # never 0 while it runs: voice_guard takes 0 for "not loaded" and would try to load it again
                _state["vram_mb"] = max(1.0, after - before) if before is not None and after is not None else 1.0
                if ram_cap_gb():
                    cap_working_set(process.pid, ram_cap_gb())
                _save()
                return
            time.sleep(0.5)
        stop(force=True)
        raise requests.ConnectionError("llama-server didn't answer")


def ram_cap_gb():
    """UMAY_LLAMA_RAM_GB: a hard upper bound for the server's working set (private pages and the model's mapped pages
    together), 0 / empty = none. Pages over it go to Windows' standby list, which is the first thing it takes back when RAM
    is short: without a bound the 35B's 15+ GB of "active" pages pushed the NPU server's memory (d3) out to the disk
    (a choice took 6-33 s instead of 2.5, 2026-11-16)."""
    try:
        return max(0.0, float(_env("UMAY_LLAMA_RAM_GB", "0")))
    except ValueError:
        return 0.0


def cap_working_set(pid, gb):
    """Sets the hard working-set maximum of process `pid` to `gb` GB (SetProcessWorkingSetSizeEx with
    QUOTA_LIMITS_HARDWS_MAX_ENABLE). True when Windows took it."""
    if gb <= 0 or not pid:
        return False
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.SetProcessWorkingSetSizeEx.argtypes = [wintypes.HANDLE, ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.OpenProcess(0x0100 | 0x1000, False, int(pid))   # PROCESS_SET_QUOTA | PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        return bool(k32.SetProcessWorkingSetSizeEx(handle, 64 * 2 ** 20, int(gb * 2 ** 30), 0x2 | 0x4))  # HARDWS_MIN_DISABLE | HARDWS_MAX_ENABLE
    finally:
        k32.CloseHandle(handle)


PREFIX_MIN_CHARS = 1500       # only a long prompt start is worth keeping (the planner's: ~3,100 tokens, 322 MB)
PREFIX_KEEP = 2               # saved prompt states kept on the disk


def _fingerprint():
    """What a saved state is only good for: this model file, window and settings."""
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
    """Puts the saved state of a long prompt start (the planner's system prompt and tool catalog) back into the slot of the
    server that is running, once per server process: after a restart (a rest, a game) the first plan then reads ~50 new
    tokens instead of ~3,100 (measured 2026-11-16: the first plan 49 s -> 21 s; saving 0.2 s, restoring 0.3 s, 322 MB).
    Within one run the server's own RAM cache already brings it back. True when a state was restored."""
    if not slots_on() or len(system) < PREFIX_MIN_CHARS:
        return False
    with _lock:
        pid = _state["pid"]
        if not _alive() or _state["inflight"] or _state.get("restored_pid") == pid:
            return False
        _state["restored_pid"] = pid            # one try per server process, whatever the answer
        if not (slots_dir() / _prefix_name(system)).exists():
            return False
        return _slot_action("restore", _prefix_name(system))


def save_prefix(system):
    """Saves the slot's state under this prompt start's name, if there is none yet (call it after a request that began with
    it). Older states beyond PREFIX_KEEP go. True when one was saved."""
    if not slots_on() or len(system) < PREFIX_MIN_CHARS:
        return False
    with _lock:
        name = _prefix_name(system)
        if not _alive() or _state["inflight"] or (slots_dir() / name).exists():
            return False
        slots_dir().mkdir(parents=True, exist_ok=True)
        saved = _slot_action("save", name)
        if saved:
            files = sorted(slots_dir().glob("prefix-*.bin"), key=lambda p: p.stat().st_mtime, reverse=True)
            for old in files[PREFIX_KEEP:]:
                try:
                    old.unlink()
                except OSError:
                    pass
        return saved


def post_chat(body, timeout=300):
    """POSTs an OpenAI-style chat body (starting the server first if needed); the Response. The server is not
    ended meanwhile."""
    ensure()
    with _lock:
        _state["inflight"] += 1
    try:
        return requests.post(chat_url(), json={**body, "model": SERVED_NAME, "stream": False}, timeout=timeout)
    finally:
        with _lock:
            _state["inflight"] -= 1


def _messages(prompt, system):
    return ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]


def ask_text(prompt, system="", max_tokens=1500, timeout=300, temperature=0.2):
    """A plain answer (no tools). Raises requests' errors / ValueError when the server doesn't give one."""
    r = post_chat({"messages": _messages(prompt, system), "max_tokens": max_tokens, "temperature": temperature}, timeout)
    r.raise_for_status()
    return (r.json()["choices"][0]["message"].get("content") or "").strip()


def ask_json(prompt, schema, system="", max_tokens=3000, timeout=300):
    """An answer that fits `schema` (the server enforces it, enums included), as a dict. Raises when it doesn't come."""
    r = post_chat({"messages": _messages(prompt, system), "max_tokens": max_tokens, "temperature": 0,
                   "response_format": {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema}}},
                  timeout)
    r.raise_for_status()
    text = r.json()["choices"][0]["message"].get("content") or "{}"
    found = json.loads(text[text.index("{"):text.rindex("}") + 1])
    if not isinstance(found, dict):
        raise ValueError("the answer is not an object")
    return found
