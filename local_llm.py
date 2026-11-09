"""Umay's local model: Qwen on this PC through Ollama (https://ollama.com), free and offline.

ai_assistant.py talks to it through Ollama's OpenAI-compatible endpoint, the same request format it
uses for OpenRouter. Ollama's default context (4096 tokens) is too small for Umay: her tool list alone
is ~28,000 characters (~8,000 tokens), so ensure_model() makes UMAY_LOCAL_MODEL once from
UMAY_LOCAL_BASE_MODEL with a CONTEXT-token window (it only writes a manifest: nothing is loaded).
voice_guard.py loads the model ahead while the GPU is calm, counts its VRAM as Umay's own, and
unloads it for a game.

Measured on the RTX 5070 Laptop with qwen3.5:4b (Q4_K_M): 3.7 GB of VRAM at 4096 tokens, a tool call
in ~0.6 s and a reply after a tool in ~0.4-0.9 s.

.env: UMAY_LLM (local | openrouter), UMAY_LOCAL_MODEL, UMAY_LOCAL_BASE_MODEL, UMAY_LOCAL_CONTEXT.
"""
import contextlib
import os
import threading
import time

import requests

# Not "localhost": on Windows that tries IPv6 (::1) first, where Ollama doesn't listen, and every
# request then waits ~2 s before falling back to IPv4 (measured: 2.4 s vs 0.4 s for the same reply).
BASE_URL = "http://127.0.0.1:11434"
CHAT_URL = f"{BASE_URL}/v1/chat/completions"
BUSY_SECONDS = 5  # like voice._Worker: the GPU load right after a reply is still partly ours
KEEP_ALIVE = "30m"


def _env(name, default):
    """Read at use time: Umay loads .env after this module is imported."""
    return os.environ.get(name, "").strip() or default


def on():
    """Umay's replies come from the local model (UMAY_LLM=openrouter: from OpenRouter)."""
    return _env("UMAY_LLM", "local").lower() != "openrouter"


def base_model():
    return _env("UMAY_LOCAL_BASE_MODEL", "qwen3.5:4b")


def context():
    return int(_env("UMAY_LOCAL_CONTEXT", "16384"))


def model():
    return _env("UMAY_LOCAL_MODEL", "umay-" + base_model().replace(":", "-"))


_made = {"model": None}
_state = {"working": 0, "last": 0.0, "answering": 0, "answered": 0.0}
_lock = threading.Lock()


def ensure_model():
    """Makes model() from base_model() with a context() window if Ollama doesn't have it yet. Raises
    requests' ConnectionError when Ollama isn't running, HTTPError when the base model isn't pulled."""
    name = model()
    if _made["model"] == name:
        return
    tags = requests.get(f"{BASE_URL}/api/tags", timeout=5)
    tags.raise_for_status()
    if not any(m.get("name") in (name, f"{name}:latest") for m in tags.json().get("models", [])):
        made = requests.post(f"{BASE_URL}/api/create", json={
            "model": name, "from": base_model(), "parameters": {"num_ctx": context()}, "stream": False,
        }, timeout=120)
        made.raise_for_status()
    _made["model"] = name


@contextlib.contextmanager
def working(background=False):
    """Around a request to the local model: busy() is true meanwhile and for BUSY_SECONDS after. A reply to the
    owner (background=False) makes answering() true too; memory work (background=True) doesn't, so it doesn't
    stop itself: waiting on its own requests, the memory learnt one log per 5 minutes (2026-11-07)."""
    with _lock:
        _state["working"] += 1
        if not background:
            _state["answering"] += 1
    try:
        yield
    finally:
        with _lock:
            _state["working"] -= 1
            _state["last"] = time.monotonic()
            if not background:
                _state["answering"] -= 1
                _state["answered"] = _state["last"]


def busy():
    """The GPU is Umay's own right now (any request, memory work too): for voice_guard."""
    return _state["working"] > 0 or time.monotonic() - _state["last"] < BUSY_SECONDS


def answering():
    """A reply to the owner is being made (not memory work): background memory jobs wait for this."""
    return _state["answering"] > 0 or time.monotonic() - _state["answered"] < BUSY_SECONDS


def vram_mb():
    """VRAM the loaded model takes (0 when it isn't loaded or Ollama doesn't answer)."""
    try:
        loaded = requests.get(f"{BASE_URL}/api/ps", timeout=2).json().get("models", [])
    except (requests.RequestException, ValueError):
        return 0
    name = model()
    return sum(m.get("size_vram", 0) for m in loaded if m.get("name") in (name, f"{name}:latest")) / 2 ** 20


def load():
    """Loads the model and keeps it KEEP_ALIVE (a reply resets that to Ollama's default, 5 minutes)."""
    ensure_model()
    requests.post(f"{BASE_URL}/api/generate", json={"model": model(), "keep_alive": KEEP_ALIVE},
                  timeout=180).raise_for_status()


def unload():
    """Frees the model's VRAM now (a game started). Quiet if Ollama isn't running."""
    try:
        requests.post(f"{BASE_URL}/api/generate", json={"model": model(), "keep_alive": 0}, timeout=10)
    except requests.RequestException:
        pass
