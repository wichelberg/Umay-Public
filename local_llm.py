"""Umay's local model: Qwen on this PC through Ollama (https://ollama.com), free and offline.

ai_assistant.py talks to it through Ollama's OpenAI-compatible endpoint, the same request format it
uses for OpenRouter. Ollama's default context (4096 tokens) is too small for Umay: her tool list alone
is ~28,000 characters (~8,000 tokens), so ensure_model() makes UMAY_LOCAL_MODEL once from
UMAY_LOCAL_BASE_MODEL with a CONTEXT-token window (it only writes a manifest: nothing is loaded).
voice_guard.py loads the model ahead while the GPU is calm, counts its VRAM as Umay's own, and
unloads it for a game.

Measured on the RTX 5070 Laptop with qwen3.5:4b (Q4_K_M): 3.7 GB of VRAM at 4096 tokens, a tool call
in ~0.6 s and a reply after a tool in ~0.4-0.9 s.

.env: UMAY_LLM (local | openrouter), UMAY_LOCAL_MODEL, UMAY_LOCAL_BASE_MODEL, UMAY_LOCAL_CONTEXT,
UMAY_TOOL_CHOICE (npu: picks()), UMAY_LOCAL_VISION (off: sees()), UMAY_LOCAL_TEMPLATE (llama3: TEMPLATES),
UMAY_LOCAL_IDLE_MINUTES (5; dormant(): unloaded after that long without a reply, 0 = kept),
UMAY_MEMORY_MODEL (memory_model(): the memory's own model on the GPU while she rests).
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
# Nobody answered this long: voice_guard unloads the model and loads nothing ahead (dormant). The owner's rule,
# 2026-11-11: RAM and VRAM aren't held for a reply nobody asks for; the next reply loads it again (~5-10 s).
IDLE_MINUTES = 5   # the owner, 2026-11-11: as long as the PC takes to count them away (companion)


def _env(name, default):
    """Read at use time: Umay loads .env after this module is imported."""
    return os.environ.get(name, "").strip() or default


def on():
    """Umay's replies come from the local model (UMAY_LLM=openrouter: from OpenRouter)."""
    return _env("UMAY_LLM", "local").lower() != "openrouter"


def picks():
    """UMAY_TOOL_CHOICE=npu: the NPU's decision model names each tool (tool_pick.choose) and this model only writes
    the tool's arguments and speaks (ai_assistant._picked_loop). So it needs no tool calling of its own: a
    roleplay model can be Umay's voice (the owner's choice 2026-11-11: L3-8B-Stheno)."""
    return on() and _env("UMAY_TOOL_CHOICE", "").lower() == "npu"


def sees():
    """The model takes images (qwen3.5 does). UMAY_LOCAL_VISION=off for a text-only one: a screenshot is then
    put into words for it by the heavy model (ai_assistant._seen)."""
    return _env("UMAY_LOCAL_VISION", "on").lower() != "off"


def base_model():
    return _env("UMAY_LOCAL_BASE_MODEL", "qwen3.5:4b")


def context():
    return int(_env("UMAY_LOCAL_CONTEXT", "16384"))


def model():
    return _env("UMAY_LOCAL_MODEL", "umay-" + base_model().replace(":", "-"))


_made = {"model": None}
_state = {"working": 0, "last": 0.0, "answering": 0, "answered": 0.0, "started": time.monotonic()}
_lock = threading.Lock()


# Chat templates Ollama gets wrong by itself (UMAY_LOCAL_TEMPLATE names one). A GGUF pulled from Hugging Face
# gets ChatML when Ollama doesn't know its own template: L3-8B-Stheno, a Llama 3 model, was served with
# <|im_start|> tags it was never trained on (2026-11-11).
TEMPLATES = {"llama3": {
    "template": ("{{ if .System }}<|start_header_id|>system<|end_header_id|>\n\n{{ .System }}<|eot_id|>{{ end }}"
                 "{{ if .Prompt }}<|start_header_id|>user<|end_header_id|>\n\n{{ .Prompt }}<|eot_id|>{{ end }}"
                 "<|start_header_id|>assistant<|end_header_id|>\n\n{{ .Response }}<|eot_id|>"),
    "stop": ["<|start_header_id|>", "<|end_header_id|>", "<|eot_id|>"]}}


def _creation():
    """What /api/create is asked for model(): the base model, the window and, if named, the chat template."""
    known = TEMPLATES.get(_env("UMAY_LOCAL_TEMPLATE", "").lower())
    return {"model": model(), "from": base_model(), "stream": False,
            "parameters": {"num_ctx": context(), **({"stop": known["stop"]} if known else {})},
            **({"template": known["template"]} if known else {})}


def ensure_model():
    """Makes model() from base_model() with a context() window if Ollama doesn't have it yet. Raises
    requests' ConnectionError when Ollama isn't running, HTTPError when the base model isn't pulled."""
    name = model()
    if _made["model"] == name:
        return
    tags = requests.get(f"{BASE_URL}/api/tags", timeout=5)
    tags.raise_for_status()
    if not any(m.get("name") in (name, f"{name}:latest") for m in tags.json().get("models", [])):
        made = requests.post(f"{BASE_URL}/api/create", json=_creation(), timeout=120)
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


def idle_minutes():
    """UMAY_LOCAL_IDLE_MINUTES (IDLE_MINUTES): no reply for this long makes the model dormant(); 0 = never."""
    try:
        return max(0.0, float(_env("UMAY_LOCAL_IDLE_MINUTES", str(IDLE_MINUTES))))
    except ValueError:
        return IDLE_MINUTES


def dormant():
    """No reply to the owner for idle_minutes() (counted from Umay's start before the first one): voice_guard then
    lets the model go. Memory work (background) doesn't wake it."""
    minutes = idle_minutes()
    last = max(_state["answered"], _state["started"])
    return minutes > 0 and not answering() and time.monotonic() - last > minutes * 60


def quiet():
    """Background work that loads models of its own (the memory's reading) may run: no reply for idle_minutes()
    (dormant), so it doesn't stack on the talker and the voice. 2026-11-11: the memory's work right after each talk
    (a second NPU server, 5.4 GB, and Whisper's stand-in, 2.6 GB) took the RAM to 97%. idle_minutes() 0: whenever
    no reply is being made, as before."""
    return not answering() and (idle_minutes() == 0 or dormant())


def memory_model():
    """UMAY_MEMORY_MODEL: the Ollama model the memory's text work uses (memory_cortex.ask_json,
    conversation_memory.ask_local). The owner's plan, 2026-11-11: in use, the talker has the VRAM; while she rests
    (quiet: when the memory works) the talker goes and the model best at the memory takes the GPU instead, so
    nothing of it sits in RAM (the NPU's chat server took 5.4 GB of RAM for it). voice_guard lets it go when the
    owner is back or a game starts. Empty: the NPU's chat model (UMAY_NPU_CHAT) or the talker, as before."""
    return _env("UMAY_MEMORY_MODEL", "")


def vram_mb(name=None):
    """VRAM a loaded model takes, model() unless named (0 when it isn't loaded or Ollama doesn't answer)."""
    try:
        loaded = requests.get(f"{BASE_URL}/api/ps", timeout=2).json().get("models", [])
    except (requests.RequestException, ValueError):
        return 0
    name = name or model()
    return sum(m.get("size_vram", 0) for m in loaded if m.get("name") in (name, f"{name}:latest")) / 2 ** 20


def load():
    """Loads the model and keeps it KEEP_ALIVE (a reply resets that to Ollama's default, 5 minutes)."""
    ensure_model()
    requests.post(f"{BASE_URL}/api/generate", json={"model": model(), "keep_alive": KEEP_ALIVE},
                  timeout=180).raise_for_status()


def unload(name=None):
    """Frees a model's VRAM now, model() unless named (a game started, she rests). Quiet if Ollama isn't running."""
    try:
        requests.post(f"{BASE_URL}/api/generate", json={"model": name or model(), "keep_alive": 0}, timeout=10)
    except requests.RequestException:
        pass
