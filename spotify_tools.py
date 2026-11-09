"""Spotify without an account (planned.txt 23a): the desktop app through Windows' media session.

Windows keeps a media session (SMTC, Windows.Media.Control) for every player that shows up in the
volume flyout; the Spotify app is one. Through it Umay can read what is playing (song, artist, album,
position, shuffle / repeat) and play, pause, skip, shuffle, repeat and seek, then read the state back,
so the answer says what really happened (media_tools.media_key can only press a key and hope).
Spotify's own volume is its audio session in the Windows mixer (pycaw), so "turn Spotify down"
leaves the PC's volume alone.

Finding and playing a given song, playlist or album, the queue, liking and playlists go through the
Spotify Web API (23b, spotify_web.py): the same tool, WEB_ACTIONS. Playback control is never confirmed (like video in web_control); if Spotify is closed, playing
opens it.
"""
import asyncio
import ctypes
import datetime
import os
import re
import threading
import time

SPOTIFY_APP = "SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify"  # the Microsoft Store app (Get-StartApps)
SPOTIFY_EXE = "spotify.exe"
OPEN_WAIT = 12.0  # seconds for a freshly opened Spotify to show its media session
VERIFY_WAIT = 2.0  # seconds to wait for a command to show in the session's state
WINRT_TIMEOUT = 8.0
RESTART_AFTER = 3.0  # seconds into a song after which Spotify's "previous" starts it over (tried 2026-11-04)

# GlobalSystemMediaTransportControlsSessionPlaybackStatus and MediaPlaybackAutoRepeatMode, as ints
# (the enums compare equal to their values).
STATUS = {0: "closed", 1: "opened", 2: "changing", 3: "stopped", 4: "playing", 5: "paused"}
REPEAT = {"off": 0, "track": 1, "all": 2}
REPEAT_NAME = {0: "off", 1: "one song", 2: "the whole list"}

VK_MEDIA_PLAY_PAUSE = 0xB3
KEYEVENTF_KEYUP = 0x2

LOCAL_ACTIONS = ("status", "play", "pause", "toggle", "next", "previous", "shuffle", "repeat", "seek", "volume",
                 "open")
WEB_ACTIONS = ("find", "queue", "like", "add_to_playlist", "playlists", "devices", "login")  # spotify_web (23b)
ACTIONS = LOCAL_ACTIONS + WEB_ACTIONS
# What the small model may say instead (it said "pause" for "play_pause" in media_key, 2026-11-02).
ACTION_ALIASES = {
    "now_playing": "status", "what_is_playing": "status", "info": "status", "state": "status",
    "resume": "play", "unpause": "play", "stop": "pause", "play_pause": "toggle", "skip": "next",
    "next_track": "next", "next_song": "next", "back": "previous", "prev": "previous",
    "previous_track": "previous", "previous_song": "previous", "restart": "previous", "rewind": "seek",
    "forward": "seek", "fast_forward": "seek", "jump": "seek", "launch": "open", "start": "open",
    "mute": "volume", "unmute": "volume",
    "search": "find", "play_search": "find", "find_and_play": "find", "play_song": "find",
    "play_playlist": "find", "add_to_queue": "queue", "enqueue": "queue", "save": "like", "heart": "like",
    "add": "add_to_playlist", "my_playlists": "playlists", "list_playlists": "playlists", "connect": "login",
}


# ---------- WinRT (async, in a thread of its own) ----------

def _run(make):
    """Runs the coroutine `make()` on a fresh event loop in its own thread: the caller may be a thread
    with a running loop or a Tk thread; WinRT's async calls need a loop of their own."""
    box = {}

    def work():
        try:
            box["value"] = asyncio.run(make())
        except BaseException as e:  # handed to the caller
            box["error"] = e

    worker = threading.Thread(target=work, name="spotify-winrt", daemon=True)
    worker.start()
    worker.join(WINRT_TIMEOUT)
    if worker.is_alive():
        raise TimeoutError("Windows' media session didn't answer")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _is_spotify(app_id):
    return "spotify" in (app_id or "").lower()


async def _session():
    from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Manager
    manager = await Manager.request_async()
    for session in manager.get_sessions():
        if _is_spotify(session.source_app_user_model_id):
            return session
    return None


def _seconds(value):
    """A WinRT TimeSpan / DateTime arrives as timedelta / datetime; None stays None."""
    if value is None:
        return None
    if isinstance(value, datetime.timedelta):
        return value.total_seconds()
    return float(value)


def position_now(position, last_updated, status, now=None):
    """Spotify updates the session's position only on events (a skip, a seek, a pause), so while it
    plays the real position is the stored one plus the time since then."""
    if position is None:
        return None
    if status != "playing" or not isinstance(last_updated, datetime.datetime):
        return position
    if last_updated.year < 2000:  # never set (WinRT's zero DateTime is 1601)
        return position
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if last_updated.tzinfo is None:
        last_updated = last_updated.replace(tzinfo=datetime.timezone.utc)
    return position + max(0.0, (now - last_updated).total_seconds())


async def _read(session):
    info = session.get_playback_info()
    status = STATUS.get(int(info.playback_status), "unknown") if info is not None else "unknown"
    state = {"status": status, "title": "", "artist": "", "album": "", "shuffle": None, "repeat": None,
             "position": None, "duration": None, "can_seek": False, "can_shuffle": False, "can_repeat": False}
    try:
        props = await session.try_get_media_properties_async()
        if props is not None:
            state.update(title=props.title or "", artist=props.artist or "", album=props.album_title or "")
    except Exception:  # Spotify between songs: no properties yet
        pass
    if info is not None:
        shuffle, repeat = info.is_shuffle_active, info.auto_repeat_mode
        state["shuffle"] = None if shuffle is None else bool(shuffle)
        state["repeat"] = None if repeat is None else int(repeat)
        controls = info.controls
        if controls is not None:
            state.update(can_seek=bool(controls.is_playback_position_enabled),
                         can_shuffle=bool(controls.is_shuffle_enabled),
                         can_repeat=bool(controls.is_repeat_enabled))
    timeline = session.get_timeline_properties()
    if timeline is not None:
        end = _seconds(timeline.end_time) or 0.0
        start = _seconds(timeline.start_time) or 0.0
        if end > start:
            state["duration"] = end - start
            pos = position_now(_seconds(timeline.position), timeline.last_updated_time, status)
            state["position"] = None if pos is None else max(0.0, min(pos, end) - start)
    return state


def _state():
    """Spotify's state as a dict, or None when Spotify has no media session (closed)."""
    async def go():
        session = await _session()
        return None if session is None else await _read(session)
    return _run(go)


def _command(name, *args):
    """Sends one SMTC command to Spotify; True if Spotify accepted it, None if there is no session."""
    async def go():
        session = await _session()
        if session is None:
            return None
        return bool(await getattr(session, name)(*args))
    return _run(go)


# ---------- answers ----------

def clock(seconds):
    seconds = int(round(seconds or 0))
    hours, rest = divmod(seconds, 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60}:{rest % 60:02d}"


def describe(state, volume=None):
    """One or two sentences for the model: what is playing and how."""
    if state is None:
        return "Spotify isn't open (or hasn't played anything since it opened)."
    song = f'"{state["title"]}"' if state["title"] else "an unknown song"
    if state["artist"]:
        song += f" by {state['artist']}"
    if state["album"] and state["album"] != state["title"]:
        song += f" ({state['album']})"
    verb = {"playing": "is playing", "paused": "is paused on", "stopped": "is stopped on"}.get(
        state["status"], f"is {state['status']} with")
    text = f"Spotify {verb} {song}"
    if state["duration"]:
        text += f", {clock(state['position'])} of {clock(state['duration'])}"
    text += "."
    extras = []
    if state["shuffle"] is not None:
        extras.append("shuffle " + ("on" if state["shuffle"] else "off"))
    if state["repeat"] is not None:
        extras.append("repeat " + REPEAT_NAME.get(state["repeat"], "off"))
    if volume:
        extras.append(volume)
    if extras:
        text += " " + ", ".join(extras).capitalize() + "."
    return text


def _wait_for(check, before=None):
    """Re-reads Spotify's state until `check(state)` holds or VERIFY_WAIT passes; returns the last state."""
    deadline = time.monotonic() + VERIFY_WAIT
    state = before
    while True:
        time.sleep(0.25)
        state = _state()
        if state is not None and check(state):
            return state, True
        if time.monotonic() >= deadline:
            return state, False


# ---------- Spotify's own volume (the Windows mixer) ----------

def _audio_sessions():
    from pycaw.pycaw import AudioUtilities
    return [s for s in AudioUtilities.GetAllSessions()
            if s.Process is not None and s.Process.name().lower() == SPOTIFY_EXE]


def volume_text():
    """'Spotify volume 80%' (+ muted), or '' when Spotify has no audio session."""
    try:
        sessions = _audio_sessions()
        if not sessions:
            return ""
        audio = sessions[0].SimpleAudioVolume
        return f"Spotify volume {round(audio.GetMasterVolume() * 100)}%" + (" (muted)" if audio.GetMute() else "")
    except Exception:
        return ""


def set_spotify_volume(level=None, change=None, mute=None):
    try:
        sessions = _audio_sessions()
    except Exception as e:
        return f"Couldn't reach the Windows mixer: {e}"
    if not sessions:
        return ("Spotify has no sound channel in the Windows mixer yet: it must be open and have played "
                "something. The PC's own volume is set_volume.")
    for session in sessions:  # Spotify can hold more than one
        audio = session.SimpleAudioVolume
        if level is not None:
            audio.SetMasterVolume(max(0, min(100, int(level))) / 100, None)
        if change is not None:
            now = audio.GetMasterVolume() * 100
            audio.SetMasterVolume(max(0, min(100, now + int(change))) / 100, None)
        if mute is not None:
            audio.SetMute(1 if mute else 0, None)
    return volume_text().replace("Spotify volume", "Spotify's volume is", 1) + ". (The PC's volume didn't change.)"


# ---------- opening ----------

def _open_app():
    os.startfile("shell:AppsFolder\\" + SPOTIFY_APP)


def _press_play_pause():
    user32 = ctypes.windll.user32
    user32.keybd_event(VK_MEDIA_PLAY_PAUSE, 0, 0, 0)
    user32.keybd_event(VK_MEDIA_PLAY_PAUSE, 0, KEYEVENTF_KEYUP, 0)


def _open_and_wait():
    """Opens Spotify and waits for its media session; the state, or None if it never showed one."""
    _open_app()
    deadline = time.monotonic() + OPEN_WAIT
    while time.monotonic() < deadline:
        time.sleep(0.5)
        try:
            state = _state()
        except Exception:
            state = None
        if state is not None:
            return state
    return None


# ---------- the tool ----------

def log_file():
    return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Umay", "spotify.log")


def _log(args, answer):
    """One line per call: what the model asked and what it got back (to see why a spoken request failed)."""
    try:
        path = log_file()
        if os.path.exists(path) and os.path.getsize(path) > 512_000:
            os.replace(path, path + ".old")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {args!r} -> {str(answer)[:400]!r}\n")
    except OSError:
        pass


def spotify(action="status", **kwargs):
    """The tool. Arguments it doesn't know (the small model makes some up) are dropped, not an error."""
    known = {k: v for k, v in kwargs.items() if k in PARAMETERS}
    extra = sorted(set(kwargs) - set(known))
    try:
        answer = _spotify(action, **known)
    except Exception as e:  # an import, WinRT / COM, the network: always a sentence for the model
        answer = f"Couldn't control Spotify ({type(e).__name__}): {e}"
    _log(dict(action=action, **kwargs), answer)
    if extra:
        answer += f" (Ignored unknown arguments: {', '.join(extra)}.)"
    return answer


def _spotify(action="status", on=None, mode=None, seconds=None, to=None, level=None, change=None, mute=None,
             query=None, kind=None, playlist=None, device=None):
    action = (action or "status").strip().lower().replace(" ", "_").replace("-", "_")
    raw = action
    action = ACTION_ALIASES.get(action, action)
    if action in ("play", "open", "toggle", "next", "status") and (
            query or (kind in ("liked", "mine") and raw != "resume")):
        action = "find"  # "play Tarkan", "open relaxing coding music": the model may put the name on play / open
    if action in WEB_ACTIONS:
        import spotify_web
        if action == "find" and not query and playlist:
            query, kind = playlist, kind or "mine"
        return spotify_web.run(action, query=query, kind=kind, playlist=playlist, device=device)
    if raw == "mute" and mute is None:
        mute = True
    if raw == "unmute" and mute is None:
        mute = False
    if raw == "rewind" and seconds is not None and to is None:
        seconds = -abs(int(seconds))
    if raw == "restart" and to is None:  # "start the song over"
        action, to = "seek", 0
    if action not in ACTIONS:
        return f"Unknown action {raw!r}. Use one of: {', '.join(ACTIONS)}."
    try:
        return _do(action, on, mode, seconds, to, level, change, mute)
    except Exception as e:  # WinRT / COM errors, a timeout
        return f"Couldn't control Spotify: {e}"


def _do(action, on, mode, seconds, to, level, change, mute):
    if action == "volume":
        if level is None and change is None and mute is None:
            now = volume_text()
            return now + "." if now else "Spotify has no sound channel in the mixer yet (it must have played something)."
        return set_spotify_volume(level, change, mute)

    state = _state()
    if action == "status":
        return describe(state, volume_text())

    if state is None:
        if action == "pause":
            return "Spotify isn't open, so nothing is playing there."
        if action not in ("play", "toggle", "open", "next", "previous"):
            return "Spotify isn't open. Say play first and I'll open it."
        state = _open_and_wait()
        if state is None:
            return ("I opened Spotify, but it hasn't come up in Windows' media controls yet; "
                    "press play in it once, then I can control it.")
        if action == "open":
            return "Opened Spotify. " + describe(state)
        if state["status"] != "playing" and action in ("play", "toggle"):
            if not _command("try_play_async"):
                _press_play_pause()
            state, ok = _wait_for(lambda s: s["status"] == "playing", state)
            return ("Opened Spotify and started it. " if ok else "Opened Spotify, but it didn't start playing. ") \
                + describe(state)

    if action == "open":
        return "Spotify is already open. " + describe(state)

    if action in ("play", "pause", "toggle"):
        want = {"play": "playing", "pause": "paused"}.get(action)
        if action == "toggle":
            want = "paused" if state["status"] == "playing" else "playing"
        if state["status"] == want:
            return "Already " + ("playing. " if want == "playing" else "paused. ") + describe(state)
        _command("try_play_async" if want == "playing" else "try_pause_async")
        state, ok = _wait_for(lambda s: s["status"] == want, state)
        return ("" if ok else "Spotify didn't respond. ") + describe(state)

    if action in ("next", "previous"):
        before = (state["title"], state["artist"])
        if not _command("try_skip_next_async" if action == "next" else "try_skip_previous_async"):
            return "Spotify refused to skip. " + describe(state)
        # "previous" past a song's first seconds starts it over (the title stays, the position drops);
        # in the first seconds it goes back a song. Spotify plays after a skip even if it was paused.
        restarts = action == "previous" and (state["position"] or 0) >= RESTART_AFTER
        state, ok = _wait_for(lambda s: (s["title"], s["artist"]) != before
                              or (restarts and s["position"] is not None and s["position"] < RESTART_AFTER), state)
        if restarts and ok and state and (state["title"], state["artist"]) == before:
            return "Started the song over (Spotify goes back a song only near its start; ask again). " \
                + describe(state)
        return ("" if ok else "Spotify didn't change the song. ") + describe(state)

    if action == "shuffle":
        target = (not state["shuffle"]) if on is None else bool(on)
        if not state["can_shuffle"]:
            return "Spotify doesn't allow shuffle for what is playing now. " + describe(state)
        _command("try_change_shuffle_active_async", target)
        state, ok = _wait_for(lambda s: s["shuffle"] == target, state)
        return ("" if ok else "Spotify didn't change shuffle. ") + describe(state)

    if action == "repeat":
        if mode is None:  # cycle like the button: off -> all -> one song -> off
            mode = {0: "all", 2: "track", 1: "off"}.get(state["repeat"] or 0, "all")
        mode = {"one": "track", "song": "track", "single": "track", "list": "all", "playlist": "all",
                "context": "all", "none": "off"}.get(str(mode).lower(), str(mode).lower())
        if mode not in REPEAT:
            return "Repeat mode must be off, track or all."
        if not state["can_repeat"]:
            return "Spotify doesn't allow repeat for what is playing now. " + describe(state)
        from winrt.windows.media import MediaPlaybackAutoRepeatMode
        _command("try_change_auto_repeat_mode_async", MediaPlaybackAutoRepeatMode(REPEAT[mode]))
        state, ok = _wait_for(lambda s: s["repeat"] == REPEAT[mode], state)
        return ("" if ok else "Spotify didn't change repeat. ") + describe(state)

    # seek
    if seconds is None and to is None:
        return "Say how far: seconds (e.g. 60 or -30) or to (a point in the song, in seconds)."
    if not state["can_seek"] or not state["duration"]:
        return "Spotify doesn't allow seeking in what is playing now. " + describe(state)
    target = float(to) if to is not None else (state["position"] or 0) + float(seconds)
    target = max(0.0, min(target, state["duration"] - 1))
    _command("try_change_playback_position_async", int(target * 10_000_000))  # 100 ns ticks
    state, ok = _wait_for(lambda s: s["position"] is not None and abs(s["position"] - target) < 4, state)
    return ("" if ok else "Spotify didn't move. ") + describe(state)


# ---------- plain sentences, before the model ----------
# The 4b model got 5 of 14 spoken Spotify requests wrong (2026-11-04): "Spotify'da Tarkan çal" only read the
# status, "turn Spotify down to 40" changed the PC's volume, "play my liked songs" searched for a song. Plain
# sentences that name Spotify go straight to the tool, like markets_follow / news_follow.

SPOKEN = re.compile(r"\s*(?:\n\n)?\s*\[(?:This message|Bu mesaj).*\Z", re.S | re.I)
ADDRESS = re.compile(r"^\W*(?:(?:hey|ok|okay)\s+)?umay\b\W*", re.I)
POLITE = r"(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?"
SPOTIFY = r"spotify(?:['’]?\w*)?"  # Spotify'da, Spotify'dan, Spotify'ı
PLAY_EN = re.compile(rf"^{POLITE}(?:play|put\s+on|start|open)\s+(?P<q>.+?)\s+(?:on|in|from|with)\s+{SPOTIFY}\W*$",
                     re.I | re.S)
PLAY_EN2 = re.compile(rf"^{SPOTIFY}\W+(?:play|open|put\s+on)\s+(?P<q>.+?)\W*$", re.I | re.S)
# the verb only as a request: "çal", "çalar mısın", "çalsana", "aç", "açabilir misin" -- not "ne çalıyor?"
PLAY_TR = re.compile(rf"^(?P<pre>.*?)\b{SPOTIFY}\s*(?P<post>.*?)\s*\b(?:çal|aç|oynat|başlat)"
                     r"(?:sana|sın|ın|ar\s+m[ıi]s[ıi]n|abilir\s+m[ıi]s[ıi]n|ır\s+m[ıi]s[ıi]n)?"
                     r"(?:\s+(?:lütfen|hemen|şimdi|bakalım))?\W*$", re.I | re.S)
# playback words with Spotify named: the model called "open" for "Resume Spotify" (2026-11-04)
CONTROL = (
    (re.compile(rf"^{POLITE}(?:resume|unpause|continue)\s+(?:the\s+music\s+(?:on|in)\s+)?{SPOTIFY}\W*$|"
                rf"^{SPOTIFY}\s+(?:devam\s+et\w*|kald[ıi]ğ[ıi]\s+yerden\s+devam\w*)\W*$", re.I), "play"),
    (re.compile(rf"^{POLITE}(?:pause|stop)\s+{SPOTIFY}\W*$|^{SPOTIFY}\s+(?:durdur|duraklat)\w*\W*$", re.I), "pause"),
    (re.compile(rf"^{POLITE}(?:next|skip)\s+(?:song|track)?\s*(?:on|in)?\s*{SPOTIFY}\W*$|"
                rf"^{SPOTIFY}\s+(?:sonraki|bir\s+sonraki)\s+şarkı\w*(?:\s+geç\w*)?\W*$", re.I), "next"),
    (re.compile(rf"^{POLITE}(?:previous|last)\s+(?:song|track)?\s*(?:on|in)?\s*{SPOTIFY}\W*$|"
                rf"^{SPOTIFY}\s+(?:önceki)\s+şarkı\w*\W*$", re.I), "previous"),
    (re.compile(rf"^(?:what(?:'s|\s+is)\s+(?:playing|on)\s+(?:on|in)\s+{SPOTIFY}|"
                rf"{SPOTIFY}\s+(?:şu\s+an\s+)?ne\s+çal[ıi]yor|{SPOTIFY}\s+(?:şu\s+an\s+)?hangi\s+şarkı\s+çal[ıi]yor)\W*$",
                re.I), "status"),
)
VOLUME_EN = re.compile(rf"^{POLITE}(?:turn|set|put)\s+{SPOTIFY}(?:\s+volume)?\s+(?P<dir>down|up)?\s*"
                       r"(?:to\s+(?P<n>\d{1,3})\s*(?:%|percent)?)?\W*$", re.I)
VOLUME_TR = re.compile(rf"^{SPOTIFY}\s+(?:ses\w*\s+)?(?:(?:%\s*)?(?P<n>\d{{1,3}})\s*(?:['’]?\w+\s+)?"
                       r"(?:yap|indir|düşür|ayarla|çıkar)\w*|(?P<dir>kıs|azalt|yükselt|artır|arttır)\w*)\W*$", re.I)
VAGUE = re.compile(r"^(?:music|some\s+music|something|müzik|bir\s+şey(?:ler)?|bişey|şarkı|bir\s+şarkı)$", re.I)
TR_SUFFIX = re.compile(r"['’](?:\w+)")  # Tarkan'ı, Şımarık'ı
FILLER = re.compile(r"(?<!\S)(?:lütfen|please|bana|bize|bir|biraz|da|de|şimdi|now|for\s+me|some)(?!\S)", re.I)


def _clean(text):
    return ADDRESS.sub("", SPOKEN.sub("", text or "")).strip()


def _query(raw):
    q = TR_SUFFIX.sub("", raw or "")
    q = re.sub(r"\s+", " ", FILLER.sub(" ", q)).strip(" ,.:;!?-\"'")
    return q


def _kind(q):
    """(query, kind) from the words: albums, playlists, "X music" / "X şarkıları" as a playlist search."""
    low = q.lower()
    for pattern, kind in ((r"\s*\b(?:album|albüm\w*)\b\s*", "album"),
                          (r"\s*\b(?:play\s*list\w*|çalma\s+liste\w*)\b\s*", "playlist")):
        if re.search(pattern, low, re.I):
            return re.sub(pattern, " ", q, flags=re.I).strip(), kind
    m = re.match(r"^(?P<what>.+?)\s+(?:music|müzi[kğ]\w*|şarkı\w*|songs)$", q, re.I)
    if m:
        return m.group("what"), "playlist"
    return q, "track"


def asked(text):
    """The spotify() arguments for a plain Spotify sentence, or None (the model handles it)."""
    text = _clean(text)
    if "spotify" not in text.lower() or len(text) > 120:
        return None
    for pattern, action in CONTROL:
        if pattern.match(text):
            return {"action": action}
    m = VOLUME_EN.match(text)
    if m and (m.group("n") or m.group("dir")):
        if m.group("n"):
            return {"action": "volume", "level": int(m.group("n"))}
        return {"action": "volume", "change": -20 if m.group("dir").lower() == "down" else 20}
    m = VOLUME_TR.match(text)
    if m:
        if m.group("n"):
            return {"action": "volume", "level": int(m.group("n"))}
        return {"action": "volume", "change": -20 if m.group("dir").lower() in ("kıs", "azalt") else 20}
    m = PLAY_EN.match(text) or PLAY_EN2.match(text)
    raw = m.group("q") if m else None
    if raw is None:
        m = PLAY_TR.match(text)
        if m:
            raw = f"{m.group('pre')} {m.group('post')}"
    if raw is None:
        return None
    q = _query(raw)
    if not q:  # "Spotify'ı aç": open it; "Spotify'ı çal": play
        return {"action": "open" if re.search(r"\b(?:aç|open)\w*", text, re.I) else "play"}
    if VAGUE.match(q):
        return {"action": "play"}
    import spotify_web
    if spotify_web.LIKED.search(spotify_web.fold(q)):
        return {"action": "find", "kind": "liked"}
    q = re.sub(r"\s+by\s+", " ", q, flags=re.I)  # "Şımarık by Tarkan" searches well as "Şımarık Tarkan"
    q = re.sub(r"^(?:a|an|the|some)\s+", "", q, flags=re.I)
    q, kind = _kind(q)
    return {"action": "find", "query": q, "kind": kind}


def spoken(answer):
    """The tool's answer as Umay's reply: the song, without device names and checks."""
    m = re.match(r"^Playing (?P<what>.+?) on [^.]+\. Now playing: (?P<now>.+?)\.$", answer or "")
    if m:
        what, now = m.group("what"), m.group("now")
        return f"Playing {what}, dear." if now in what else f"Playing {what}, dear. Now: {now}."
    m = re.match(r"^Playing (?P<what>.+?) on [^.]+\.", answer or "")
    if m:
        return f"I've asked Spotify to play {m.group('what')}, dear, but couldn't confirm it started."
    m = re.match(r"^Spotify's volume is (?P<n>\d+)%", answer or "")
    if m:
        return f"Spotify is at {m.group('n')}% now, dear."
    m = re.match(r"^(?:Opened Spotify and started it\. |Already (?:playing|paused)\. |Spotify is already open\. )?"
                 r"Spotify (?P<state>is playing|is paused on|is stopped on) (?P<what>\"[^\"]*\"(?: by [^(,.]+)?)",
                 answer or "")
    if m:
        what = m.group("what").strip()
        return f"Playing {what}, dear." if m.group("state") == "is playing" else \
            f"Spotify {m.group('state')} {what}, dear."
    return answer


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
        "spotify",
        "Controls the Spotify app on the PC and says what it is playing; checks the result. Use it whenever "
        "the user mentions Spotify, or asks what song is playing. To play something by name or mood (e.g. "
        "'relaxing coding music' -> action find, query 'relaxing coding', kind playlist): find with query and "
        "kind (track, artist, album, playlist, mine = their own playlists, liked = their liked songs). Other "
        "actions: status, play, pause, toggle, next, previous, shuffle, repeat, seek, volume (Spotify's own; the "
        "PC's is set_volume), open, queue (add a song after this one), like, add_to_playlist, playlists, "
        "devices, login (first time). Playing opens Spotify if it is closed. device = where to play (e.g. phone).",
        {
            "action": {"type": "string", "enum": list(ACTIONS)},
            "on": {"type": "boolean", "description": "shuffle: true = on, false = off; leave out to flip."},
            "mode": {"type": "string", "enum": list(REPEAT), "description": "repeat: off, track (one song), all."},
            "seconds": {"type": "integer", "description": "seek: seconds to move (negative = back)."},
            "to": {"type": "integer", "description": "seek: the point in the song, in seconds from the start."},
            "level": {"type": "integer", "description": "volume: 0-100"},
            "change": {"type": "integer", "description": "volume: percent points to add (negative = quieter)."},
            "mute": {"type": "boolean", "description": "volume: true = mute Spotify, false = unmute."},
            "query": {"type": "string", "description": "find / queue / like / add_to_playlist: what to look "
                                                       "for, e.g. 'Tarkan Şımarık', 'Tarkan', 'chill'."},
            "kind": {"type": "string", "enum": ["track", "artist", "album", "playlist", "mine", "liked"],
                     "description": "find: what query names (default track)."},
            "playlist": {"type": "string", "description": "add_to_playlist: the user's playlist name."},
            "device": {"type": "string", "description": "find: where to play: pc (default), phone, or a name."},
        },
        ["action"],
    ),
]

TOOL_FUNCTIONS = {"spotify": spotify}
PARAMETERS = set(TOOLS[0]["function"]["parameters"]["properties"]) - {"action"}
