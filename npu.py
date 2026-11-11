"""Umay's models on the NPU, through FastFlowLM (`flm serve`, https://github.com/FastFlowLM/FastFlowLM).

One server process holds them: a decision model (d3:mini: it only chooses, with a probability for every
option, POST /v1/systemone) and, when their settings name them, a speech and an embedding model. A chat model
is loaded by the first request that asks for one (measured: 8 s), so none is held while nobody uses it.

Why: the memory's yes/no and which-of-these questions. Measured on this PC, 2026-11-10 (development/model_tests):
relation of two facts 96% (the 4b 73%), "is this the fact they called wrong" 100% (the 4b 42%: it said yes to
every related pair), about a second each. A d3 model can't write: a question that needs text stays with the
local model (memory_cortex.ask).

The NPU does one request at a time and its models live in RAM (d3:mini alone: 4.0 GB), not in VRAM.

.env: UMAY_NPU (auto | on | off; auto = on when flm.exe is there), UMAY_NPU_EXE, UMAY_NPU_URL,
UMAY_NPU_DECISION (d3:mini), UMAY_NPU_EMBED (embed-gemma:300m, for tool_pick's shortlist; off: not loaded),
UMAY_NPU_ASR (whisper-v3:turbo, for voice.LocalStt; off: not loaded). All three: about 6.3 GB of RAM.
UMAY_NPU_CHAT (off by default; e.g. qwen3.5:4b): a chat model in a server process of its own, started when
needed and stopped UMAY_NPU_CHAT_IDLE_MINUTES (3) after its last request (chat_model). Its log: npu_chat.log.
Log: %LOCALAPPDATA%\\Umay\\npu.log (the server's own output).
"""
import contextlib
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

import numpy as np
import psutil
import requests

URL_DEFAULT = "http://127.0.0.1:52625"    # not "localhost": Windows tries IPv6 first (see local_llm.py)
START_SECONDS = 90      # the server answers within this long of its start (d3:mini alone: ~6 s)
UP_FOR = 5.0            # an answer to "is it up" is believed this long
OPTION_LINE = "^- {option}:\\s*(.+)$"   # how a prompt says what a choice means ("- SAME: ...")

CHAT_START_SECONDS = 60
CHAT_IDLE_MINUTES = 3   # the learner's questions about one conversation come seconds apart; a game's replies minutes
CHAT_WATCH_SECONDS = 20
CHAT_PREFIX_MB = 512    # FastFlowLM's prompt cache takes up to 4 GB by default; RAM is what this PC runs out of

_state = {"process": None, "up": False, "checked": 0.0, "starting": False, "writing": 0,
          "chat_up": False, "chat_used": 0.0, "watching": False}
_lock = threading.Lock()
_chat_lock = threading.RLock()
on_writing = []   # called (no arguments, must return at once) when the chat model starts to write: voice.py


def _env(name, default=""):
    return os.environ.get(name, "").strip() or default


def exe():
    return Path(_env("UMAY_NPU_EXE") or Path.home() / "Documents" / "Apps" / "FastFlowLM" / "app" / "flm.exe")


def on():
    """The NPU models are wanted: UMAY_NPU=on, or flm.exe is installed and UMAY_NPU isn't off."""
    wanted = _env("UMAY_NPU", "auto").lower()
    return wanted == "on" or (wanted == "auto" and exe().is_file())


def url():
    return _env("UMAY_NPU_URL", URL_DEFAULT).rstrip("/")


def decision_model():
    return _env("UMAY_NPU_DECISION", "d3:mini")


def up(timeout=1.0):
    """The server answers right now (asked, not remembered)."""
    try:
        ok = requests.get(f"{url()}/v1/models", timeout=timeout).ok
    except requests.RequestException:
        ok = False
    _state["up"], _state["checked"] = ok, time.monotonic()
    return ok


def ready():
    """on() and the server answers; asked at most once in UP_FOR seconds, so it costs a caller nothing."""
    if not on():
        return False
    if time.monotonic() - _state["checked"] > UP_FOR:
        up()
    return _state["up"]


def embed_model():
    """The embedding model served next to d3 ("" when UMAY_NPU_EMBED=off): for the tool shortlist (tool_pick)."""
    name = _env("UMAY_NPU_EMBED", "embed-gemma:300m")
    return "" if name.lower() == "off" else name


def asr_model():
    """Whisper on the NPU ("" when UMAY_NPU_ASR=off): voice.LocalStt asks it first. The owner's choice,
    2026-11-11: speech stays on the NPU, in a game too, and the GPU is the talking model's."""
    name = _env("UMAY_NPU_ASR", "whisper-v3:turbo")
    return "" if name.lower() == "off" else name


def asr_ready():
    return bool(asr_model()) and ready()


def transcribe(wav_bytes, prompt="", language="", timeout=30):
    """The text heard in a WAV (POST /v1/audio/transcriptions), "" for silence. Measured 2026-11-11: 1.1-1.9 s
    for a 4 s and a 12 s clip (the GPU worker: 0.33 s a command). Raises when the server doesn't answer."""
    fields = {"model": asr_model(), **({"prompt": prompt} if prompt else {}), **({"language": language} if language else {})}
    r = requests.post(f"{url()}/v1/audio/transcriptions", data=fields,
                      files={"file": ("heard.wav", wav_bytes, "audio/wav")}, timeout=timeout)
    r.raise_for_status()
    return str(r.json().get("text") or "").strip()


def chat_model():
    """The chat model on the NPU ("" unless UMAY_NPU_CHAT names one, e.g. qwen3.5:4b): the memory's questions
    that need text (memory_cortex.ask_json) and, during a game, Umay's replies (ai_assistant).

    It gets a server process of its own, on the port after the main one, started by the first request that needs
    it and stopped CHAT_IDLE (UMAY_NPU_CHAT_IDLE_MINUTES, 3) minutes after the last. FastFlowLM can't unload a
    model, and this one holds 5.5 GB of RAM: measured 2026-11-11, 7.0 GB free with it, 12.5 GB four seconds after
    its process was stopped, the decision model on the main server answering all the while (the two processes
    take turns on the NPU). Up in 6 s, the model's load is 8.6 s more on the first request."""
    name = _env("UMAY_NPU_CHAT", "off")
    return "" if name.lower() == "off" else name


def chat_ready():
    """A chat model is named and the NPU's main server answers: a request to it can be made (it starts its own
    server when needed)."""
    return bool(chat_model()) and ready()


def chat_base():
    host, _, port = url().rpartition(":")
    return _env("UMAY_NPU_CHAT_URL") or f"{host}:{int(port) + 1}"


def chat_url():
    return f"{chat_base()}/v1/chat/completions"


def _chat_answers(timeout=1.0):
    try:
        return requests.get(f"{chat_base()}/v1/models", timeout=timeout).ok
    except requests.RequestException:
        return False


def _chat_processes():
    """The chat server's processes, ours or one an earlier Umay left: flm.exe serving on the chat port."""
    port = chat_base().rsplit(":", 1)[-1]
    found = []
    for process in psutil.process_iter(["name", "cmdline"]):
        try:
            line = process.info["cmdline"] or []
            if (process.info["name"] or "").lower() == "flm.exe" and "serve" in line and port in line:
                found.append(process)
        except (psutil.Error, OSError):
            pass
    return found


def stop_chat():
    """Stops the chat model's server, which gives its RAM back. Quiet when there is none."""
    for process in _chat_processes():
        try:
            process.kill()
        except (psutil.Error, OSError):
            pass
    _state["chat_up"] = False


def _start_chat():
    """Makes the chat model's server answer (blocks up to CHAT_START_SECONDS). Raises ConnectionError when it
    doesn't. One that hangs from before is stopped first."""
    with _chat_lock:
        if _chat_answers():
            _state["chat_up"] = True
            return
        stop_chat()
        home = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Umay"
        home.mkdir(parents=True, exist_ok=True)
        try:
            process = subprocess.Popen(
                [str(exe()), "serve", chat_model(), "--port", chat_base().rsplit(":", 1)[-1],
                 "--prefix-cache-mb", str(CHAT_PREFIX_MB)],
                cwd=str(exe().parent), stdout=open(home / "npu_chat.log", "ab"), stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as e:
            raise requests.ConnectionError(f"the NPU's chat server didn't start: {e}") from None
        deadline = time.monotonic() + CHAT_START_SECONDS
        while time.monotonic() < deadline and process.poll() is None:
            if _chat_answers():
                _state["chat_up"] = True
                _watch_chat()
                return
            time.sleep(0.5)
        stop_chat()
        raise requests.ConnectionError("the NPU's chat server didn't answer")


def chat_idle_seconds():
    try:
        return max(0.5, float(_env("UMAY_NPU_CHAT_IDLE_MINUTES", str(CHAT_IDLE_MINUTES)))) * 60
    except ValueError:
        return CHAT_IDLE_MINUTES * 60


def _watch_chat():
    """One thread that stops the chat server once nothing has asked it for chat_idle_seconds()."""
    if _state["watching"]:
        return
    _state["watching"] = True

    def loop():
        try:
            while _state["chat_up"]:
                time.sleep(CHAT_WATCH_SECONDS)
                with _chat_lock:
                    if not busy() and time.monotonic() - _state["chat_used"] > chat_idle_seconds():
                        stop_chat()
        finally:
            _state["watching"] = False
    threading.Thread(target=loop, name="npu-chat-idle", daemon=True).start()


def post_chat(body, timeout=300):
    """POSTs an OpenAI-style chat body to the chat model's server, starting it first when it isn't up; the
    Response. busy() is true meanwhile. `model` and `think` are set here: without thinking, since Qwen3.5 reasons
    before every answer otherwise."""
    with writing():
        if not (_state["chat_up"] and _chat_answers()):
            _start_chat()
        _state["chat_used"] = time.monotonic()
        try:
            return requests.post(chat_url(), json={**body, "model": chat_model(), "think": False, "stream": False},
                                 timeout=timeout)
        finally:
            _state["chat_used"] = time.monotonic()


def chat(messages, schema=None, max_tokens=1500, timeout=300, temperature=None):
    """The chat model's reply to OpenAI-style messages (text only, no tools): the whole answer JSON of
    POST /v1/chat/completions. `schema`: a JSON schema the reply must fit (enforced by the server since 1.0.10)."""
    body = {"messages": messages, "max_tokens": max_tokens}
    if schema is not None:
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema}}
        body["temperature"] = 0
    elif temperature is not None:
        body["temperature"] = temperature
    r = post_chat(body, timeout)
    r.raise_for_status()
    return r.json()


@contextlib.contextmanager
def writing():
    """Around a request that makes the chat model write: busy() is true meanwhile."""
    with _lock:
        _state["writing"] += 1
        first = _state["writing"] == 1
    if first:
        for told in on_writing:
            try:
                told()
            except Exception:
                pass
    try:
        yield
    finally:
        with _lock:
            _state["writing"] -= 1


def busy():
    """The chat model is writing an answer right now. The NPU does one request at a time, so Whisper and the
    decision model would wait behind it (FastFlowLM's own number: a decision takes 146 ms alone and 12.7 s behind a
    600-token completion): voice.LocalStt lets the GPU's worker hear meanwhile."""
    return _state["writing"] > 0


def ask_json(prompt, schema, timeout=300):
    """One question to the NPU's chat model, answered as JSON under `schema`. Raises ValueError when the reply
    isn't JSON (cut at max_tokens), requests' errors when the server doesn't answer."""
    text = chat([{"role": "user", "content": prompt}], schema, timeout=timeout)["choices"][0]["message"].get("content") or ""
    try:
        answer = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except ValueError:
        raise ValueError(f"no JSON in the answer: {text[:120]!r}") from None
    if not isinstance(answer, dict):
        raise ValueError(f"not an object: {text[:120]!r}")
    return answer


def _command():
    port = url().rsplit(":", 1)[-1]
    command = [str(exe()), "serve", "-d", decision_model(), "--port", port]
    for flag, name in (("-a", asr_model()), ("-e", embed_model())):
        if name:
            command += [flag, name]
    return command


def start():
    """Starts the server if it is wanted and not already answering (one started by hand is used as it is).
    Returns whether it answers. Blocks up to START_SECONDS: call it from a background thread."""
    if not on():
        return False
    with _lock:
        if up() or _state["starting"]:
            return _state["up"]
        _state["starting"] = True
    try:
        home = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Umay"
        home.mkdir(parents=True, exist_ok=True)
        log = open(home / "npu.log", "ab")
        _state["process"] = subprocess.Popen(
            _command(), cwd=str(exe().parent), stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        deadline = time.monotonic() + START_SECONDS
        while time.monotonic() < deadline and _state["process"].poll() is None:
            if up():
                return True
            time.sleep(1)
        return up()
    except OSError:
        return False
    finally:
        _state["starting"] = False


def stop():
    """Stops the server this Umay started (never one started by hand)."""
    process, _state["process"] = _state["process"], None
    if process and process.poll() is None:
        process.terminate()
    stop_chat()
    _state["up"], _state["checked"] = False, 0.0


def decide(state, questions, timeout=60):
    """Typed answers to questions about a text: POST /v1/systemone. questions: {id: {"type": "noul",
    "instructions": ...} | {"type": "choice", "instructions": ..., "criteria": {option: what it means}}}.
    Returns {id: {"type": ..., "noul": P(yes)} | {"choice": ..., "confidence": ..., "probabilities": {...}}}."""
    r = requests.post(f"{url()}/v1/systemone", json={"state": state, "questions": questions}, timeout=timeout)
    r.raise_for_status()
    return r.json().get("answers") or {}


def embed(texts, timeout=60):
    """Unit vectors for the texts, one row each (POST /v1/embeddings). Raises when there is no embedding model
    or the server doesn't answer."""
    if not embed_model():
        raise ValueError("no embedding model on the NPU (UMAY_NPU_EMBED=off)")
    r = requests.post(f"{url()}/v1/embeddings", json={"model": embed_model(), "input": list(texts)}, timeout=timeout)
    r.raise_for_status()
    vectors = np.array([d["embedding"] for d in r.json()["data"]], dtype=np.float32)
    return vectors / np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-9)


def choice_only(schema):
    """True when every answer the schema asks for is a yes/no or one of a list: what a decision model can give."""
    props = (schema or {}).get("properties") or {}
    return bool(props) and all(p.get("type") == "boolean" or p.get("enum") for p in props.values())


def _meaning(prompt, option):
    """What the prompt itself says an option means: its "- OPTION: ..." line, else the option's name."""
    m = re.search(OPTION_LINE.format(option=re.escape(str(option))), prompt, re.MULTILINE)
    return m.group(1).strip()[:300] if m else str(option)


def ask_choice(prompt, schema, timeout=60):
    """The answer to a choice-only JSON schema (choice_only), from the decision model: the prompt goes in as
    the text, each boolean as a yes/no question, each enum as a choice among its options. Same shape as
    memory_cortex.ask_json's answer. Raises ValueError for a schema that needs text, or an answer that isn't
    there; requests' errors when the server doesn't answer."""
    if not choice_only(schema):
        raise ValueError("a decision model can't write text")
    questions = {}
    for key, spec in schema["properties"].items():
        if spec.get("type") == "boolean":
            questions[key] = {"type": "noul", "instructions": f"Answer the question the state asks ({key.replace('_', ' ')})."}
        else:
            questions[key] = {"type": "choice", "instructions": "Choose the answer to the question the state asks.",
                              "criteria": {str(o): _meaning(prompt, o) for o in spec["enum"]}}
    answers = decide(prompt, questions, timeout)
    out = {}
    for key, question in questions.items():
        answer = answers.get(key) or {}
        if question["type"] == "noul":
            if not isinstance(answer.get("noul"), (int, float)):
                raise ValueError(f"no answer for {key}: {answer}")
            out[key] = answer["noul"] >= 0.5   # "noul" is how likely yes is
        else:
            if answer.get("choice") not in question["criteria"]:
                raise ValueError(f"no answer for {key}: {answer}")
            out[key] = answer["choice"]
    return out
