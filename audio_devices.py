"""Which microphone Umay hears and which speakers or headphones her voice plays on: the owner's choice in the
Ctrl+F3 panel (hud_audio.py), kept in %LOCALAPPDATA%\\Umay\\audio.json (UMAY_AUDIO_FILE).

A device is kept by its full name as Windows shows it (WASAPI's list: the devices that are there right now,
with their whole names), never by number: the numbers change when something is plugged in. Streams are opened
on the MME device of that name, since MME converts the sample rate and WASAPI's shared mode won't (Umay records
at 16 kHz). MME cuts names at 31 characters, so a name is matched by its start there.

No choice = Windows' default. For the microphone that is MME's "Microsoft Sound Mapper - Input", which goes to
whatever Windows calls the default when a stream opens. PortAudio's own default is the one from when it
started (Umay's start), so a headset plugged in later was never heard before 2026-11-11. With no choice her
voice plays through winsound, which follows Windows too.

PortAudio reads the device list once. refresh() reads it again, but it ends every open stream, so it is called
only with none of ours open (wake.reopen).

Virtual cables (VB-Audio: the WhatsApp call's route, calls.py) are left out of the lists.
No numpy here: the HUD's process imports this module (docs/pitfalls.md, "numpy in the HUD's process").
"""
import json
import os
import threading
from pathlib import Path

INPUT, OUTPUT = "input", "output"
VIRTUAL = ("vb-audio",)
MAPPER = {INPUT: "Microsoft Sound Mapper - Input", OUTPUT: "Microsoft Sound Mapper - Output"}
MME_CHARS = 31            # MME's names are cut at this many characters
OPEN_ORDER = {"MME": 0, "Windows DirectSound": 1}   # APIs that convert the sample rate, the best first

_lock = threading.Lock()


def path():
    return Path(os.environ.get("UMAY_AUDIO_FILE", "").strip()
                or Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Umay" / "audio.json")


def load():
    """{"input": name or None, "output": name or None}: the owner's choice (None: Windows' default)."""
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {kind: (str(data[kind]) if data.get(kind) else None) for kind in (INPUT, OUTPUT)}


def chosen(kind):
    return load()[kind]


def choose(kind, name):
    """Keeps `name` as the device for `kind` (INPUT / OUTPUT); None or "" goes back to Windows' default."""
    if kind not in (INPUT, OUTPUT):
        raise ValueError(f"no such kind of device: {kind!r}")
    with _lock:
        data = load()
        data[kind] = name or None
        target = path()
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_suffix(".tmp")
        part.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(part, target)


def _sd(sd=None):
    if sd is not None:
        return sd
    import sounddevice
    return sounddevice


def _channels(kind):
    return "max_input_channels" if kind == INPUT else "max_output_channels"


def virtual(name):
    return any(v in str(name).lower() for v in VIRTUAL)


def listing(sd=None):
    """What the panel shows: {"inputs": [...], "outputs": [...], "default_input": name or None, "default_output": ...}:
    the devices Windows has now, by their whole names (WASAPI's list; MME's cut names when there is no WASAPI),
    virtual cables left out."""
    sd = _sd(sd)
    apis, devices = sd.query_hostapis(), sd.query_devices()
    api = next((i for i, a in enumerate(apis) if "WASAPI" in a["name"]), None)
    if api is None:
        api = next((i for i, a in enumerate(apis) if a["name"] == "MME"), 0)
    out = {}
    for kind in (INPUT, OUTPUT):
        names = []
        for d in devices:
            name = d["name"]
            if d["hostapi"] == api and d[_channels(kind)] > 0 and not virtual(name) \
                    and name not in names and name not in MAPPER.values():
                names.append(name)
        out[f"{kind}s"] = names
        default = apis[api].get(f"default_{kind}_device", -1)
        out[f"default_{kind}"] = devices[default]["name"] if 0 <= default < len(devices) else None
    return out


def index(name, kind, sd=None):
    """The device number to open `name` on (its whole name, or MME's cut one): MME first, then DirectSound,
    which convert the sample rate, then any other by its exact name. None when it isn't there."""
    if not name:
        return None
    sd = _sd(sd)
    apis = [a["name"] for a in sd.query_hostapis()]
    found = []
    for i, d in enumerate(sd.query_devices()):
        if d[_channels(kind)] <= 0:
            continue
        api = apis[d["hostapi"]]
        cut = api == "MME" and len(d["name"]) >= MME_CHARS and name.startswith(d["name"])
        if d["name"] == name or cut:
            found.append((OPEN_ORDER.get(api, len(OPEN_ORDER)), i))
    return min(found)[1] if found else None


def input_device(sd=None):
    """(the device number a microphone stream opens, what it is called): the chosen microphone, else Windows'
    default through MME's mapper; (None, ...) when neither is found, which is PortAudio's own default. A device
    list that can't be read is PortAudio's default too: listening never stops over the choice."""
    sd = _sd(sd)
    name = chosen(INPUT)
    try:
        if name:
            number = index(name, INPUT, sd)
            if number is not None:
                return number, name
        number = index(MAPPER[INPUT], INPUT, sd)
    except Exception:
        number = None
    return number, ("Windows default" + (f" ({name} isn't connected)" if name else ""))


def output_device(sd=None):
    """(the device number her voice plays on, its name), or (None, None) for Windows' default (winsound). A choice
    that isn't connected is (None, None) too."""
    name = chosen(OUTPUT)
    if not name:
        return None, None
    number = index(name, OUTPUT, _sd(sd))
    return (number, name) if number is not None else (None, None)


def refresh(sd=None):
    """PortAudio reads the device list again (a headset plugged in since it started). It ends every open stream:
    call it only with none of ours open."""
    sd = _sd(sd)
    try:
        sd.stop()      # sounddevice.play's stream, if one is left
    except Exception:
        pass
    sd._terminate()
    sd._initialize()
