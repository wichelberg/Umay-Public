"""Umay knows the owner's face: look() tells whether they are in front of the laptop's camera (the morning
greeting, companion.py), learn_my_face() learns it ("Umay, learn my face": from the camera, or from photos in a
folder). face_worker.py does both in hands_env (setup_voice_env.py --hands); the camera is on only while it
looks (~2-10 s). While the eyes are on the camera is theirs, so pictures from them (eyes.photo) are used.

Kept in voice_home: face_models\\ (YuNet and SFace from OpenCV's model zoo, ~38 MB, fetched the first time) and
face\\owner.npy (SFace's numbers of the owner's face, never a picture).
"""
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import requests

import eyes
import paths

ZOO = "https://github.com/opencv/opencv_zoo/raw/main/models/"
MODELS = {"face_detection_yunet_2023mar.onnx": ZOO + "face_detection_yunet/face_detection_yunet_2023mar.onnx",
          "face_recognition_sface_2021dec.onnx": ZOO + "face_recognition_sface/face_recognition_sface_2021dec.onnx"}
LOOK_TIMEOUT = 40
LEARN_TIMEOUT = 90
PHOTO_TYPES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
MAX_PHOTOS = 40
EYES_SHOTS = 10
EYES_GAP = 0.6  # seconds between the eyes' pictures: a little turn of the head each


def models_dir():
    return paths.voice_home() / "face_models"


def faces_file():
    return paths.voice_home() / "face" / "owner.npy"


def learned():
    return faces_file().is_file()


def _ensure_models():
    folder = models_dir()
    folder.mkdir(parents=True, exist_ok=True)
    for name, url in MODELS.items():
        target = folder / name
        if target.is_file() and target.stat().st_size > 100_000:
            continue
        part = target.with_suffix(".part")
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(part, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        part.replace(target)


def _run(args, timeout):
    """face_worker.py's answer (its JSON line). Raises RuntimeError with what went wrong."""
    if not eyes.available():
        raise RuntimeError("the camera's Python (hands_env) isn't set up: run setup_voice_env.py --hands")
    _ensure_models()
    try:
        out = subprocess.run([str(eyes.python()), "-X", "utf8", str(paths.resource_dir() / "face_worker.py"), *args],
                             capture_output=True, text=True, encoding="utf-8", timeout=timeout,
                             cwd=str(paths.resource_dir()), creationflags=subprocess.CREATE_NO_WINDOW).stdout
    except subprocess.TimeoutExpired:
        raise RuntimeError("the camera took too long")
    for line in reversed(out.splitlines()):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("event") == "error":
            raise RuntimeError(event.get("text") or "the camera failed")
        return event
    raise RuntimeError("the face check gave no answer")


def look():
    """{"faces": n, "owner": True / False / None (no face learned yet), "score": s}. Raises RuntimeError."""
    tail = [str(models_dir()), str(faces_file())]
    if not eyes.active():
        return _run(["--look", *tail], LOOK_TIMEOUT)
    picture = Path(tempfile.gettempdir()) / f"umay_face_{os.getpid()}_{int(time.time() * 1000)}.jpg"
    try:
        picture.write_bytes(eyes.photo())
        return _run(["--image", str(picture), *tail], LOOK_TIMEOUT)
    finally:
        picture.unlink(missing_ok=True)


def _photos_in(folder):
    found = [p for p in sorted(Path(folder).iterdir()) if p.suffix.lower() in PHOTO_TYPES]
    return found[:MAX_PHOTOS]


def _eyes_pictures(tmp):
    """EYES_SHOTS pictures from the running eyes (2026-11-03: "Hey Umay" had turned them on, so learning from
    the camera failed: they held it)."""
    pictures = []
    for n in range(EYES_SHOTS):
        path = tmp / f"eyes_{n}.jpg"
        path.write_bytes(eyes.photo())
        pictures.append(path)
        time.sleep(EYES_GAP)
    return pictures


def learn_my_face(photos_folder: str = "") -> str:
    """The tool: learns the owner's face from photos in a folder, else from the laptop's camera (through the
    eyes while they are on)."""
    tail = [str(models_dir()), str(faces_file())]
    try:
        if photos_folder.strip():
            folder = Path(os.path.expandvars(os.path.expanduser(photos_folder.strip().strip('"'))))
            if not folder.is_dir():
                return f"I couldn't learn your face: there's no folder {folder}."
            photos = _photos_in(folder)
            if not photos:
                return f"I couldn't learn your face: there are no photos (jpg, png, webp) in {folder}."
            event = _run(["--learn-images", *tail, *map(str, photos)], LEARN_TIMEOUT)
        elif eyes.active():
            with tempfile.TemporaryDirectory() as tmp:
                event = _run(["--learn-images", *tail, *map(str, _eyes_pictures(Path(tmp)))], LEARN_TIMEOUT)
        else:
            event = _run(["--learn", *tail], LEARN_TIMEOUT)
    except RuntimeError as e:
        return f"I couldn't learn your face: {e}."
    return (f"I learned your face from {event.get('shots')} pictures. Only numbers describing it are kept, "
            "no photo. I'll look for you when you sit down in the morning.")


TOOLS = [{
    "type": "function",
    "function": {
        "name": "learn_my_face",
        "description": (
            "Learns the user's face from photos in a folder if they give one, else from the laptop's camera (about "
            "10 seconds; they should sit in front of it, alone, and turn their head a little). ONLY when the "
            "user asks you to learn or recognize their face. Tell them exactly what it returns."),
        "parameters": {"type": "object", "properties": {
            "photos_folder": {"type": "string", "description": "A folder of photos of the user's face, if given."},
        }, "required": []},
    },
}]
TOOL_FUNCTIONS = {"learn_my_face": learn_my_face}
