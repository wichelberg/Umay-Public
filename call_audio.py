"""The sound of a WhatsApp call, for Umay's side of it (calls.py): what the owner says comes out of the speakers,
and Umay hears it from there (WASAPI loopback of the default speakers, not the microphone: the room's echo and
noise stay out, and the owner can still hear the call in the room). Her own voice goes into VB-CABLE
("CABLE Input"), which WhatsApp Beta takes as its microphone ("CABLE Output"), so she never hears herself.

LoopbackRecorder has voice.Recorder's face (cut, level(), stop()), so voice_session.Session runs a call the
way it runs a spoken conversation. WASAPI loopback gives nothing while nothing plays, so a ticker feeds that
quiet in as silence: else the end of what the owner said would never come.

CableEar and peaks() measure both ways for calls.log (2026-11-02 her greeting never reached the owner, and
nothing showed where it went): the cable's other end (what WhatsApp gets from her) and the loopback (what she
gets from WhatsApp).
"""
import io
import threading
import time

import numpy as np
import soundfile as sf

import voice

RATE = 16000
TICK = 0.05            # seconds: no sound from the speakers this long counts as silence
CABLE = "CABLE Input"   # where her voice goes (VB-CABLE's playback side)
CABLE_EAR = "CABLE Output"  # what WhatsApp takes as its microphone
_heard = {"peak": 0.0}  # the loopback's loudest sample since peaks() was last read


def peaks(ear=None):
    """(cable, loopback): the loudest samples since the last call, 0..1. Resets both."""
    heard, _heard["peak"] = _heard["peak"], 0.0
    return (ear.peak() if ear is not None else 0.0), heard


class CableEar:
    """Listens to the cable's microphone side, beside WhatsApp (shared mode), only to measure it."""

    def __init__(self):
        import sounddevice

        found = [i for i, d in enumerate(sounddevice.query_devices())
                 if d["max_input_channels"] > 0 and CABLE_EAR.lower() in d["name"].lower()]
        if not found:
            raise RuntimeError(f"no input device called {CABLE_EAR!r}")
        self._peak = 0.0

        def callback(block, frames, when, status):
            self._peak = max(self._peak, float(np.abs(block).max()))

        self._stream = sounddevice.InputStream(device=found[0], channels=1, samplerate=48000, dtype="float32",
                                               callback=callback)
        self._stream.start()

    def peak(self):
        peak, self._peak = self._peak, 0.0
        return peak

    def stop(self):
        self._stream.stop()
        self._stream.close()


def to_16k_mono(block, rate, channels):
    """An interleaved float32 block at `rate` -> mono at 16 kHz (averaging stands in for the low-pass filter)."""
    mono = block.reshape(-1, channels).mean(axis=1) if channels > 1 else block
    if rate == RATE:
        return mono.astype(np.float32)
    step = rate / RATE
    if float(step).is_integer():
        n = int(step)
        mono = mono[: len(mono) // n * n].reshape(-1, n).mean(axis=1)
        return mono.astype(np.float32)
    idx = np.arange(0, len(mono), step)
    return np.interp(idx, np.arange(len(mono)), mono).astype(np.float32)


class LoopbackRecorder:
    """Records what the default speakers play until stop(); on_silence() once the owner has finished talking
    (voice.SilenceCut decides, as for the microphone)."""

    def __init__(self, on_silence=None, floor=None, longest=voice.SilenceCut.LONGEST):
        import pyaudiowpatch as pa

        self._pa = pa.PyAudio()
        speakers = self._pa.get_device_info_by_index(
            self._pa.get_host_api_info_by_type(pa.paWASAPI)["defaultOutputDevice"])
        device = next((d for d in self._pa.get_loopback_device_info_generator()
                       if speakers["name"] in d["name"]), None)
        if device is None:
            self._pa.terminate()
            raise RuntimeError(f"no loopback for {speakers['name']}")
        self.rate, self.channels = int(device["defaultSampleRate"]), int(device["maxInputChannels"]) or 2
        self._chunks = []
        self.cut = voice.SilenceCut(RATE, floor, longest) if on_silence else None
        self._on_silence, self._done = on_silence, False
        self._lock = threading.Lock()

        self._last = time.monotonic()
        self._stopped = False

        def callback(data, frames, when, status):
            block = np.frombuffer(data, dtype=np.float32)
            if block.size:
                _heard["peak"] = max(_heard["peak"], float(np.abs(block).max()))
            self._take(to_16k_mono(block, self.rate, self.channels))
            return None, pa.paContinue

        self._stream = self._pa.open(format=pa.paFloat32, channels=self.channels, rate=self.rate, input=True,
                                     input_device_index=device["index"], frames_per_buffer=self.rate // 20,
                                     stream_callback=callback)
        self._stream.start_stream()
        threading.Thread(target=self._tick, daemon=True).start()

    def _take(self, block):
        with self._lock:
            self._last = time.monotonic()
            self._chunks.append(block)
        if self.cut is not None and not self._done and self.cut.feed(block):
            self._done = True
            threading.Thread(target=self._on_silence, daemon=True).start()

    def _tick(self):
        """Silence for the time nothing played (the loopback sent nothing)."""
        while not self._stopped:
            time.sleep(TICK)
            with self._lock:
                gap = time.monotonic() - self._last
            if gap >= TICK * 2:
                self._take(np.zeros(int(gap * RATE), np.float32))

    def level(self):
        return self.cut.level if self.cut is not None else 0.0

    def stop(self):
        """Stops recording and returns wav bytes (None if nothing was recorded)."""
        self._stopped = True
        try:
            self._stream.stop_stream()
            self._stream.close()
        finally:
            self._pa.terminate()
        with self._lock:
            chunks = list(self._chunks)
        if not chunks:
            return None
        out = io.BytesIO()
        sf.write(out, np.concatenate(chunks), RATE, format="WAV", subtype="PCM_16")
        return out.getvalue()
