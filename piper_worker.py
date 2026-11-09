"""Local English text-to-speech (Piper). Runs inside piper_env, not Umay's Python.

piper_env (setup_voice_env.py --piper) holds piper-tts and onnxruntime. Piper runs on the CPU, so the
GPU stays free for Whisper and the local model: measured on the Ryzen AI 7 350, a 4-5 s sentence takes
~0.1 s (~40x faster than real time). The voice it makes is only the reader: Umay's RVC voice pack
(voice_worker.py) turns it into her voice afterwards. Started and kept alive by voice.py, stopped by it
when idle. Same protocol as tts_worker.py (Chatterbox), so voice.py treats both alike.

argv: <voice .onnx file (its .onnx.json next to it)>
stdin: one JSON request per line: {"text": what to say, "out": wav path}
stdout: {"ready": true, "device": "cpu", "seconds": ..., "rate": ...} once loaded, then one
        JSON reply per request: {"ok": true, "seconds": ..., "audio_seconds": ...} or {"ok": false, "error": "..."}
"""
import json
import sys
import time
import wave

WARM_UP = "Hello, I'm ready."  # read once before "ready", so a reply's first sentence isn't slow


def main():
    started = time.perf_counter()
    from piper import PiperVoice

    voice = PiperVoice.load(sys.argv[1])
    for _ in voice.synthesize(WARM_UP):
        pass
    print(json.dumps({"ready": True, "device": "cpu", "seconds": round(time.perf_counter() - started, 2),
                      "rate": voice.config.sample_rate}), flush=True)

    for line in sys.stdin:
        if not line.strip():
            continue
        started = time.perf_counter()
        try:
            request = json.loads(line)
            with wave.open(request["out"], "wb") as out:
                voice.synthesize_wav(request["text"], out)
            with wave.open(request["out"]) as made:
                audio_seconds = made.getnframes() / made.getframerate()
            reply = {"ok": True, "seconds": round(time.perf_counter() - started, 2),
                     "audio_seconds": round(audio_seconds, 2)}
        except Exception as e:
            reply = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        print(json.dumps(reply), flush=True)  # ASCII-escaped: the pipe's code page can't break it


if __name__ == "__main__":
    main()
