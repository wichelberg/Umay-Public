"""Converts speech into Umay's voice. Runs inside Wichelberg's Python, not Umay's.

Wichelberg (the user's voice changer) has the RVC voice packs, ONNX Runtime and
the conversion code; this script reuses its offline AI path (render_ai: high-pass,
gate, auto level, RVC with automatic pitch shift to the voice's target, silence
mask) instead of duplicating it. Started and kept alive by voice.py.

On "gpu" it runs on the GPU with the most VRAM through DirectML, but only if that GPU's output
matches the CPU's by Wichelberg's parity rule (voicechanger.ai.parity, docs/DECISIONS.md D9/D21):
a GPU that Wichelberg's own benchmark marked as different is never used, and the check is run
here once per GPU, ONNX Runtime version and voice pack (cached in <parity file>). Otherwise, or
if DirectML fails later, it runs on the CPU. DirectML's memory grows with the longest input it has
seen (measured on the RTX 5070 Laptop: +1.1 GB at 3 s, +1.8 GB at 6 s, +3.2 GB at 15 s, +6.4 GB at
30 s), so only audio up to GPU_MAX_SECONDS goes to the GPU; longer audio
is converted on the CPU, with the same result.

argv: <wichelberg _wichelberg dir> <voice pack folder, or a voice id in Wichelberg's library>
      [cpu | gpu, default cpu] [parity file]
stdin: one JSON request per line: {"in": audio path, "wav": out path, "ogg": out path or null}
stdout: {"ready": true, "device": "gpu" | "cpu", "seconds": ..., "note": ...} once loaded, then one JSON
        reply per request: {"ok": true, "seconds": ..., "device": ...} or {"ok": false, "error": "..."}
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

SAMPLE_RATE = 48000  # Wichelberg's engines take and return 48 kHz mono float32
PARITY_SECONDS = 4.0
# Keeps the GPU worker under ~2 GB of VRAM. 4.5 s (~1.5 GB) was measured too: the first piece is
# often 4-6 s, and on the CPU the first sound came 2-5 s later.
GPU_MAX_SECONDS = 8.0


def _wichelberg_says_different(wichelberg, device_id):
    """Wichelberg's benchmark found this GPU's output different from the CPU's."""
    try:
        config = json.loads((wichelberg / "config" / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    gpus = ((config.get("ai") or {}).get("benchmark") or {}).get("gpus") or []
    return any(g.get("device_id") == device_id and g.get("parity_ok") is False for g in gpus)


def _gpu_model(wichelberg, voice, parity_file, convert):
    """(RvcModel on the GPU or None, why not / the parity result)."""
    import onnxruntime as ort
    from voicechanger.ai import parity
    from voicechanger.ai.rvc_onnx import RvcModel
    from voicechanger.ai.runtime import GPU, Runtime, RuntimeConfig, default_gpu, gpu_supported
    from voicechanger.ai.testsignal import speech_like

    gpu = default_gpu()
    if not gpu_supported() or gpu is None:
        return None, "no DirectML GPU"
    if _wichelberg_says_different(wichelberg, gpu.device_id):
        return None, f"{gpu.name}: Wichelberg's parity test failed"
    model = RvcModel(voice, Runtime(RuntimeConfig(accel=GPU, gpu_device_id=gpu.device_id)), "fcpe")
    if model.runtime.fallback_reason:
        return None, model.runtime.fallback_reason
    key = f"{gpu.name} | onnxruntime {ort.__version__} | {voice.id} {voice.version}"
    try:
        known = json.loads(parity_file.read_text(encoding="utf-8")) if parity_file else {}
    except (OSError, ValueError):
        known = {}
    if key not in known:
        signal = speech_like(PARITY_SECONDS, SAMPLE_RATE).astype("float32")
        cpu = RvcModel(voice, Runtime(), "fcpe")
        result = parity.compare(convert(cpu, signal), convert(model, signal))
        cpu.close()
        known[key] = {"ok": result.ok, "describe": result.describe(), "date": f"{datetime.now():%Y-%m-%d %H:%M}"}
        if parity_file:
            parity_file.write_text(json.dumps(known, indent=1, ensure_ascii=False), encoding="utf-8")
    if not known[key]["ok"]:
        model.close()
        return None, f"{gpu.name}: {known[key]['describe']}"
    return model, f"{gpu.name}: {known[key]['describe']}"


def main():
    started = time.perf_counter()
    wichelberg, voice_id = Path(sys.argv[1]), sys.argv[2]
    accel = sys.argv[3] if len(sys.argv) > 3 else "cpu"
    parity_file = Path(sys.argv[4]) if len(sys.argv) > 4 else None
    sys.path.insert(0, str(wichelberg))

    import soundfile as sf
    from voicechanger import render
    from voicechanger.ai.base import ConvertParams
    from voicechanger.ai.rvc_onnx import RvcModel, resample
    from voicechanger.ai.runtime import Runtime
    from voicechanger.ai.voices import load_voice
    from voicechanger.dsp.params import VoiceSettings
    from voicechanger.render import render_ai

    pack = Path(voice_id)
    voice = load_voice(pack if pack.is_dir() else wichelberg / "models" / "voices" / voice_id)
    settings = VoiceSettings()
    params = ConvertParams(index_rate=voice.default_index_rate)
    key = (voice.id, voice.version, "fcpe")  # render_ai's own model cache: put the model to use in it

    def convert(model, audio):
        render._AI_MODELS.clear()
        render._AI_MODELS[key] = model
        return render_ai(audio, settings, voice, 0.0, params)[0]

    gpu_model, note = None, "asked for the CPU"
    if accel == "gpu":
        try:
            gpu_model, note = _gpu_model(wichelberg, voice, parity_file, convert)
        except Exception as e:
            note = f"GPU failed: {type(e).__name__}: {e}"
    models = {}

    def model_for(audio):
        if gpu_model is not None and len(audio) <= GPU_MAX_SECONDS * SAMPLE_RATE:
            return gpu_model
        if "cpu" not in models:  # opened when first needed: ~1 s
            models["cpu"] = RvcModel(voice, Runtime(), "fcpe")
        return models["cpu"]

    if gpu_model is None:
        model_for(None)
    from voicechanger.ai.testsignal import speech_like
    warm = speech_like(1.0, SAMPLE_RATE).astype("float32")
    convert(model_for(warm), warm)  # the first conversion is ~3x slower (measured: 1.07 s vs 0.38 s)
    print(json.dumps({"ready": True, "device": "cpu" if gpu_model is None else gpu_model.generator.provider,
                      "seconds": round(time.perf_counter() - started, 2), "note": note}), flush=True)

    for line in sys.stdin:
        if not line.strip():
            continue
        started = time.perf_counter()
        try:
            request = json.loads(line)
            audio, rate = sf.read(request["in"], dtype="float32", always_2d=True)
            audio = audio.mean(axis=1)
            if rate != SAMPLE_RATE:
                audio = resample(audio, rate, SAMPLE_RATE)
            model = model_for(audio)
            converted = convert(model, audio)
            sf.write(request["wav"], converted, SAMPLE_RATE, subtype="PCM_16")
            if request.get("ogg"):
                sf.write(request["ogg"], converted, SAMPLE_RATE, format="OGG", subtype="OPUS")
            # DirectML falls back to the CPU by itself if it fails mid-run
            reply = {"ok": True, "seconds": round(time.perf_counter() - started, 2), "device": model.generator.provider}
        except Exception as e:
            reply = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        print(json.dumps(reply), flush=True)


if __name__ == "__main__":
    main()
