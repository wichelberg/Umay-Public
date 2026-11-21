"""The keyboard's light follows Umay's state color (the owner's wish, 2026-11-21): the HUD's orb tells `show(hex)` the color
it shows, and this hands it to UmayLights.exe (lights_worker.py) as one line on its stdin, only when it changes.

UmayLights.exe lies in Apps\\Umay\\lights (next to Umay.exe, in "lights"): its sparse package (packaging/lights) is registered
with that folder as its external location, so the path must not change. Without the exe (not installed, or running from
source) nothing happens. When Umay closes, the pipe closes, the helper ends and Windows gets the light back.
UMAY_LIGHTS=off: never started. UMAY_LIGHTS_EXE: another exe (tests).
"""
import os
import subprocess
import sys
import time
from pathlib import Path

RESTARTS = 3          # a helper that keeps dying is left alone
RESTART_GAP_S = 30

_state = {"proc": None, "last": None, "starts": 0, "tried": 0.0}


def enabled():
    return os.environ.get("UMAY_LIGHTS", "on").strip().lower() not in ("off", "0", "false", "no")


def exe_path():
    given = os.environ.get("UMAY_LIGHTS_EXE", "").strip()
    if given:
        return Path(given)
    return Path(sys.executable).resolve().parent / "lights" / "UmayLights.exe"


def _start():
    now = time.monotonic()
    if _state["starts"] >= RESTARTS or now - _state["tried"] < RESTART_GAP_S:
        return None
    _state["tried"] = now
    exe = exe_path()
    if not exe.is_file():
        _state["starts"] = RESTARTS   # not installed: don't look again
        return None
    _state["starts"] += 1
    try:
        _state["proc"] = subprocess.Popen([str(exe)], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        _state["last"] = None
    except OSError:
        _state["proc"] = None
    return _state["proc"]


def show(color):
    """The keyboard in `color` ("#FF969D"); a repeat of the last color costs nothing."""
    if not enabled():
        return
    hexcolor = str(color or "").strip().lstrip("#").upper()
    if len(hexcolor) != 6 or hexcolor == _state["last"]:
        return
    proc = _state["proc"]
    if proc is None or proc.poll() is not None:
        proc = _start()
        if proc is None:
            return
    try:
        proc.stdin.write((hexcolor + "\n").encode("ascii"))
        proc.stdin.flush()
        _state["last"] = hexcolor
    except (OSError, ValueError):
        _state["proc"] = None


def stop():
    proc = _state["proc"]
    _state.update(proc=None, last=None)
    if proc is not None:
        try:
            proc.stdin.close()
        except (OSError, ValueError):
            pass
