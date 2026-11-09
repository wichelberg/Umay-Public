"""Whose face is in front of the laptop's camera: Umay's morning greeting (companion.py) looks for the owner's.
OpenCV's YuNet finds the faces and SFace turns each into 128 numbers, compared with the owner's (cosine
similarity); both are small ONNX models on the CPU. Runs in hands_env (its OpenCV), like hands_worker.py, whose
camera choice it uses (the laptop's own, never a phone's or a virtual one); the camera is on only while it looks.

- `face_worker.py --learn <models> <faces.npy>`: LEARN_SHOTS pictures with one face in them, a little apart in
  time (turning the head a little helps); only SFace's numbers are kept, never a picture.
- `face_worker.py --look <models> <faces.npy>`: looks for up to LOOK_SECONDS, stopping at the owner's face.
- `face_worker.py --image <file> <models> <faces.npy>`: the same for one picture (while the eyes are on, the
  camera is theirs: eyes.photo() takes it).
- `face_worker.py --learn-images <models> <faces.npy> <picture>...`: learns from pictures (photos the owner
  gave, or the eyes' pictures): in each, the biggest face; at least LEARN_LEAST_PICTURES of them.
Without a faces.npy any face counts (nothing learned yet).
stdout, one JSON line: {"event": "learned", "shots": n}, {"event": "seen", "faces": n, "owner": bool | null,
"score": s} (owner null: nothing learned), {"event": "error", "text": ...}.
"""
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np

DETECTOR = "face_detection_yunet_2023mar.onnx"
RECOGNIZER = "face_recognition_sface_2021dec.onnx"
MATCH = 0.363        # SFace's cosine threshold (OpenCV's own sample)
MIN_FACE = 60        # px: a smaller face is too far away to tell
LOOK_SECONDS = 8.0
LEARN_SHOTS = 12
LEARN_LEAST = 5
LEARN_LEAST_PICTURES = 3
LEARN_SECONDS = 30.0
SHOT_GAP = 0.5       # seconds between the pictures learned: a little turn of the head each


def say(obj):
    print(json.dumps(obj), flush=True)


class Faces:
    def __init__(self, models):
        self.detector = cv2.FaceDetectorYN.create(str(models / DETECTOR), "", (320, 320), 0.8, 0.3, 5000)
        self.recognizer = cv2.FaceRecognizerSF.create(str(models / RECOGNIZER), "")

    def find(self, frame):
        h, w = frame.shape[:2]
        self.detector.setInputSize((w, h))
        _, found = self.detector.detect(frame)
        return [f for f in (found if found is not None else []) if min(f[2], f[3]) >= MIN_FACE]

    def numbers(self, frame, face):
        return self.recognizer.feature(self.recognizer.alignCrop(frame, face)).flatten().astype(np.float32)


def similarity(numbers, known):
    """The best cosine similarity of a face's numbers to the learned ones (rows of `known`)."""
    a = numbers / (np.linalg.norm(numbers) + 1e-9)
    k = known / (np.linalg.norm(known, axis=1, keepdims=True) + 1e-9)
    return float(np.max(k @ a))


def judge(frames, faces, known):
    """{"faces", "owner", "score"} over the frames: stops at the owner (or, with nothing learned, any face)."""
    out = {"event": "seen", "faces": 0, "owner": None if known is None else False, "score": 0.0}
    for frame in frames:
        found = faces.find(frame)
        out["faces"] = max(out["faces"], len(found))
        if known is None:
            if found:
                break
            continue
        for face in found:
            out["score"] = round(max(out["score"], similarity(faces.numbers(frame, face), known)), 3)
            if out["score"] >= MATCH:
                out["owner"] = True
                return out
    return out


def camera_frames(seconds):
    import hands_worker  # its camera choice (it imports MediaPipe too: a second or two)

    cap, _ = hands_worker.open_camera()
    if cap is None:
        raise RuntimeError("the laptop's camera didn't answer (a phone's or a virtual camera is never used)")
    try:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            ok, frame = cap.read()
            if ok and frame is not None and frame.mean() > 3:
                yield frame
    finally:
        cap.release()


def learn(faces, path):
    shots, last = [], 0.0
    for frame in camera_frames(LEARN_SECONDS):
        if time.monotonic() - last < SHOT_GAP:
            continue
        found = faces.find(frame)
        if len(found) == 1:  # two faces: whose is whose isn't known
            shots.append(faces.numbers(frame, found[0]))
            last = time.monotonic()
        if len(shots) >= LEARN_SHOTS:
            break
    if len(shots) < LEARN_LEAST:
        raise RuntimeError(f"saw one face clearly in only {len(shots)} pictures; sit in front of the camera, "
                           "in some light, alone")
    path.parent.mkdir(parents=True, exist_ok=True)
    np.save(path, np.stack(shots))
    return {"event": "learned", "shots": len(shots)}


def learn_pictures(faces, path, pictures):
    shots = []
    for picture in pictures:
        frame = cv2.imdecode(np.fromfile(picture, np.uint8), cv2.IMREAD_COLOR)  # (imread can't read "ü" paths)
        found = faces.find(frame) if frame is not None else []
        if found:
            shots.append(faces.numbers(frame, max(found, key=lambda f: f[2] * f[3])))
    if len(shots) < LEARN_LEAST_PICTURES:
        raise RuntimeError(f"found a clear face in only {len(shots)} of {len(pictures)} pictures; it needs "
                           f"{LEARN_LEAST_PICTURES}, the face big enough and in some light")
    path.parent.mkdir(parents=True, exist_ok=True)
    np.save(path, np.stack(shots))
    return {"event": "learned", "shots": len(shots), "of": len(pictures)}


def main(argv):
    try:
        if argv[0] == "--learn-images":
            say(learn_pictures(Faces(Path(argv[1])), Path(argv[2]), argv[3:]))
            return
        if argv[0] == "--image":
            picture, models, known_path = argv[1], Path(argv[2]), Path(argv[3])
        else:
            models, known_path = Path(argv[1]), Path(argv[2])
        faces = Faces(models)
        if argv[0] == "--learn":
            say(learn(faces, known_path))
            return
        known = np.load(known_path) if known_path.is_file() else None
        frames = ([cv2.imdecode(np.fromfile(picture, np.uint8), cv2.IMREAD_COLOR)] if argv[0] == "--image"
                  else camera_frames(LOOK_SECONDS))
        say(judge((f for f in frames if f is not None), faces, known))
    except Exception as e:
        say({"event": "error", "text": str(e) or type(e).__name__})


if __name__ == "__main__":
    main(sys.argv[1:])
