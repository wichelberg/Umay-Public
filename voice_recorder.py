"""Records the owner reading sentences aloud, for training speech-to-text on their voice.

python voice_recorder.py [--sentences file] [--again]

Shows one sentence at a time from voice_sentences_tr.txt. Space starts/stops recording,
D plays it back, Enter keeps it and moves on, S skips. Kept clips go to voice_data
(source "reading"), next to the confirmed Telegram and 🎤 clips. Sentences already
recorded are skipped, so it can be stopped and resumed any time; --again goes through
all of them once more (a second take helps too).
"""
import argparse
import io
import tempfile
import tkinter as tk
import winsound
from pathlib import Path

import numpy as np
import soundfile as sf

import paths
import voice
import voice_data

SENTENCES = paths.resource_dir() / "voice_sentences_tr.txt"
TARGET_MINUTES = 30
MIN_SECONDS = 0.6
QUIET_PEAK = 0.03
CLIP_PEAK = 0.99

BG = "#241333"
CARD = "#2F1B44"
GOLD = "#D4AF37"
TEXT = "#FDFBFF"
MUTED = "#8A7F99"
RED = "#E0565B"


def load_sentences(path):
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


def check(wav_bytes):
    """(seconds, warning or None) for a recorded take."""
    audio, rate = sf.read(io.BytesIO(wav_bytes), dtype="float32")
    seconds = len(audio) / rate
    peak = float(np.abs(audio).max()) if len(audio) else 0.0
    if seconds < MIN_SECONDS:
        return seconds, "Çok kısa, tekrar kaydet."
    if peak < QUIET_PEAK:
        return seconds, "Çok sessiz: mikrofona biraz yaklaş ya da daha yüksek sesle oku."
    if peak >= CLIP_PEAK:
        return seconds, "Ses patlıyor (kırpılma): mikrofondan biraz uzaklaş."
    return seconds, None


class App:
    def __init__(self, root, sentences, again):
        self.root = root
        done = set() if again else {s["text"] for s in voice_data.samples() if s.get("source") == "reading"}
        self.todo = [s for s in sentences if s not in done]
        self.total = len(sentences)
        self.index = 0
        self.recorder = None
        self.take = None
        self.play_path = Path(tempfile.gettempdir()) / "umay_reading_take.wav"

        root.title("Umay · ses kaydı")
        root.configure(bg=BG)
        root.geometry("760x380")
        root.minsize(520, 320)
        self.progress = tk.Label(root, bg=BG, fg=MUTED, font=("Segoe UI", 10))
        self.progress.pack(pady=(14, 6))
        card = tk.Frame(root, bg=CARD, padx=24, pady=24)
        card.pack(fill="both", expand=True, padx=18)
        self.sentence = tk.Label(card, bg=CARD, fg=TEXT, font=("Segoe UI", 20), wraplength=660, justify="center")
        self.sentence.pack(expand=True)
        self.status = tk.Label(root, bg=BG, fg=GOLD, font=("Segoe UI", 10))
        self.status.pack(pady=8)
        row = tk.Frame(root, bg=BG)
        row.pack(pady=(0, 16))
        self.record_btn = self._button(row, "● Kaydet  [Boşluk]", self.toggle)
        self._button(row, "▶ Dinle  [D]", self.play)
        self.keep_btn = self._button(row, "✓ Tamam, sonraki  [Enter]", self.keep)
        self._button(row, "↷ Atla  [S]", self.skip)
        root.bind("<space>", lambda e: self.toggle())
        root.bind("<Return>", lambda e: self.keep())
        root.bind("<Key-d>", lambda e: self.play())
        root.bind("<Key-s>", lambda e: self.skip())
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.show()

    def _button(self, parent, text, command):
        button = tk.Button(parent, text=text, command=command, bg=BG, fg=GOLD, activebackground=CARD,
                           activeforeground=TEXT, relief="flat", borderwidth=0, padx=12, pady=6,
                           font=("Segoe UI", 10, "bold"), takefocus=0, cursor="hand2")
        button.pack(side="left", padx=4)
        return button

    def recorded_minutes(self):
        return sum(s.get("seconds", 0) for s in voice_data.samples()) / 60

    def show(self):
        self.take = None
        minutes = self.recorded_minutes()
        if self.index >= len(self.todo):
            self.sentence.config(text="Hepsi bitti, teşekkürler!\nİkinci tur için: python voice_recorder.py --again")
            self.status.config(text="", fg=GOLD)
        else:
            self.sentence.config(text=self.todo[self.index])
            self.status.config(text="Hazır olunca Boşluk'a bas ve cümleyi oku.", fg=GOLD)
        done = self.total - len(self.todo) + self.index
        self.progress.config(text=f"Cümle {min(done + 1, self.total)} / {self.total}   ·   "
                                  f"toplam kayıt {minutes:.1f} / {TARGET_MINUTES} dk")

    def toggle(self):
        if self.index >= len(self.todo):
            return
        if self.recorder is None:
            winsound.PlaySound(None, 0)
            try:
                self.recorder = voice.Recorder()
            except Exception as e:
                self.status.config(text=f"Mikrofon açılamadı: {e}", fg=RED)
                return
            self.take = None
            self.record_btn.config(text="■ Durdur  [Boşluk]", fg=RED)
            self.status.config(text="● Kaydediliyor… okuyunca Boşluk'a bas.", fg=RED)
            return
        recorder, self.recorder = self.recorder, None
        self.record_btn.config(text="● Kaydet  [Boşluk]", fg=GOLD)
        self.take = recorder.stop()
        if self.take is None:
            self.status.config(text="Ses gelmedi, tekrar dene.", fg=RED)
            return
        seconds, warning = check(self.take)
        if warning and seconds < MIN_SECONDS:
            self.take = None
        self.status.config(text=warning or f"{seconds:.1f} sn kaydedildi. Enter: kaydet ve geç · D: dinle · Boşluk: tekrar",
                           fg=RED if warning else GOLD)

    def play(self):
        if self.take:
            self.play_path.write_bytes(self.take)
            winsound.PlaySound(str(self.play_path), winsound.SND_FILENAME | winsound.SND_ASYNC)

    def keep(self):
        if self.recorder is not None or not self.take or self.index >= len(self.todo):
            return
        voice_data.save(self.take, self.todo[self.index], "reading")
        self.index += 1
        self.show()

    def skip(self):
        if self.recorder is not None or self.index >= len(self.todo):
            return
        self.index += 1
        self.show()

    def close(self):
        if self.recorder is not None:
            self.recorder.stop()
        winsound.PlaySound(None, 0)
        self.play_path.unlink(missing_ok=True)
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sentences", default=str(SENTENCES), help="one sentence per line (# = comment)")
    parser.add_argument("--again", action="store_true", help="record sentences that already have a take too")
    args = parser.parse_args()
    root = tk.Tk()
    App(root, load_sentences(args.sentences), args.again)
    root.mainloop()


if __name__ == "__main__":
    main()
