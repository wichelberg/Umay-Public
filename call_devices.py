"""Windows' default communication devices during Umay's WhatsApp calls (calls.py).

WhatsApp Beta's call engine plays and records on the default *communication* devices, whatever its own
"Video & voice" settings say (2026-11-02: set to CABLE Output and Realtek, a call still played on the
monitor, LG FULL HD, and took the laptop's microphone, so the owner heard the room and never her). Per-app
devices (Windows' volume mixer) took the speakers but refused every microphone.

So for a call: the communication speakers become the default speakers (the ones Umay hears the call from,
call_audio's loopback) and the communication microphone becomes CABLE Output (her voice). Before the call
starts, since the engine picks its devices then. Afterwards the owner's devices come back; they are kept in
%LOCALAPPDATA%\\Umay\\call_devices.json meanwhile, so a crash mid-call is undone when Umay starts again.
"""
import ctypes
import json
import os
from pathlib import Path

import call_audio

RENDER, CAPTURE = 0, 1
CONSOLE, COMMUNICATIONS = 0, 2
POLICY_CONFIG = "{870af99c-171d-4f9e-af0d-e63df40c2bc9}"   # PolicyConfigClient (undocumented, used by
I_POLICY_CONFIG = "{f8679f50-850a-41cf-9c72-430f290290c8}"  # every default-device switcher since Windows 7)
SET_DEFAULT_ENDPOINT = 13                                   # its vtable slot


def _saved_path():
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Umay" / "call_devices.json"


def _enumerator():
    import comtypes
    from pycaw.constants import CLSID_MMDeviceEnumerator
    from pycaw.pycaw import IMMDeviceEnumerator

    try:
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    except OSError:
        pass
    return comtypes.CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_INPROC_SERVER)


def _default(flow, role):
    return _enumerator().GetDefaultAudioEndpoint(flow, role).GetId()


def _find(name, flow):
    from pycaw.pycaw import AudioUtilities

    found = _enumerator().EnumAudioEndpoints(flow, 1)  # active ones
    for i in range(found.GetCount()):
        device = found.Item(i)
        if name.lower() in AudioUtilities.CreateDevice(device).FriendlyName.lower():
            return device.GetId()
    raise RuntimeError(f"no audio device called {name!r}")


def _set_default(device_id, role):
    import comtypes

    clsid, iid, ptr = comtypes.GUID(POLICY_CONFIG), comtypes.GUID(I_POLICY_CONFIG), ctypes.c_void_p()
    ctypes.oledll.ole32.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(ptr))
    vtable = ctypes.cast(ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
    try:
        ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int)(
            vtable[SET_DEFAULT_ENDPOINT])(ptr, device_id, role)
    finally:
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])(ptr)  # Release


def use_for_call():
    """The communication devices become the speakers and CABLE Output (the owner's are saved first)."""
    path = _saved_path()
    if not path.exists():  # else a crashed call's switch: what's saved is still the owner's
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"render": _default(RENDER, COMMUNICATIONS),
                                    "capture": _default(CAPTURE, COMMUNICATIONS)}), encoding="utf-8")
    _set_default(_default(RENDER, CONSOLE), COMMUNICATIONS)
    _set_default(_find(call_audio.CABLE_EAR, CAPTURE), COMMUNICATIONS)


def restore():
    """The owner's communication devices again (nothing to do if none are saved). True if it restored them."""
    path = _saved_path()
    if not path.exists():
        return False
    saved = json.loads(path.read_text(encoding="utf-8"))
    for key in ("render", "capture"):
        if saved.get(key):
            _set_default(saved[key], COMMUNICATIONS)
    path.unlink()
    return True
