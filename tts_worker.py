"""Local Turkish text-to-speech (Chatterbox Multilingual). Runs inside tts_env, not Umay's Python.

tts_env (setup_voice_env.py --tts) holds Chatterbox and the newer transformers/librosa it
needs; torch comes from Wichelberg's training venv. The voice it makes is only the reader:
a short Turkish reference clip gives it Turkish intonation, and Umay's RVC voice pack
(voice_worker.py) turns it into her voice afterwards. Started and kept alive by voice.py,
stopped by it when idle. GPU only: on the CPU it is several times slower than real time,
so voice.py then says nothing (the reply stays as text).

argv: <model dir> <reference wav> <language>
stdin: one JSON request per line: {"text": what to say, "out": wav path}
stdout: {"ready": true, "device": "cuda", "seconds": ..., "rate": ...} once loaded, then one
        JSON reply per request: {"ok": true, "seconds": ..., "audio_seconds": ...} or {"ok": false, "error": "..."}
"""
import json
import sys
import time
from pathlib import Path

EXAGGERATION = 0.5  # emotion strength
CFG_WEIGHT = 0.5    # how closely it follows the reference's pacing
WARM_UP = "Merhaba, hazırım."  # read once before "ready", so a reply's first sentence isn't slow


def main():
    model_dir, reference, language = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    started = time.perf_counter()
    import soundfile as sf
    import torch
    from chatterbox.mtl_tts import ChatterboxMultilingualTTS

    if not torch.cuda.is_available():
        raise SystemExit("no CUDA GPU")
    model = ChatterboxMultilingualTTS.from_local(model_dir, "cuda")
    model.prepare_conditionals(reference, exaggeration=EXAGGERATION)
    if torch.cuda.is_bf16_supported():
        # T3 (the 0.5B token model) in bfloat16: 1 GB less VRAM (peak 3.5 -> 2.5 GB), same speed, and on
        # 20 Turkish sentences Whisper heard it as well (WER 5.4% vs 9.0% in fp32: noise). S3Gen, which
        # makes the sound itself, stays in fp32.
        model.t3.to(torch.bfloat16)
        model.conds.t3 = model.conds.t3.to(dtype=torch.bfloat16)
        torch.cuda.empty_cache()
    with torch.inference_mode():  # the first sentence after loading is ~1-5 s slower (CUDA warm-up):
        model.generate(WARM_UP, language_id=language, exaggeration=EXAGGERATION, cfg_weight=CFG_WEIGHT)
    print(json.dumps({"ready": True, "device": "cuda", "seconds": round(time.perf_counter() - started, 2),
                      "rate": model.sr}), flush=True)

    for line in sys.stdin:
        if not line.strip():
            continue
        started = time.perf_counter()
        try:
            request = json.loads(line)
            with torch.inference_mode():
                wav = model.generate(request["text"], language_id=language, exaggeration=EXAGGERATION,
                                     cfg_weight=CFG_WEIGHT)
            audio = wav.squeeze().cpu().numpy()
            sf.write(request["out"], audio, model.sr, subtype="PCM_16")
            reply = {"ok": True, "seconds": round(time.perf_counter() - started, 2),
                     "audio_seconds": round(len(audio) / model.sr, 2)}
        except Exception as e:
            reply = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        print(json.dumps(reply), flush=True)  # ASCII-escaped: the pipe's code page can't break it


if __name__ == "__main__":
    main()
