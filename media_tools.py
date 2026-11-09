"""Tools for media playback and the PC's sound volume.

Playback uses the keyboard media keys, which Windows routes to whatever is
playing (YouTube in Opera, Spotify, ...) without that window having to be in
front, so it works even when Umay can't focus the browser.
"""
import ctypes

from pycaw.pycaw import AudioUtilities

MEDIA_KEYS = {"play_pause": 0xB3, "next": 0xB0, "previous": 0xB1, "stop": 0xB2}
KEYEVENTF_KEYUP = 0x2

_user32 = ctypes.windll.user32


# What the local model says instead of "play_pause" (2026-11-02: "Pause the music" -> action "pause").
ACTION_ALIASES = {"pause": "play_pause", "play": "play_pause", "resume": "play_pause"}


def media_key(action: str) -> str:
    action = ACTION_ALIASES.get(action, action)
    vk = MEDIA_KEYS.get(action)
    if vk is None:
        return f"Unknown action. Use one of: {', '.join(MEDIA_KEYS)}."
    _user32.keybd_event(vk, 0, 0, 0)
    _user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    return (
        f"Pressed the {action} media key. This can't confirm the result; "
        "use look_at_screen if you need to check it."
    )


def _endpoint():
    return AudioUtilities.GetSpeakers().EndpointVolume


def set_volume(level: int = None, change: int = None, mute: bool = None) -> str:
    try:
        volume = _endpoint()
        if level is not None:
            volume.SetMasterVolumeLevelScalar(max(0, min(100, int(level))) / 100, None)
        if change is not None:
            current = volume.GetMasterVolumeLevelScalar() * 100
            volume.SetMasterVolumeLevelScalar(max(0, min(100, current + int(change))) / 100, None)
        if mute is not None:
            volume.SetMute(1 if mute else 0, None)
        now = round(volume.GetMasterVolumeLevelScalar() * 100)
        return f"Volume is {now}%" + (" (muted)." if volume.GetMute() else ".")
    except Exception as e:  # no audio device, COM errors
        return f"Couldn't change the volume: {e}"


def _tool(name, description, properties, required):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


TOOLS = [
    _tool(
        "media_key",
        "Presses a keyboard media key: play_pause (pause/resume whatever is playing, e.g. YouTube or Spotify), "
        "next (the next track; never for skipping seconds inside a song), previous, stop. Works without the "
        "player being in front. Prefer this for pausing/resuming "
        "YouTube or an unnamed player; for Spotify use the spotify tool (it also checks the result).",
        {"action": {"type": "string", "enum": list(MEDIA_KEYS)}},
        ["action"],
    ),
    _tool(
        "set_volume",
        "Reads or changes the PC's master volume (every app at once). Call with no arguments to read it. "
        "Not for one app: Spotify's own volume is the spotify tool (action volume). "
        "level = set to that percent; change = relative step (e.g. -10); mute = true/false.",
        {
            "level": {"type": "integer", "description": "0-100"},
            "change": {"type": "integer", "description": "Percent points to add (negative = quieter)."},
            "mute": {"type": "boolean"},
        },
        [],
    ),
]

TOOL_FUNCTIONS = {"media_key": media_key, "set_volume": set_volume}
