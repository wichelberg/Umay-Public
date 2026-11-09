"""Tools that let Umay open sites and YouTube videos in Opera GX.

This is the "light" approach: Umay only hands Opera URLs (it opens them in a new tab of
the running window, or starts first). Everything inside pages, the video's loop, volume and
mute included, goes through the Umay Web Control extension (web_control.py). The one key
press left here is YouTube's own "f" for fullscreen (press_fullscreen): browsers allow
fullscreen only after a real key press or click, which an extension can't make.

Opening pages doesn't change anything on the PC, so unlike pc_tools these don't
ask for confirmation.
"""
import ctypes
import ctypes.wintypes as wintypes
import re
import subprocess
import time
import winreg
from collections import Counter
from urllib.parse import quote_plus, urlparse

from pynput.keyboard import Controller

OPERA_REG_KEY = r"Software\Clients\StartMenuInternet\Opera GXStable\shell\open\command"
OPERA_PROCESS = "opera.exe"
YOUTUBE_RESULTS = 10
FOCUS_WAIT_SECONDS = 0.4
FULLSCREEN_KEY = ("f",)  # YouTube's own shortcut (no extension involved)

_user32 = ctypes.windll.user32
_keyboard = Controller()
_last_results = []  # [(title, url, entry)] from the latest search_youtube
_request = {"text": ""}  # what the owner wrote in this request, in lower case
HANDLE = re.compile(r"youtube\.com/(@[^/?#\s]+)", re.IGNORECASE)
LATEST = re.compile(r"\b(latest|newest|most recent|last (video|upload)|en son|son video)", re.IGNORECASE)
CHANNEL_WORD = re.compile(r"\b(channel|kanal)", re.IGNORECASE)


class ToolError(Exception):
    pass


def _opera_exe():
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, OPERA_REG_KEY) as key:
                command = winreg.QueryValueEx(key, "")[0]
        except OSError:
            continue
        return command.split('"')[1] if command.startswith('"') else command.split()[0]
    raise ToolError("Opera GX doesn't seem to be installed.")


def _open(url):
    """Opens url in Opera GX: a new tab if it's running, otherwise it starts with it."""
    if urlparse(url).scheme not in ("http", "https"):
        raise ToolError("Only http(s) web addresses can be opened.")
    subprocess.Popen([_opera_exe(), url])


def _run(fn):
    try:
        return fn()
    except ToolError as e:
        return str(e)
    except OSError as e:
        return f"Failed: {e}"


def new_request(text):
    """A new request from the owner: an address written in it is theirs, and is opened as written."""
    _request["text"] = (text or "").lower()


def _flat(address):
    """yt-dlp's flat list for a search ("ytsearch10:...") or a channel page; raises ToolError when YouTube can't be read."""
    import yt_dlp  # heavy; only load when actually searching

    options = {"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True, "playlistend": YOUTUBE_RESULTS}
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(address, download=False)
    except Exception as e:  # yt-dlp's own errors: no network, a removed channel
        raise ToolError(f"Couldn't read YouTube: {type(e).__name__}.") from e
    return [entry for entry in info.get("entries") or [] if entry.get("id")]


def _key(name):
    return re.sub(r"[\W_]", "", (name or "").lower())


def _main_channel(entries):
    """The entry of the channel that fills more than half of the list, or None. "His channel" means that one: the 4b
    picked a fan channel with his name in the title 4 times in 32 (2026-11-09)."""
    names = Counter(entry.get("channel") for entry in entries if entry.get("channel"))
    if not names:
        return None
    name, count = names.most_common(1)[0]
    if count * 2 <= len(entries):
        return None
    return next(entry for entry in entries if entry.get("channel") == name)


def _named(entries, name):
    key = _key(name)
    return next((entry for entry in entries if key and key in
                 (_key(entry.get("channel")), _key(entry.get("uploader")), _key(entry.get("uploader_id")))), None)


def _find_channel(name):
    """An entry from the channel meant, or None: `name`, or with none the channel of the list just shown. YouTube says
    who is who: an address the model writes itself can be someone else (youtube.com/@SomeStreamer is "Ohcase", 2026-11-09)."""
    shown = [entry for _, _, entry in _last_results]
    if not name:
        return _main_channel(shown)
    found = _named(shown, name)
    if found:
        return found
    results = _flat(f"ytsearch{YOUTUBE_RESULTS}:{name}")
    return _named(results, name) or _main_channel(results)


def _address(entry):
    return entry.get("uploader_url") or entry.get("channel_url")


def _checked(url):
    """The address to open: a YouTube @address the owner didn't write must be proven, else it isn't opened."""
    handle = HANDLE.search(url)
    if not handle or handle.group(1).lower() in _request["text"]:
        return url
    entry = _find_channel(handle.group(1)[1:])
    if not entry or not _address(entry):
        raise ToolError(f"Not opened: I can't confirm {handle.group(1)} is the right channel. Use open_youtube_channel "
                        "with the name, or ask the user.")
    return _address(entry)


def open_in_browser(url: str) -> str:
    def run():
        address = _checked(url)
        _open(address)
        return f"Opened {address} in Opera GX."
    return _run(run)


def _wants_latest():
    """The owner asked for the newest video, whichever tool the 4b chose: right after a list it opened the channel
    or result 1 (7 + 1 times in 8, 2026-11-09)."""
    return bool(LATEST.search(_request["text"])) and not CHANNEL_WORD.search(_request["text"])


def _channel_entry(name):
    entry = _find_channel((name or "").strip())
    if not entry or not _address(entry):
        raise ToolError(f"I couldn't tell which YouTube channel {name or 'that is'} is. Ask the user for the name.")
    return entry


def open_youtube_channel(name: str = "") -> str:
    def run():
        if _wants_latest():
            return open_latest_youtube_video(name)
        entry = _channel_entry(name)
        _open(_address(entry))
        return f"Opened the YouTube channel {entry.get('channel') or name}."
    return _run(run)


def open_latest_youtube_video(name: str = "") -> str:
    """The channel's own upload order: a search sorts by relevance, so its first result isn't the newest video."""
    def run():
        entry = _channel_entry(name)
        who = entry.get("channel") or name
        videos = _flat(f"{_address(entry)}/videos")
        if not videos:
            raise ToolError(f"{who} has no videos I can read.")
        _open(f"https://www.youtube.com/watch?v={videos[0]['id']}")
        return f"Opened {who}'s latest video: {videos[0].get('title') or '?'}."
    return _run(run)


def search_youtube(query: str) -> str:
    def run():
        _open(f"https://www.youtube.com/results?search_query={quote_plus(query)}")
        entries = _flat(f"ytsearch{YOUTUBE_RESULTS}:{query}")
        _last_results.clear()
        for entry in entries:
            if entry.get("id"):
                _last_results.append((entry.get("title") or "?", f"https://www.youtube.com/watch?v={entry['id']}", entry))
        if not _last_results:
            return "Opened the search in Opera GX, but couldn't read any results."
        lines = []
        for i, (title, _, entry) in enumerate(_last_results, 1):
            extra = ", ".join(filter(None, [entry.get("channel"), _duration(entry.get("duration"))]))
            lines.append(f"{i}. {title}" + (f" ({extra})" if extra else ""))
        return (
            "Opened the search in Opera GX. Show the user this numbered list as-is "
            "(they pick by number with open_youtube_result):\n" + "\n".join(lines)
        )
    return _run(run)


def _duration(seconds):
    if not seconds:
        return ""
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{secs:02}" if hours else f"{minutes}:{secs:02}"


def open_youtube_result(number: int) -> str:
    def run():
        if _last_results and _wants_latest():
            return open_latest_youtube_video()
        if not _last_results:
            raise ToolError("There's no YouTube search yet. Call search_youtube first.")
        if not 1 <= int(number) <= len(_last_results):
            raise ToolError(f"Pick a number between 1 and {len(_last_results)}.")
        title, url, _ = _last_results[int(number) - 1]
        _open(url)
        return f"Opened #{number}: {title}"
    return _run(run)


# --- keyboard control of the Opera window ----------------------------------------

def _opera_window():
    """The topmost visible Opera GX main window, or None."""
    out = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {OPERA_PROCESS}", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW,
    ).stdout
    pids = {int(line.split('","')[1]) for line in out.splitlines() if line.startswith('"')}
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and _user32.IsWindowVisible(hwnd) and _user32.GetWindowTextLengthW(hwnd):
            if not _user32.GetWindow(hwnd, 4):  # GW_OWNER: skip popups/tooltips
                found.append(hwnd)
                return False  # EnumWindows goes top to bottom; the first one is the front-most
        return True

    _user32.EnumWindows(visit, 0)
    return found[0] if found else None


def _focus(hwnd):
    """Brings hwnd to the front. Windows only lets the foreground app do that, so a
    synthetic Alt press first makes this process count as having had input."""
    if _user32.IsIconic(hwnd):
        _user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    _user32.keybd_event(0x12, 0, 0, 0)  # Alt down
    _user32.keybd_event(0x12, 0, 2, 0)  # Alt up
    _user32.SetForegroundWindow(hwnd)
    time.sleep(FOCUS_WAIT_SECONDS)
    return _user32.GetForegroundWindow() == hwnd


def _press(keys):
    for k in keys:
        _keyboard.press(k)
    for k in reversed(keys):
        _keyboard.release(k)


def _focused_opera():
    hwnd = _opera_window()
    if hwnd is None:
        raise ToolError("Opera GX isn't open.")
    # Never type into another app: only press keys if Opera really is in front.
    if not _focus(hwnd):
        raise ToolError("Couldn't bring Opera GX to the front, so I didn't press anything.")
    return hwnd


def press_fullscreen() -> str:
    """YouTube's "f" in Opera GX's current tab (web_control.control_video's 'fullscreen')."""
    def run():
        _focused_opera()
        _press(FULLSCREEN_KEY)
        return ("Pressed YouTube's fullscreen key (f) in Opera GX's current tab: it toggles fullscreen. "
                "Use look_at_screen to check if needed.")
    return _run(run)


# --- tool schemas ------------------------------------------------------------------

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
        "open_in_browser",
        "Opens a web page in Opera GX (the user's browser): a new tab if Opera is running, otherwise it starts "
        "Opera with it. Always this for 'open <site>' or 'go to <site>', by name too: YouTube is https://www.youtube.com, Gmail is "
        "https://mail.google.com. Only if you don't know a site's address, find it with web_search first. Never guess "
        "a YouTube channel's @address: use open_youtube_channel.",
        {"url": {"type": "string", "description": "Full http(s) URL, e.g. https://www.youtube.com"}},
        ["url"],
    ),
    _tool(
        "search_youtube",
        "Searches YouTube: opens the results page in Opera GX and returns a numbered list of videos. "
        "Show the user that list so they can pick a video by number. Only to find videos: to just open YouTube, "
        "use open_in_browser; someone's channel: open_youtube_channel; their newest video: open_latest_youtube_video; "
        "to play one from a list already shown: open_youtube_result.",
        {"query": {"type": "string", "description": "What to search for on YouTube."}},
        ["query"],
    ),
    _tool(
        "open_youtube_result",
        "Opens a video from the latest search_youtube list in Opera GX, by its number in that list. Use when the "
        "user picks from that list ('the second one'); don't search again. Not for 'his latest video': that's "
        "open_latest_youtube_video.",
        {"number": {"type": "integer", "description": "The video's number in the list (1 = first)."}},
        ["number"],
    ),
    _tool(
        "open_youtube_channel",
        "Opens a YouTube channel's page in Opera GX. Use for 'open <name>'s channel', 'go to his channel'.",
        {"name": {"type": "string", "description": "The channel's name; empty = the channel of the list just shown."}},
        [],
    ),
    _tool(
        "open_latest_youtube_video",
        "Opens the newest video a YouTube channel uploaded, in Opera GX. Use for 'the latest video of <name>', 'his "
        "latest video', 'his newest upload'.",
        {"name": {"type": "string", "description": "The channel's name; empty = the channel of the list just shown."}},
        [],
    ),
]

TOOL_FUNCTIONS = {
    "open_in_browser": open_in_browser,
    "search_youtube": search_youtube,
    "open_youtube_result": open_youtube_result,
    "open_youtube_channel": open_youtube_channel,
    "open_latest_youtube_video": open_latest_youtube_video,
}
