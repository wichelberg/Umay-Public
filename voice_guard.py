"""Keeps Umay's voice models ready without getting in a game's way. The PC's health comes first.

Every CHECK_SECONDS (BATTERY_CHECK_SECONDS on battery) it reads the NVIDIA GPU (nvidia-smi) and picks a mode:
- game: something else keeps the GPU busy (>= UMAY_TTS_GPU_BUSY %, default 40, twice in a row),
  the GPU is at UMAY_HEALTH_GPU_TEMP (default 87 °C) or hotter, or its memory is 95% full.
  While our own models work (loading, answering, 5 s after) the load counts as ours, unless the VRAM
  beyond our models (OTHERS_GAME_MB) shows a big program too. Models load off the guard's thread, so
  readings go on while they load. Whisper, Chatterbox and the voice pack stop at once, even half
  loaded, and nothing local runs (the owner types
  during games): replies stay as text, and a voice note that still comes in goes to OpenRouter.
  The local model (local_llm.py) goes second, only if the VRAM is still over UMAY_GAME_VRAM_LIMIT_MB
  (default 7000) without the voice models: a loaded but idle model costs a game nothing but VRAM (see
  below), so a light game keeps it and replies stay quick; a heavier one gets all the VRAM. It is
  looked at on every reading of the game (a new level takes more), and a reply that loads it again
  during a game is let finish first. When the NPU serves a chat model and the local model only talks
  (UMAY_NPU_CHAT with UMAY_TOOL_CHOICE=npu), replies come from the NPU during a game (ai_assistant._on_npu)
  and the local model is unloaded at once: the game gets all the VRAM.
- ready: the GPU has been calm for a while (30 s after Umay starts, 2 minutes after a game),
  is cool, has room for the models, and the PC is on mains power. Whisper, the TTS (Piper on the
  CPU, or Chatterbox) and the local model are loaded ahead and kept loaded, so a reply starts fast;
  the voice pack loads when 🎤 is pressed (on the GPU only with UMAY_RVC_DEVICE=gpu or without the
  local model: RVC_GPU_MB). Only in this mode does wake.py listen for "Hey Umay" (it is
  told through `listeners`, and closes the microphone at the first busy reading). After
  UMAY_LOCAL_IDLE_MINUTES (5) without a reply (local_llm.dormant) it rests: nothing is loaded ahead, the
  local model is unloaded (_rest) and the next reply loads it; wake.py still listens.
- idle: on battery, or UMAY_VOICE_PRELOAD=off. Nothing is loaded ahead; models load when
  needed and unload after their idle time (a loaded model keeps a laptop's NVIDIA GPU awake).

Measured on the RTX 5070 Laptop: loaded but idle models changed neither power (11.3 W vs 11.4 W),
temperature nor clocks; what they cost is ~5 GB of VRAM, which a game may need (it then stutters).
Mode changes are written to voice_home/voice_guard.log.
"""
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime

import psutil

import local_llm
import npu
import paths
import voice

CHECK_SECONDS = 5            # on mains power: a game is noticed in ~10 s
BATTERY_CHECK_SECONDS = 30    # nvidia-smi wakes a sleeping laptop GPU: rarely on battery
BUSY_HITS = 2                 # readings in a row
FIRST_CALM_SECONDS = 30       # after Umay starts
CALM_SECONDS = 2 * 60         # after a game
MEMORY_FULL = 0.95            # share of VRAM: step aside
MEMORY_ROOM = 0.90            # load only if the models fit under this share
COOL_MARGIN = 5               # °C below the temperature limit to load again
# What each loaded model takes (measured: Whisper 1.22 GB, Chatterbox 2.73 GB with T3 in bf16, the local
# qwen3.5:4b 3.7 GB at 4096 tokens: "llm" leaves room for its larger window). Piper runs on the CPU.
NEED_MB = {"stt": 1300, "tts": 2900, "llm": 4000}
# The voice pack on the GPU (measured: up to ~2 GB). Not loaded ahead, so only ~4 GB stay loaded while
# the owner isn't talking; with all three the 8 GB card is ~81% full (measured 6.58 GB; 93.7% before
# Chatterbox's T3 went to bf16). It loads when 🎤 is pressed (~3 s, while the owner talks) and unloads
# after UMAY_RVC_IDLE_MINUTES (default 3). If the VRAM gets full while it is on the GPU, it moves to
# the CPU before anything else (_make_room).
RVC_GPU_MB = 2100
# VRAM used by others (all of it minus our models) that means a big program: the desktop, browser and
# overlays take 0.5-1.2 GB here (measured in voice_guard.log), a game 3 GB and more.
OTHERS_GAME_MB = 2500
# In a game, the VRAM above which the local model is unloaded too, after the voice models (the owner's
# rule, 2026-11-02: Whisper first; if it is still tight, the local model as well).
GAME_VRAM_LIMIT_MB = 7000


@dataclass
class Reading:
    util: float      # %
    mem_used: float  # MiB
    mem_total: float
    temp: float      # °C


@dataclass
class State:
    mode: str = "idle"
    hits: int = 0
    calm_since: float = None
    calm_needed: float = FIRST_CALM_SECONDS


def _setting(name, default):
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def need_mb(tts_on_gpu=True, llm_local=False, llm_mb=0, stt=True):
    """VRAM the models loaded ahead take together. llm_mb: what the local model takes if it is loaded already:
    that, not NEED_MB's guess (2026-11-03 qwen3.5:4b took 3481 MB of the 4000 guessed, and those 519 MB kept
    the voice from ever loading with Blender open). stt=False: Whisper on the NPU hears (npu.asr_ready), so the
    GPU's worker isn't loaded ahead and needs no room.""" 
    llm = (llm_mb or NEED_MB["llm"]) if llm_local else 0
    return (NEED_MB["stt"] if stt else 0) + (NEED_MB["tts"] if tts_on_gpu else 0) + llm


def llm_must_go(reading, voice_mb, limit=None):
    """In a game: the VRAM without the voice models being stopped (voice_mb) is still over the limit, so
    the local model goes too. No reading: it goes (nothing to judge by, the game comes first)."""
    limit = _setting("UMAY_GAME_VRAM_LIMIT_MB", GAME_VRAM_LIMIT_MB) if limit is None else limit
    return reading is None or reading.mem_used - voice_mb > limit


def step(state, reading, now, plugged, ours_busy, ours_mb, preload=True, need=None):
    """The mode for this reading (pure: no GPU, no workers). ours_busy: a model is working right now,
    so the GPU load is partly ours; ours_mb: VRAM our loaded models take; need: VRAM the models loaded
    ahead take together (default: Whisper and Chatterbox)."""
    if reading is None:  # no nvidia-smi: nothing to judge by
        return state.mode
    busy_limit = _setting("UMAY_TTS_GPU_BUSY", 40)
    temp_limit = _setting("UMAY_HEALTH_GPU_TEMP", 87)
    # While a model of ours works the load is taken as ours, unless the VRAM beyond our models shows
    # another big program (a game starting while ours load): then it counts all the same.
    ours_only = ours_busy and reading.mem_used - ours_mb < OTHERS_GAME_MB
    others_busy = busy_limit > 0 and not ours_only and reading.util >= busy_limit
    state.hits = state.hits + 1 if others_busy else 0
    if state.hits >= BUSY_HITS or reading.temp >= temp_limit or reading.mem_used >= MEMORY_FULL * reading.mem_total:
        if state.mode != "game":
            state.calm_needed = CALM_SECONDS
        state.mode, state.calm_since = "game", None
        return state.mode

    room = MEMORY_ROOM * reading.mem_total - (reading.mem_used - ours_mb)
    need = need_mb() if need is None else need
    calm = not others_busy and reading.temp < temp_limit - COOL_MARGIN and room >= need
    if not calm:
        state.calm_since = None
    else:
        state.calm_since = state.calm_since if state.calm_since is not None else now
        if now - state.calm_since >= state.calm_needed:
            state.mode = "ready" if plugged and preload else "idle"
    if state.mode == "ready" and not (plugged and preload):
        state.mode = "idle"
    return state.mode


# --- the running guard ----------------------------------------------------------------------

_state = State()
_started = {"on": False}
# Called with (mode, suspect) after every reading; suspect: one busy reading already, a game may be
# starting (wake.py closes the microphone then, without waiting for the second one).
listeners = []


def _read_gpu():
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10,
                             creationflags=subprocess.CREATE_NO_WINDOW).stdout
        util, used, total, temp = (float(v) for v in out.splitlines()[0].split(","))
        return Reading(util, used, total, temp)
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def _plugged():
    battery = psutil.sensors_battery()
    return battery is None or bool(battery.power_plugged)


def _rvc_on_gpu():
    return voice.voice_pack.loaded() and voice.voice_pack.device == "gpu"


def _make_room(reading):
    """VRAM full while the voice pack is on the GPU: it goes to the CPU first (frees ~2 GB at once,
    and it is the cheapest to bring back). Returns the reading to judge by."""
    if reading is None or reading.mem_used < MEMORY_FULL * reading.mem_total or not _rvc_on_gpu():
        return reading
    voice.voice_pack.off_the_gpu()
    _log(f"VRAM full: voice pack moved to the CPU for {voice.VoicePack.CPU_MINUTES} min ({reading})")
    return _read_gpu() or reading


def _voice_mb():
    """VRAM the voice models take: Whisper, Chatterbox and the voice pack on the GPU."""
    tts_mb = NEED_MB["tts"] if voice.local_tts.loaded() and voice.local_tts.on_gpu() else 0
    return (NEED_MB["stt"] if voice.local_stt.loaded() else 0) + tts_mb + (RVC_GPU_MB if _rvc_on_gpu() else 0)


def _ours_mb(llm_mb):
    return _voice_mb() + llm_mb


def _ours_busy():
    return voice.workers_busy() or (local_llm.on() and local_llm.busy())


LLM_RETRY_SECONDS = 60  # after a failed load (Ollama not running): not every reading
_llm_loading = {"on": False, "retry_at": 0.0}


def _load_llm():
    """Loads the local model ahead (off the guard's thread; one load at a time)."""
    if _llm_loading["on"] or time.monotonic() < _llm_loading["retry_at"]:
        return
    _llm_loading["on"] = True

    def load():
        try:
            local_llm.load()
        except Exception:
            _llm_loading["retry_at"] = time.monotonic() + LLM_RETRY_SECONDS
            raise
        finally:
            _llm_loading["on"] = False
    _in_background(load)


def _log(text):
    try:
        with open(paths.voice_home() / "voice_guard.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}\n")
    except OSError:
        pass


def apply(mode, reading=None):
    """Puts the workers in line with `mode` (`reading`: the GPU reading it came from)."""
    voice.gaming["on"] = mode == "game"
    asleep = mode == "ready" and local_llm.on() and local_llm.dormant()
    keep = mode == "ready" and not asleep
    voice.local_tts.keep_loaded = keep
    voice.local_stt.keep_loaded = keep and not npu.asr_ready()  # only a stand-in while the NPU hears
    _memory_model_away(mode, mode != "game" and local_llm.dormant())  # on battery (idle) too: resting is resting
    if mode == "game":  # the voice models at once, even one still loading: the game comes first
        voice_mb = _voice_mb()
        voice.local_tts.stop(at_once=True)
        voice.local_stt.stop(at_once=True)
        voice.voice_pack.stop(at_once=True)
        llm_mb = local_llm.vram_mb() if local_llm.on() else 0
        moved = local_llm.picks() and npu.chat_ready()  # her replies come from the NPU's chat model in a game
        if llm_mb and not local_llm.busy() and (moved or llm_must_go(reading, voice_mb)):
            local_llm.unload()
            _log(f"game: {'replies move to the NPU' if moved else 'VRAM still over the limit without the voice models'}"
                 f": local model unloaded ({llm_mb:.0f} MB, {reading})")
    elif voice.voice_pack.stale():  # on the CPU after a full VRAM: it restarts on the GPU when needed
        voice.voice_pack.stop()
    elif asleep:
        _rest()
    elif mode == "ready":
        for worker, on in ((voice.local_stt, voice.stt_on() and not npu.asr_ready()),
                           (voice.local_tts, voice.tts_on())):
            if on and not worker.loaded() and not worker.busy():
                _in_background(worker.start)
        if local_llm.on() and not local_llm.busy() and not local_llm.vram_mb():
            _load_llm()


def _memory_model_away(mode, resting):
    """The memory's own model (local_llm.memory_model) has the GPU only while she rests: the owner back (a reply
    since) or a game, and it goes, once its request is done, so the talker or the game gets the VRAM."""
    memory = local_llm.memory_model()
    if not memory or resting or local_llm.busy():
        return
    memory_mb = local_llm.vram_mb(memory)
    if memory_mb:
        local_llm.unload(memory)
        _log(f"{'game' if mode == 'game' else 'the owner is back'}: memory model unloaded ({memory_mb:.0f} MB)")


def _rest():
    """Nobody was answered for local_llm.idle_minutes(): nothing is loaded ahead (the voice workers unload after
    their own idle time) and the local model goes now (~6 GB of VRAM and RAM). "Hey Umay" still works: the NPU
    hears it. The next reply loads the model again; memory work that loads it lets it go again afterwards."""
    llm_mb = local_llm.vram_mb()
    if llm_mb and not local_llm.busy():
        local_llm.unload()
        _log(f"no reply for {local_llm.idle_minutes():g} min: local model unloaded ({llm_mb:.0f} MB)")


def _in_background(load):
    """Loads a model off the guard's thread: a cold load takes up to minutes (2026-10-20, a game
    starting meanwhile: 2 min 20 s), and the guard must keep reading the GPU every few seconds."""
    def run():
        try:
            load()
        except Exception as e:  # stopped for a game, or broken: the next "ready" reading tries again
            _log(f"loading failed: {type(e).__name__}: {e}")
    threading.Thread(target=run, daemon=True).start()


_waiting = {"said": False}


def _say_waiting(mode, reading, ours, need):
    """Once a wait: the voice can't load for want of VRAM (other programs use it), in voice_guard.log."""
    room = MEMORY_ROOM * reading.mem_total - (reading.mem_used - ours) if reading is not None else None
    short = mode == "idle" and room is not None and room < need
    if short and not _waiting["said"]:
        _log(f"waiting for VRAM: {room:.0f} MB room for the {need:.0f} MB the voice and the local model need "
             f"({reading})")
    _waiting["said"] = short


def _loop():
    preload = os.environ.get("UMAY_VOICE_PRELOAD", "").strip().lower() != "off"
    while True:
        plugged = True
        try:
            before = _state.mode
            reading = _make_room(_read_gpu())
            plugged = _plugged()
            llm_mb = local_llm.vram_mb() if local_llm.on() else 0
            memory = local_llm.memory_model()
            ours = _ours_mb(llm_mb) + (local_llm.vram_mb(memory) if memory else 0)  # the memory's model is ours too
            need = need_mb(voice.local_tts.on_gpu(), local_llm.on(), llm_mb, stt=not npu.asr_ready())
            mode = step(_state, reading, time.monotonic(), plugged, _ours_busy(), ours, preload, need)
            if mode != before:
                _log(f"{before} -> {mode} ({reading})")
            _say_waiting(mode, reading, ours, need)
            apply(mode, reading)
            for listener in listeners:
                listener(mode, _state.hits > 0)
        except Exception as e:  # never let the guard die: the voice would stay as it is
            _log(f"error: {type(e).__name__}: {e}")
        time.sleep(CHECK_SECONDS if plugged else BATTERY_CHECK_SECONDS)


def start():
    """Starts guarding in the background (main.py). Does nothing twice."""
    if _started["on"]:
        return
    _started["on"] = True
    threading.Thread(target=_loop, daemon=True).start()
