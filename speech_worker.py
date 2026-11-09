"""Local speech-to-text (faster-whisper). Runs inside voice_env, not Umay's Python.

voice_env (made by setup_voice_env.py) only holds faster-whisper and ctranslate2; the
rest, including the CUDA DLLs that ship with torch, comes from Wichelberg's training
venv. Started and kept alive by voice.py, stopped by it when idle.

argv: <models dir> <model name> <language, or "auto">
stdin: one JSON request per line: {"wav": audio path, "prompt": text or null}
stdout: {"ready": true, "device": ..., "seconds": ...} once loaded, then one JSON reply
        per request: {"ok": true, "text": ..., "seconds": ...} or {"ok": false, "error": "..."}

argv: --download <models dir> <model name>   only downloads the model (setup_voice_env.py),
        so Umay never waits on a download while you wait for a transcript.
"""
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

GPU_COMPUTE = "int8_float16"
CPU_COMPUTE = "int8"
BEAM_SIZE = 5
# Audio longer than Whisper's 30 s window is first cut at pauses (VAD) into pieces that are
# transcribed on their own (batched). Plain transcribe() without timestamps cuts it at fixed 30 s
# windows and loses words at the seams (2-7 words a minute measured); with timestamps it does
# worse on the fine-tuned models (trained without them). Shorter audio is one window anyway and
# keeps the plain path.
WINDOW_SECONDS = 30
BATCH_SIZE = 8


def _add_cuda_dlls():
    """ctranslate2 needs cuBLAS 12 and cuDNN 9; torch (from Wichelberg's venv) ships both."""
    spec = importlib.util.find_spec("torch")
    if spec is None or not spec.submodule_search_locations:
        return
    lib = Path(list(spec.submodule_search_locations)[0]) / "lib"
    if lib.is_dir():
        os.add_dll_directory(str(lib))
        os.environ["PATH"] = str(lib) + os.pathsep + os.environ.get("PATH", "")


def _model_dir(models_dir, model_name):
    """A local model folder: model_name itself if it is one, else models_dir/model_name
    (voice.LocalStt.model_folder looks in the same place), downloaded there if missing.
    A plain folder, not the Hugging Face cache: the cache needs symlinks, which Windows
    only allows in developer mode or as admin."""
    if Path(model_name).is_dir():
        return str(model_name)
    folder = Path(models_dir) / model_name.replace("/", "--")
    if not (folder / "model.bin").is_file():
        from faster_whisper.utils import download_model

        download_model(model_name, output_dir=str(folder))
    return str(folder)


def _load(models_dir, model_name):
    """The model on the GPU if it really runs there (checked with one short transcription),
    otherwise on the CPU. Returns (model, device)."""
    import ctranslate2
    import numpy as np
    from faster_whisper import WhisperModel

    path = _model_dir(models_dir, model_name)
    silence = np.zeros(16000, dtype=np.float32)
    if ctranslate2.get_cuda_device_count() > 0:
        try:
            model = WhisperModel(path, device="cuda", compute_type=GPU_COMPUTE)
            list(model.transcribe(silence, beam_size=1)[0])
            return model, "cuda"
        except Exception as e:
            print(f"GPU unavailable, using the CPU: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
    return WhisperModel(path, device="cpu", compute_type=CPU_COMPUTE), "cpu"


def main():
    if sys.argv[1] == "--download":
        print(json.dumps({"path": _model_dir(sys.argv[2], sys.argv[3])}), flush=True)
        return
    models_dir, model_name, language = sys.argv[1], sys.argv[2], sys.argv[3]
    started = time.perf_counter()
    _add_cuda_dlls()
    model, device = _load(models_dir, model_name)
    from faster_whisper import BatchedInferencePipeline, decode_audio

    batched = BatchedInferencePipeline(model)
    print(json.dumps({"ready": True, "device": device, "seconds": round(time.perf_counter() - started, 2)}), flush=True)

    for line in sys.stdin:
        if not line.strip():
            continue
        started = time.perf_counter()
        try:
            request = json.loads(line)
            audio = decode_audio(request["wav"])  # 16 kHz mono
            options = dict(language=None if language == "auto" else language, beam_size=BEAM_SIZE,
                           condition_on_previous_text=False, without_timestamps=True,
                           initial_prompt=request.get("prompt") or None)
            if len(audio) > WINDOW_SECONDS * 16000:
                segments, _ = batched.transcribe(audio, batch_size=BATCH_SIZE, **options)
            else:
                segments, _ = model.transcribe(audio, vad_filter=True, **options)
            text = " ".join(segment.text.strip() for segment in segments).strip()
            reply = {"ok": True, "text": text, "seconds": round(time.perf_counter() - started, 2)}
        except Exception as e:
            reply = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        print(json.dumps(reply), flush=True)  # ASCII-escaped: the pipe's code page can't break it


if __name__ == "__main__":
    main()
