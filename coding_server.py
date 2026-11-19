"""Coding mode's own model server: the coding model (UMAY_CODING_MODEL, the owner's TwIL-8B Heretic) in a `llama-server`
process of its own, on its own port, next to Umay's normal models (Ollama's talker, the NPU). Not llama_server.py: that
module is the single big-model BACKEND of the talker (UMAY_LOCAL_BACKEND), and a second model in it would move the talker too.

Measured 2026-11-17/18 on this PC (8 GB card, Q5_K_M, 5.8 GiB): full offload at 16k context 7683 MiB = 94% (voice_guard
calls 95% "a game"); `-ngl 37 -c 12288` with a q8 KV cache 6715 MiB (82%) at 24.9 tokens/s, the same room the 35B took.
The model file is in the OS cache after the first load: the server is up in ~6 s.

- OFF outside coding mode (the owner's rule, 2026-11-18): nothing starts it but the coding window (`warm` when it opens,
  `post_chat` from a task); it stops when the window closes (coding_mode._release, or when the task running then ends),
  after UMAY_CODING_IDLE_MINUTES (10; 0 = never) without a request, for a game's VRAM and for a reply that needs the card.
- Makes room first: Ollama's loaded models are unloaded (the card holds one of them or this, not both).
- A GAME (`llama_server.hold`, set by voice_guard) takes the card: the GPU server is stopped at once (even mid-task: the
  request is repeated, `post_chat`) and coding mode goes on from RAM alone: `-ngl 0` with a few threads
  (UMAY_CODING_GAME_THREADS, half the physical cores) so the game keeps the rest. Slow, and the owner's choice. When
  the game is over the next request starts it on the GPU again (`ensure` notices the wrong mode).
- Gives way: `yield_to_reply` (a normal reply needs the card: `local_llm.working`) stops an idle GPU server.
- While it runs on the GPU voice_guard loads nothing ahead (the talker, Whisper, the voice) and the memory's work waits
  (`local_llm.quiet`).
Settings: UMAY_CODING_MODEL, UMAY_CODING_EXE (else UMAY_LLAMA_EXE), UMAY_CODING_PORT (52628), UMAY_CODING_CONTEXT
(12288), UMAY_CODING_GPU_LAYERS (37), UMAY_CODING_ARGS, UMAY_CODING_IDLE_MINUTES, UMAY_CODING_GAME_THREADS.
"""
import os
import shlex
import subprocess
import threading
import time
from pathlib import Path

import psutil
import requests

import llama_server

PORT_DEFAULT = 52628
CONTEXT_DEFAULT = 12288
GPU_LAYERS_DEFAULT = 37
IDLE_MINUTES_DEFAULT = 10
START_SECONDS = 150
SERVED_NAME = "umay-coding"
DEFAULT_ARGS = "-fa on -ctk q8_0 -ctv q8_0"
DEFAULT_EXE = Path.home() / "Documents" / "Apps" / "llama.cpp" / "bin" / "llama-server.exe"
IDLE_CHECK_SECONDS = 20

_state = {"pid": 0, "inflight": 0, "last": time.monotonic(), "vram_mb": 0.0, "warming": 0, "watcher": False,
          "mode": ""}      # "gpu" | "cpu" (a game): how the running server was started
_lock = threading.RLock()


def _env(name, default=""):
    return os.environ.get(name, "").strip() or default


def exe():
    return Path(_env("UMAY_CODING_EXE") or _env("UMAY_LLAMA_EXE") or str(DEFAULT_EXE))


def model_path():
    return Path(_env("UMAY_CODING_MODEL"))


def available():
    """The settings name a model file and a llama-server.exe that exist."""
    return bool(_env("UMAY_CODING_MODEL")) and model_path().is_file() and exe().is_file()


def _int(name, default, low=0):
    try:
        return max(low, int(_env(name, str(default))))
    except ValueError:
        return default


def port():
    return _int("UMAY_CODING_PORT", PORT_DEFAULT, 1)


def context():
    return _int("UMAY_CODING_CONTEXT", CONTEXT_DEFAULT, 2048)


def idle_minutes():
    try:
        return max(0.0, float(_env("UMAY_CODING_IDLE_MINUTES", str(IDLE_MINUTES_DEFAULT))))
    except ValueError:
        return float(IDLE_MINUTES_DEFAULT)


def base():
    return f"http://127.0.0.1:{port()}"


def chat_url():
    return f"{base()}/v1/chat/completions"


def wanted_mode():
    """"cpu" while a game has the machine (llama_server.hold, set by voice_guard), else "gpu"."""
    return "cpu" if llama_server.hold["on"] else "gpu"


def game_threads():
    """UMAY_CODING_GAME_THREADS: threads of the CPU-only server in a game, default half the physical cores (min 2)."""
    cores = psutil.cpu_count(logical=False) or 4
    return _int("UMAY_CODING_GAME_THREADS", max(2, cores // 2), 1)


def command(mode=None):
    """The command line: the pinned part (thinking off, one slot, local only) + UMAY_CODING_ARGS. "gpu": partial offload
    (UMAY_CODING_GPU_LAYERS); "cpu" (a game): no layer on the card, a few threads."""
    mode = mode or wanted_mode()
    layers = 0 if mode == "cpu" else _int("UMAY_CODING_GPU_LAYERS", GPU_LAYERS_DEFAULT)
    return [str(exe()), "-m", str(model_path()), "-c", str(context()), "--host", "127.0.0.1", "--port", str(port()),
            "-ngl", str(layers), "-np", "1", "--jinja", "--reasoning", "off", "--alias", SERVED_NAME,
            *(["-t", str(game_threads())] if mode == "cpu" else []),
            *shlex.split(_env("UMAY_CODING_ARGS", DEFAULT_ARGS), posix=False)]


def answers(timeout=1.0):
    try:
        r = requests.get(f"{base()}/health", timeout=timeout)
        return r.ok and r.json().get("status") == "ok"
    except (requests.RequestException, ValueError):
        return False


def _processes():
    """llama-server processes on our port (one an earlier Umay left included)."""
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
        _state["pid"], _state["vram_mb"], _state["mode"] = 0, 0.0, ""
    return False


def on_gpu():
    """Our server runs and has layers on the card (a game must take them back; a CPU-only one is left alone)."""
    return running() and _state["mode"] != "cpu"


def vram_mb():
    return _state["vram_mb"] if on_gpu() else 0.0


def warming():
    return _state["warming"] > 0


def stop(force=False):
    """Ends the server THIS process started (its VRAM and RAM come back). False, nothing done, while a request is in
    flight unless `force`. Only our own process: stop() once killed a server on the port that a test run (another process)
    had nothing to do with, and a task of the spike died of it (2026-11-18); leftovers are `cleanup`'s."""
    with _lock:
        if _state["inflight"] and not force:
            return False
        if _state["pid"]:
            try:
                psutil.Process(_state["pid"]).kill()
            except (psutil.Error, OSError):
                pass
        _state["pid"], _state["vram_mb"], _state["mode"] = 0, 0.0, ""
        return True


def cleanup():
    """Ends a coding server nobody here started (an earlier Umay's, kept alive after it): at Umay's start, and before
    `ensure` starts its own. The card stays free outside coding mode."""
    with _lock:
        for process in _processes():
            if process.pid != _state["pid"]:
                try:
                    process.kill()
                except (psutil.Error, OSError):
                    pass


def _bind_to_umay(process):
    """The server ends with this process, a crash or a kill included: a Windows job object that kills what it holds when
    its last handle closes (the 35B's server outlived Umay and kept 14 GB and the card, 2026-11-18). Best effort: Umay
    inside a job that forbids it just goes without."""
    try:
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class Counters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount",
                "WriteTransferCount", "OtherTransferCount")]

        class Extended(ctypes.Structure):
            _fields_ = [("Basic", Basic), ("Counters", Counters), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        job = kernel32.CreateJobObjectW(None, None)
        info = Extended()
        info.Basic.LimitFlags = 0x2000                       # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not job or not kernel32.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info)):
            return False
        if not kernel32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(int(process._handle))):
            return False
        _state["job"] = job                                  # kept open for as long as this process lives
        return True
    except Exception:
        return False


def _make_room():
    """The card holds Ollama's talker or this model, not both: what Ollama has loaded goes (it loads again on the next
    reply). The big-model backend's server (llama_server) must not be running either."""
    import local_llm   # (local_llm imports this module lazily too)

    if llama_server.running():
        if not llama_server.stop():
            raise requests.ConnectionError("the big local model is answering: coding mode waits")
    for name in {local_llm.model(), local_llm.memory_model()} - {"", None, llama_server.SERVED_NAME}:
        local_llm.unload(name)


def ensure():
    """Makes the server answer, in the mode the moment asks for (`wanted_mode`: on the card, or RAM only in a game); blocks
    up to START_SECONDS. A server of the other mode (or an orphan of an earlier Umay) is ended and started again, unless a
    request is in flight. Raises requests.ConnectionError when it can't: the files are missing, a game started while it
    loaded, or it doesn't come up."""
    mode = wanted_mode()
    with _lock:
        if answers():
            if running() and (_state["mode"] == mode or _state["inflight"]):
                _state["last"] = time.monotonic()
                _watch()
                return
            stop(force=True)       # the other mode, or a server nobody here started
        if not available():
            raise requests.ConnectionError("the coding model or llama-server.exe isn't where the settings say "
                                           "(UMAY_CODING_MODEL, UMAY_CODING_EXE)")
        stop(force=True)
        cleanup()                  # what an earlier Umay left on the port
        if mode == "gpu":
            _make_room()
        before = llama_server._gpu_used_mb()
        home = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Umay"
        home.mkdir(parents=True, exist_ok=True)
        try:
            with open(home / "coding_server.log", "ab") as log:
                log.write(f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} starting ({mode}): "
                          f"{' '.join(command(mode)[3:])}\n".encode("utf-8"))
                log.flush()
                # RAM only in a game: even `-ngl 0` made a CUDA context of ~300 MiB (measured 2026-11-18, 369 -> 669 MiB), and
                # the owner wants no VRAM taken from a game: the process doesn't see the card at all
                env = {**os.environ, "CUDA_VISIBLE_DEVICES": "-1"} if mode == "cpu" else None
                process = subprocess.Popen(command(mode), cwd=str(exe().parent), stdout=log, stderr=subprocess.STDOUT,
                                           stdin=subprocess.DEVNULL, env=env,
                                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as e:
            raise requests.ConnectionError(f"the coding model's server didn't start: {e}") from None
        _state["pid"], _state["mode"] = process.pid, mode
        _bind_to_umay(process)
        deadline = time.monotonic() + START_SECONDS
        while time.monotonic() < deadline and process.poll() is None:
            if mode == "gpu" and llama_server.hold["on"]:
                break                                  # a game started while it loaded: the card is not ours now
            if answers():
                after = llama_server._gpu_used_mb()
                _state["vram_mb"] = 0.0 if mode == "cpu" else (
                    max(1.0, after - before) if before is not None and after is not None else 1.0)
                _state["last"] = time.monotonic()
                _watch()
                return
            time.sleep(0.5)
        stop(force=True)
        raise requests.ConnectionError("the coding model's server didn't answer"
                                       + (" (a game started)" if mode == "gpu" and llama_server.hold["on"] else ""))


def post_chat(body, timeout=600):
    """POSTs an OpenAI-style chat body (starting the server first if needed); the Response. Never ended meanwhile. If the
    connection breaks under it (a game started and the GPU server was stopped, or it crashed) it is asked once more
    from a server started the way the moment asks for."""
    for attempt in (1, 2):
        ensure()
        with _lock:
            _state["inflight"] += 1
        try:
            return requests.post(chat_url(), json={**body, "model": SERVED_NAME, "stream": False}, timeout=timeout)
        except requests.ConnectionError:
            if attempt == 2:
                raise
        finally:
            with _lock:
                _state["inflight"] -= 1
                _state["last"] = time.monotonic()


def warm(prime=None, done=None):
    """The coding window opened: starts the server in the background (and `prime()`, a first small request), so the first
    task doesn't wait for it. Returns at once; `done()` after, however it went. Also in a game (RAM only)."""
    if not available() or warming():
        return
    _state["warming"] += 1

    def run():
        try:
            ensure()
            if prime is not None:
                prime()
        except Exception as e:  # a game, the files, no room: the first task tries again and says why
            print(f"Coding model: warm-up failed: {type(e).__name__}: {e}")
        finally:
            _state["warming"] -= 1
            if done is not None:
                try:
                    done()
                except Exception:
                    pass
    threading.Thread(target=run, daemon=True).start()


def yield_to_reply():
    """A normal reply needs the card (local_llm.working): an idle coding server goes. One serving a task stays: that
    reply then shares the card (slower). A RAM-only server (a game) isn't on the card: it stays."""
    if on_gpu() and not _state["inflight"] and not warming():
        stop()
        return True
    return False


def _watch():
    """One thread that ends the server after UMAY_CODING_IDLE_MINUTES without a request."""
    with _lock:
        if _state["watcher"]:
            return
        _state["watcher"] = True

    def loop():
        while True:
            time.sleep(IDLE_CHECK_SECONDS)
            minutes = idle_minutes()
            if not running():
                continue
            if minutes > 0 and not _state["inflight"] and time.monotonic() - _state["last"] > minutes * 60:
                stop()
    threading.Thread(target=loop, daemon=True).start()
