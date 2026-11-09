"""Fine-tunes Umay's speech-to-text on the owner's voice (LoRA) and makes a model that
speech_worker.py can load.

Run it with train_env's Python (`python setup_voice_env.py --training` makes it):
  %LOCALAPPDATA%\\Umay\\voice\\train_env\\Scripts\\python.exe train_stt.py [options]

One run does everything:
 1. Data: the owner's confirmed clips from voice_data (Telegram "Yes", ✓/✎ under 🎤,
    voice_recorder.py), split by sentence into train and a held-out test share, plus their
    recordings of voice_reading_tr.txt in UserVoice (N_<device>.<ext>, e.g. 1_pc.m4a,
    1_telefon.m4a; the last section is the test, scored per device). Clips
    longer than Whisper's 30 s are cut at quiet moments, and their text is shared out over
    the pieces (see stt_eval.assign_words). --cv N mixes N Common Voice tr clips into
    training so it doesn't unlearn other voices; --cv-test N adds N Common Voice tr test
    clips to check exactly that.
 2. The base model's word error rate (WER) on the test clips.
 3. LoRA training on the train clips (the base stays frozen, ~1% of the weights learn).
 4. The tuned model's WER on the same test clips.
 5. The tuned model, merged and converted to CTranslate2, goes to
    voice_home/models/<name>; the run ends with the UMAY_STT_MODEL line that uses it.
    Nothing changes for Umay until that line is in .env.
Results: voice_home/training/<name>/report.json (with every test transcript).

WER (stt_eval.wer) lowercases, drops punctuation and apostrophes, and is also given
ignoring numbers: Whisper writes "15" where the reference says "on beş", which counts as
errors that aren't really mistakes.
"""
import argparse
import csv
import json
import math
import random
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402
import torch  # noqa: E402
import torchaudio  # noqa: E402

import paths  # noqa: E402
import stt_eval  # noqa: E402
import voice_data  # noqa: E402

BASE_MODEL = "RsGoksel/RsGoksel_ITU_Mainframe"  # HF weights of the default ct2 model
COMMON_VOICE = (Path.home() / "Documents" / "Projects" / "Wichelberg_VoiceChanger" / "data" / "egitim"
                / "CommonVoice" / "cv-corpus-27.0-2026-09-11" / "tr")
RATE = 16000
MAX_SECONDS = 30  # Whisper's window
MAX_LABEL_TOKENS = 440


# --- data -----------------------------------------------------------------------------

def load_audio(path):
    """Mono 16 kHz float32. libsndfile for wav/flac/ogg/mp3, PyAV (ffmpeg) for the rest,
    e.g. a phone recorder's m4a."""
    try:
        audio, rate = sf.read(str(path), dtype="float32", always_2d=True)
    except Exception:
        return _decode_with_av(path)
    audio = torch.from_numpy(audio.mean(axis=1))
    if rate != RATE:
        audio = torchaudio.functional.resample(audio, rate, RATE)
    return audio.numpy()


def _decode_with_av(path):
    import av

    chunks = []
    resampler = av.AudioResampler(format="flt", layout="mono", rate=RATE)
    with av.open(str(path)) as container:
        for frame in container.decode(audio=0):
            chunks += [out.to_ndarray().reshape(-1) for out in resampler.resample(frame)]
    chunks += [out.to_ndarray().reshape(-1) for out in resampler.resample(None)]
    return np.concatenate(chunks).astype(np.float32)


def user_voice_clips(folder, reading_file, test_section):
    """(train, test) from the owner's recordings of the read-aloud texts. The test section
    (default: the last one) is only scored, never trained on."""
    sections = stt_eval.reading_sections(Path(reading_file).read_text(encoding="utf-8"))
    test_section = test_section or max(sections, default=0)
    train, test = [], []
    for path, number, device in stt_eval.user_voice_files(folder):
        if number not in sections:
            print(f"  {path.name}: there is no section {number} in {Path(reading_file).name}, skipped", flush=True)
            continue
        clip = {"audio": path, "text": sections[number], "source": f"uservoice {device}"}
        (test if number == test_section else train).append(clip)
    return train, test


def owner_clips(test_share):
    """(train, test) from voice_data, split by sentence."""
    clips = [{"audio": voice_data.data_dir() / s["audio"], "text": s["text"], "source": s["source"]}
             for s in voice_data.samples()]
    return stt_eval.split_by_sentence(clips, test_share)


def common_voice(split, count, seed):
    if count <= 0:
        return []
    with open(COMMON_VOICE / f"{split}.tsv", encoding="utf-8") as f:
        rows = list(csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE))
    random.Random(seed).shuffle(rows)
    return [{"audio": COMMON_VOICE / "clips" / r["path"], "text": r["sentence"], "source": "commonvoice"}
            for r in rows[:count]]


def with_audio(clips, tokenizer):
    """Loads each clip's audio. Returns (fit, long, dropped): `long` are over Whisper's 30 s
    and get cut later (split_long); unreadable, empty or too-long-text clips are dropped."""
    fit, long, dropped = [], [], 0
    for clip in clips:
        try:
            audio = load_audio(clip["audio"])
        except Exception:
            dropped += 1
            continue
        seconds = len(audio) / RATE
        if seconds < 0.3:
            dropped += 1
        elif seconds > MAX_SECONDS:
            long.append({**clip, "array": audio})
        elif len(tokenizer(clip["text"]).input_ids) > MAX_LABEL_TOKENS:
            dropped += 1
        else:
            fit.append({**clip, "array": audio})
    return fit, long, dropped


def split_long(clips, model, processor, device, batch_size):
    """Cuts each long clip at quiet moments into pieces Whisper can take and gives each piece
    its share of the confirmed text. Returns (pieces, pieces dropped as unsure)."""
    pieces, unsure = [], 0
    for clip in clips:
        parts = [{**clip, "array": a} for a in np.split(clip["array"], stt_eval.cut_points(clip["array"], RATE))]
        heard = transcribe(model, processor, parts, device, batch_size)
        for part, text in zip(parts, stt_eval.assign_words(clip["text"], heard)):
            if text and len(processor.tokenizer(text).input_ids) <= MAX_LABEL_TOKENS:
                pieces.append({**part, "text": text})
            else:
                unsure += 1
    return pieces, unsure


# --- model ----------------------------------------------------------------------------

class Processor:
    """Feature extractor + fast tokenizer. Not WhisperProcessor: newer uploads only ship
    tokenizer.json, and transformers 4.49's WhisperProcessor only takes the slow tokenizer."""

    def __init__(self, folder):
        from transformers import WhisperFeatureExtractor, WhisperTokenizerFast

        self.feature_extractor = WhisperFeatureExtractor.from_pretrained(folder)
        self.tokenizer = WhisperTokenizerFast.from_pretrained(folder)

    def batch_decode(self, *args, **kwargs):
        return self.tokenizer.batch_decode(*args, **kwargs)

    def save_pretrained(self, folder):
        self.feature_extractor.save_pretrained(folder)
        self.tokenizer.save_pretrained(folder)


def base_folder(name):
    folder = paths.voice_home() / "base" / name.replace("/", "--")
    if not (folder / "config.json").is_file():
        from huggingface_hub import snapshot_download

        print(f"Downloading {name} (~1.6 GB) ...", flush=True)
        snapshot_download(name, local_dir=str(folder), allow_patterns=["*.json", "*.safetensors"])
    return folder


def batches(items, size):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def features(processor, clips, device):
    arrays = [clip["array"] for clip in clips]
    return processor.feature_extractor(arrays, sampling_rate=RATE, return_tensors="pt").input_features.to(device)


def labels(processor, model, clips, device):
    tokens = processor.tokenizer([clip["text"] for clip in clips], padding=True, return_tensors="pt")
    ids = tokens.input_ids.masked_fill(tokens.attention_mask == 0, -100)
    if (ids[:, 0] == model.config.decoder_start_token_id).all():
        ids = ids[:, 1:]  # the model puts the start token back itself
    return ids.to(device)


@torch.no_grad()
def transcribe(model, processor, clips, device, batch_size):
    model.eval()
    heard = []
    for batch in batches(clips, batch_size):
        with torch.autocast("cuda", dtype=torch.bfloat16, enabled=device == "cuda"):
            ids = model.generate(input_features=features(processor, batch, device), language="tr",
                                 task="transcribe", max_new_tokens=225)
        heard += [text.strip() for text in processor.batch_decode(ids, skip_special_tokens=True)]
    return heard


def score(model, processor, sets, device, batch_size):
    """{set name: {wer..., "pairs": [[reference, heard], ...]}}"""
    result = {}
    for name, clips in sets.items():
        if clips:
            pairs = list(zip([c["text"] for c in clips], transcribe(model, processor, clips, device, batch_size)))
            result[name] = {**stt_eval.wer(pairs), "pairs": pairs}
    return result


def train(model, processor, clips, device, args):
    params = [p for p in model.parameters() if p.requires_grad]
    optimizer = torch.optim.AdamW(params, lr=args.lr, weight_decay=0.01)
    steps = math.ceil(len(clips) / (args.batch * args.accumulate)) * args.epochs
    if args.max_steps:
        steps = min(steps, args.max_steps)
    from transformers import get_linear_schedule_with_warmup

    scheduler = get_linear_schedule_with_warmup(optimizer, max(1, steps // 10), steps)
    rng = random.Random(args.seed)
    step, losses, started = 0, [], time.time()
    model.train()
    while step < steps:
        order = clips[:]
        rng.shuffle(order)
        optimizer.zero_grad()  # a last partial accumulation doesn't carry into the next epoch
        for index, batch in enumerate(batches(order, args.batch)):
            with torch.autocast("cuda", dtype=torch.bfloat16, enabled=device == "cuda"):
                loss = model(input_features=features(processor, batch, device),
                             labels=labels(processor, model, batch, device)).loss
            (loss / args.accumulate).backward()
            losses.append(loss.item())
            if (index + 1) % args.accumulate:
                continue
            torch.nn.utils.clip_grad_norm_(params, 1.0)
            optimizer.step()
            scheduler.step()
            optimizer.zero_grad()
            step += 1
            if step % 10 == 0 or step == steps:
                recent = losses[-args.accumulate * 10:]
                print(f"  step {step}/{steps}  loss {sum(recent) / len(recent):.3f}  "
                      f"{time.time() - started:.0f} s", flush=True)
            if step >= steps:
                break
    optimizer.zero_grad()
    return {"steps": steps, "first_loss": round(float(np.mean(losses[:args.accumulate * 5])), 3),
            "last_loss": round(float(np.mean(losses[-args.accumulate * 5:])), 3),
            "seconds": round(time.time() - started)}


def export(model, processor, out_name, keep_hf):
    """Merges LoRA into the base and writes a CTranslate2 model where speech_worker looks."""
    from ctranslate2.converters import TransformersConverter

    class Converter(TransformersConverter):
        """ctranslate2 4.8's converter passes `dtype`, which transformers 4.49 calls `torch_dtype`."""

        def load_model(self, model_class, model_name_or_path, **kwargs):
            if "dtype" in kwargs:
                kwargs["torch_dtype"] = kwargs.pop("dtype")
            return super().load_model(model_class, model_name_or_path, **kwargs)

    work = paths.voice_home() / "training" / out_name
    hf = work / "merged"
    merged = model.merge_and_unload()
    merged.to(torch.float16).save_pretrained(hf, safe_serialization=True)
    processor.save_pretrained(hf)
    target = paths.voice_home() / "models" / out_name
    Converter(str(hf), copy_files=["tokenizer.json", "preprocessor_config.json"]).convert(
        str(target), quantization="float16", force=True)
    if not keep_hf:
        for file in hf.iterdir():
            file.unlink()
        hf.rmdir()
    return target


# --- main -----------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default=BASE_MODEL, help="Hugging Face Whisper model to start from")
    parser.add_argument("--name", default=f"owner-{datetime.now():%Y%m%d-%H%M}", help="name of the new model")
    parser.add_argument("--test-share", type=float, default=0.15, help="share of the owner's sentences held out")
    parser.add_argument("--cv", type=int, default=0, help="Common Voice tr clips mixed into training")
    parser.add_argument("--cv-test", type=int, default=0, help="Common Voice tr test clips to check other voices")
    here = Path(__file__).resolve().parent
    parser.add_argument("--user-voice", default=str(here / "UserVoice"),
                        help="folder of the owner's recordings of the read-aloud texts (N_<device>.<ext>)")
    parser.add_argument("--reading", default=str(here / "voice_reading_tr.txt"), help="the read-aloud texts")
    parser.add_argument("--test-section", type=int, default=0,
                        help="read-aloud section kept for testing (default: the last one)")
    parser.add_argument("--epochs", type=int, default=4)
    parser.add_argument("--max-steps", type=int, default=0, help="stop after this many optimizer steps (0 = epochs)")
    parser.add_argument("--lr", type=float, default=2e-4)
    parser.add_argument("--rank", type=int, default=16, help="LoRA rank")
    parser.add_argument("--batch", type=int, default=4)
    parser.add_argument("--accumulate", type=int, default=4, help="batches per optimizer step")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--keep-hf", action="store_true", help="keep the merged Hugging Face model too")
    args = parser.parse_args()

    from peft import LoraConfig, get_peft_model
    from transformers import WhisperForConditionalGeneration

    device = "cuda" if torch.cuda.is_available() else "cpu"
    if device == "cpu":
        print("No CUDA GPU: this would take very long on the CPU.", flush=True)
    torch.manual_seed(args.seed)
    base = base_folder(args.base)
    processor = Processor(base)
    processor.tokenizer.set_prefix_tokens(language="turkish", task="transcribe", predict_timestamps=False)

    owner_train, owner_test = owner_clips(args.test_share)
    reading_train, reading_test = user_voice_clips(args.user_voice, args.reading, args.test_section)
    train_clips, train_long, dropped = with_audio(
        owner_train + reading_train + common_voice("train", args.cv, args.seed), processor.tokenizer)
    test_fit, test_long, dropped_test = with_audio(owner_test + reading_test, processor.tokenizer)
    cv_test, _, dropped_cv = with_audio(common_voice("test", args.cv_test, args.seed), processor.tokenizer)
    dropped += dropped_test + dropped_cv

    model = WhisperForConditionalGeneration.from_pretrained(base, torch_dtype=torch.bfloat16).to(device)
    model.config.use_cache = False
    model.generation_config.forced_decoder_ids = None

    if train_long or test_long:
        print(f"Cutting {len(train_long) + len(test_long)} clips longer than {MAX_SECONDS} s ...", flush=True)
        pieces, unsure = split_long(train_long, model, processor, device, args.batch * 2)
        train_clips += pieces
        pieces, unsure_test = split_long(test_long, model, processor, device, args.batch * 2)
        test_fit += pieces
        dropped += unsure + unsure_test
    # One test set per kind of recording: the 🎤/Telegram clips, each recording device, Common Voice.
    test_sets = {}
    for clip in test_fit:
        test_sets.setdefault("owner" if clip["source"] in ("mic", "telegram", "reading") else clip["source"],
                             []).append(clip)
    test_sets["commonvoice"] = cv_test
    owner_minutes = sum(len(c["array"]) for c in train_clips if c["source"] != "commonvoice") / RATE / 60
    print(f"Train: {len(train_clips)} clips ({owner_minutes:.1f} min of the owner, {args.cv} Common Voice asked)"
          f" · test: " + ", ".join(f"{len(v)} {k}" for k, v in test_sets.items()) + f" · dropped {dropped}",
          flush=True)
    if not train_clips or not any(test_sets.values()):
        sys.exit("Not enough clips: record with voice_recorder.py (or add --cv/--cv-test for a dry run).")
    if owner_minutes and owner_minutes < 5:
        print("Under 5 minutes of your voice: expect little change; ~20+ minutes works better.", flush=True)

    print("Scoring the base model ...", flush=True)
    before = score(model, processor, test_sets, device, args.batch * 2)

    model = get_peft_model(model, LoraConfig(r=args.rank, lora_alpha=args.rank * 2, lora_dropout=0.05,
                                             target_modules=["q_proj", "k_proj", "v_proj", "out_proj"]))
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    total = sum(p.numel() for p in model.parameters())
    print(f"Training {trainable / 1e6:.1f}M of {total / 1e6:.0f}M weights ...", flush=True)
    stats = train(model, processor, train_clips, device, args)
    model.config.use_cache = True

    print("Scoring the tuned model ...", flush=True)
    after = score(model, processor, test_sets, device, args.batch * 2)

    work = paths.voice_home() / "training" / args.name
    work.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(work / "lora")
    report = {"name": args.name, "base": args.base, "args": vars(args), "train_clips": len(train_clips),
              "owner_train_minutes": round(owner_minutes, 1), "training": stats, "model": None,
              "peak_vram_gb": round(torch.cuda.max_memory_allocated() / 1e9, 2) if device == "cuda" else None,
              "before": before, "after": after}
    report_path = work / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\nWER (lower is better)            before -> after")
    for name in before:
        b, a = before[name], after[name]
        print(f"  {name:18s} {b['wer']}% -> {a['wer']}%   ignoring numbers "
              f"{b['wer_ignoring_numbers']}% -> {a['wer_ignoring_numbers']}%   ({b['clips']} clips)")
    print(f"Training: {stats}, peak VRAM {report['peak_vram_gb']} GB", flush=True)

    print("\nMerging and converting for Umay ...", flush=True)
    target = export(model, processor, args.name, args.keep_hf)
    report["model"] = str(target)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Model: {target}\nReport: {report_path}")
    print(f"To use it, put this in Umay's .env and restart her:\nUMAY_STT_MODEL={args.name}")


if __name__ == "__main__":
    main()
