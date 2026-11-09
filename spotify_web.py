"""Spotify Web API (planned.txt 23b): find and play a song, artist, album or playlist, queue a song, like the
current song, add it to one of the owner's playlists, list them.

The music still plays in the Spotify app on the PC: the API is a remote control. A command goes to Spotify's
servers, which pass it to the app (a "Spotify Connect" device); no browser tab is involved.

Setup (once): an app at developer.spotify.com (owner needs Premium; Development Mode, up to 5 users) with the
redirect URI REDIRECT_URI, its Client ID in .env as SPOTIFY_CLIENT_ID. The first use opens Spotify's consent
page in the browser (PKCE: no client secret anywhere); the tokens are kept in TOKEN_FILE.

Spotify's February 2026 rules for new Development Mode apps (developer.spotify.com, changes/february-2026):
search returns at most 10 per page; saving to the library is PUT /me/library?uris=...; adding to a playlist is
POST /playlists/{id}/items; Spotify's own category / recommendation endpoints are gone, so a "chill playlist"
is a playlist search. A refresh token may expire (reported: 6 months); then the login runs again.
"""
import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import threading
import time
import unicodedata
import urllib.parse
import webbrowser
from difflib import SequenceMatcher
from pathlib import Path

import requests

PORT = 47822  # next to web_control's 47821
REDIRECT_URI = f"http://127.0.0.1:{PORT}/callback"  # Spotify allows no "localhost" (concepts/redirect_uri)
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
SCOPES = ("user-read-playback-state user-modify-playback-state user-read-currently-playing "
          "playlist-read-private playlist-read-collaborative playlist-modify-private playlist-modify-public "
          "user-library-read user-library-modify")
LOGIN_WAIT = 180  # seconds for the owner to approve in the browser
DEVICE_WAIT = 15  # seconds for a freshly opened Spotify app to show up as a device
TIMEOUT = 15
KINDS = ("track", "artist", "album", "playlist", "mine", "liked")

_lock = threading.Lock()


class SpotifyError(Exception):
    """A message for the model: what went wrong and what the owner can do."""


def token_file():
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Umay" / "spotify_token.json"


def client_id():
    return os.environ.get("SPOTIFY_CLIENT_ID", "").strip()


# ---------- tokens ----------

def _load():
    try:
        return json.loads(token_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save(token):
    path = token_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(token, indent=1), encoding="utf-8")


def _stamp(answer, old=None):
    """Spotify's token answer -> what is kept (a refresh may come without a new refresh token)."""
    return {"access_token": answer["access_token"],
            "refresh_token": answer.get("refresh_token") or (old or {}).get("refresh_token"),
            "expires_at": time.time() + int(answer.get("expires_in", 3600)) - 60,
            "scope": answer.get("scope", "")}


def _refresh(token):
    reply = requests.post(TOKEN_URL, data={"grant_type": "refresh_token", "refresh_token": token["refresh_token"],
                                           "client_id": client_id()}, timeout=TIMEOUT)
    if reply.status_code in (400, 401):  # invalid_grant: expired or revoked
        try:
            token_file().unlink()
        except OSError:
            pass
        return None
    reply.raise_for_status()
    fresh = _stamp(reply.json(), token)
    _save(fresh)
    return fresh


def pkce_pair():
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def auth_url(challenge, state):
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": client_id(), "scope": SCOPES, "redirect_uri": REDIRECT_URI,
        "code_challenge_method": "S256", "code_challenge": challenge, "state": state})


PAGE = ("<!doctype html><meta charset='utf-8'><title>Umay</title>"
        "<body style='font-family:sans-serif;background:#111;color:#eee;padding:3em'><h2>{}</h2>"
        "<p>{}</p></body>")


def _wait_for_code(state):
    """Serves REDIRECT_URI once; the code Spotify sends back, or raises."""
    box = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if not self.path.startswith("/callback"):
                self.send_response(404)
                self.end_headers()
                return
            if query.get("state", [""])[0] != state:
                body = PAGE.format("Umay: this login link is old.", "Ask Umay again.")
            elif "code" in query:
                box["code"] = query["code"][0]
                body = PAGE.format("Umay is connected to Spotify.", "You can close this tab.")
            else:
                box["error"] = query.get("error", ["unknown"])[0]
                body = PAGE.format("Umay didn't get access to Spotify.", box["error"])
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

        def log_message(self, *args):
            pass

    try:
        server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        raise SpotifyError(f"Port {PORT} is busy, so Spotify's login can't come back to Umay ({e}).")
    server.timeout = 1
    deadline = time.monotonic() + LOGIN_WAIT
    try:
        while time.monotonic() < deadline and not box:
            server.handle_request()
    finally:
        server.server_close()
    if "code" in box:
        return box["code"]
    if box.get("error") == "access_denied":
        raise SpotifyError("The owner didn't allow Umay on Spotify's page.")
    if "error" in box:
        raise SpotifyError(f"Spotify's login failed: {box['error']}.")
    raise SpotifyError("Spotify's login page was opened in the browser but nobody approved it in 3 minutes.")


def login():
    """Opens Spotify's consent page and waits for the owner's approval; the new token."""
    if not client_id():
        raise SpotifyError(NO_CLIENT_ID)
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    webbrowser.open(auth_url(challenge, state))
    code = _wait_for_code(state)
    reply = requests.post(TOKEN_URL, data={"grant_type": "authorization_code", "code": code,
                                           "redirect_uri": REDIRECT_URI, "client_id": client_id(),
                                           "code_verifier": verifier}, timeout=TIMEOUT)
    if reply.status_code != 200:
        raise SpotifyError(f"Spotify didn't give a token: {reply.text[:200]}")
    token = _stamp(reply.json())
    _save(token)
    return token


NO_CLIENT_ID = ("Spotify search isn't set up: SPOTIFY_CLIENT_ID is missing in Umay's .env (an app at "
                "developer.spotify.com, redirect URI " + REDIRECT_URI + "). Playback control, now playing and "
                "volume work without it.")


def access_token(allow_login=True):
    with _lock:
        if not client_id():
            raise SpotifyError(NO_CLIENT_ID)
        token = _load()
        if token and token.get("expires_at", 0) <= time.time() and token.get("refresh_token"):
            token = _refresh(token)
        if not token:
            if not allow_login:
                raise SpotifyError("Umay isn't logged in to Spotify yet.")
            token = login()
        return token["access_token"]


# ---------- the API ----------

def _explain(reply):
    try:
        message = reply.json().get("error", {})
        message = message.get("message", "") if isinstance(message, dict) else str(message)
    except ValueError:
        message = reply.text[:200]
    reason = ""
    try:
        reason = reply.json().get("error", {}).get("reason", "")
    except (ValueError, AttributeError):
        pass
    if reply.status_code == 403 and ("premium" in message.lower() or reason == "PREMIUM_REQUIRED"):
        return "Spotify says this needs Premium (the account, and the developer app's owner)."
    if reply.status_code == 403 and ("registered" in message.lower() or "developer" in message.lower()):
        return ("Spotify blocks this account for the developer app: add your Spotify e-mail under User "
                "Management on developer.spotify.com (the app's page), then ask again.")
    if reply.status_code == 404 and reason == "NO_ACTIVE_DEVICE":
        return "No Spotify app is open on any device."
    if reply.status_code == 429:
        return "Spotify says too many requests; try again in a minute."
    return f"Spotify answered {reply.status_code}: {message or reason or 'no details'}."


def call(method, path, params=None, body=None, _retried=False):
    """One API call; the JSON answer (None for 204), or SpotifyError with a readable reason."""
    headers = {"Authorization": "Bearer " + access_token()}
    reply = requests.request(method, API + path, params=params, json=body, headers=headers, timeout=TIMEOUT)
    if reply.status_code == 401 and not _retried:  # revoked or clock skew: refresh once
        with _lock:
            token = _load()
            if token and token.get("refresh_token"):
                token["expires_at"] = 0
                _save(token)
        return call(method, path, params, body, _retried=True)
    if reply.status_code == 429 and not _retried:
        time.sleep(min(int(reply.headers.get("Retry-After", "2") or 2), 5))
        return call(method, path, params, body, _retried=True)
    if reply.status_code >= 400:
        raise SpotifyError(_explain(reply))
    if reply.status_code == 204 or not reply.content:
        return None
    try:
        return reply.json()
    except ValueError:
        return None


# ---------- matching names ----------

FOLD = str.maketrans({"ı": "i", "İ": "i", "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g", "ü": "u", "Ü": "u",
                      "ö": "o", "Ö": "o", "ç": "c", "Ç": "c"})


def fold(text):
    text = (text or "").translate(FOLD).casefold()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^\w]+", " ", text).strip()


def best_name(wanted, items, key=lambda item: item["name"]):
    """The item whose name is closest to `wanted` (exact, then contained, then similar), or None."""
    want = fold(wanted)
    if not want:
        return None
    best, best_score = None, 0.0
    for item in items:
        name = fold(key(item))
        if not name:
            continue
        if name == want:
            score = 3.0
        elif want in name or name in want:
            score = 2.0 + min(len(want), len(name)) / max(len(want), len(name))
        else:
            score = SequenceMatcher(None, want, name).ratio()
        if score > best_score:
            best, best_score = item, score
    return best if best_score >= 0.6 else None


# ---------- devices ----------

def _computer_name():
    return os.environ.get("COMPUTERNAME", "")


def pick_device(devices, wanted=None):
    """The device to play on: the one named (`wanted`: a name, "phone", "pc"), else this PC's Spotify app,
    else the active one."""
    devices = [d for d in devices if d.get("id") and not d.get("is_restricted")]
    if wanted:
        want = fold(wanted)
        kinds = {"phone": "smartphone", "telefon": "smartphone", "mobile": "smartphone",
                 "pc": "computer", "computer": "computer", "bilgisayar": "computer", "tablet": "tablet",
                 "tv": "tv", "speaker": "speaker", "hoparlor": "speaker"}
        if want in kinds:
            match = [d for d in devices if d.get("type", "").lower() == kinds[want]]
            if kinds[want] == "computer":
                match.sort(key=lambda d: fold(d.get("name")) != fold(_computer_name()))
            return match[0] if match else None
        return best_name(wanted, devices)
    here = fold(_computer_name())
    for d in devices:
        if d.get("type", "").lower() == "computer" and here and fold(d.get("name")) == here:
            return d
    computers = [d for d in devices if d.get("type", "").lower() == "computer"]
    if len(computers) == 1:
        return computers[0]
    active = [d for d in devices if d.get("is_active")]
    return (active or computers or devices or [None])[0]


def _devices():
    return (call("GET", "/me/player/devices") or {}).get("devices", [])


def device_for(wanted=None):
    """A device id, opening the Spotify app on the PC if no device is there."""
    device = pick_device(_devices(), wanted)
    if device is not None:
        return device
    if wanted and fold(wanted) not in ("pc", "computer", "bilgisayar"):
        raise SpotifyError(f"No Spotify device called {wanted!r} is online.")
    import spotify_tools
    spotify_tools._open_app()
    deadline = time.monotonic() + DEVICE_WAIT
    while time.monotonic() < deadline:
        time.sleep(1)
        device = pick_device(_devices())
        if device is not None:
            return device
    raise SpotifyError("I opened Spotify on the PC, but it didn't show up as a device. Try again in a moment.")


# ---------- what the tool does ----------

def _artists(item):
    return ", ".join(a["name"] for a in item.get("artists") or [] if a.get("name"))


def label(kind, item):
    name = item.get("name", "?")
    if kind == "track":
        return f'"{name}" by {_artists(item)}'
    if kind == "album":
        return f'the album "{name}" by {_artists(item)}'
    if kind == "artist":
        return name
    owner = (item.get("owner") or {}).get("display_name")
    return f'the playlist "{name}"' + (f" by {owner}" if owner else "")


def search(query, kind="track", limit=5):
    kind = kind if kind in ("track", "artist", "album", "playlist") else "track"
    # no market=from_token: it needs the user-read-private scope (403 "Insufficient client scope", tried
    # 2026-11-04); with a user's token Spotify uses the account's country anyway
    answer = call("GET", "/search", {"q": query, "type": kind, "limit": max(1, min(limit, 10))}) or {}
    return [item for item in (answer.get(kind + "s") or {}).get("items") or [] if item]  # null entries happen


def my_playlists():
    items, offset = [], 0
    while offset < 200:
        page = call("GET", "/me/playlists", {"limit": 50, "offset": offset}) or {}
        items += [p for p in page.get("items") or [] if p]
        if not page.get("next"):
            break
        offset += 50
    return items


def _play(device, body):
    call("PUT", "/me/player/play", {"device_id": device["id"]}, body)


def _now_playing_name(expect=None, wait=4.0):
    """What plays a moment after a command (Spotify needs a second), or '' if nothing plays (or not
    `expect`, a track URI) by then. A paused old song doesn't count."""
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        time.sleep(0.7)
        try:
            answer = call("GET", "/me/player/currently-playing") or {}
        except SpotifyError:
            return ""
        item = answer.get("item") or {}
        if answer.get("is_playing") and item.get("name") and (expect is None or item.get("uri") == expect):
            return f'"{item["name"]}" by {_artists(item)}'
    return ""


LIKED = re.compile(r"\b(?:liked\s+songs?|my\s+likes|liked\s+music|favou?rites?|favori\w*|begendik\w*|"
                   r"begendig\w*|sevdig\w*)\b")  # on fold()ed text: "Beğendiklerim" -> "begendiklerim"
MINE = re.compile(r"^(?:my|benim)\s+|\b(?:listem\w*|playlistim\w*|calma\s+listem\w*)\b")
PLAYLIST_WORD = re.compile(r"\b(?:play\s*lists?|playlistim?\w*|calma\s+liste\w*|liste\w*)\b")


def understand(query, kind):
    """What the model or a sentence gave -> (query, kind): "liked songs" is the Liked Songs, not a song search
    ("I Like to Move It" played, 2026-11-04); "my X playlist" / "X listemi" is one of the owner's playlists."""
    plain = fold(query)
    if LIKED.search(plain) and len(plain.split()) <= 4:
        return None, "liked"
    if MINE.search(plain) and kind in (None, "track", "playlist", "mine"):
        name = PLAYLIST_WORD.sub(" ", MINE.sub(" ", plain))
        return re.sub(r"\s+", " ", name).strip() or query, "mine"
    return query, kind


def find_and_play(query, kind="track", device=None):
    query, kind = understand(query, kind)
    kind = (kind or "track").lower()
    if kind not in KINDS:
        kind = "track"
    if kind == "playlist" and query:
        own = best_name(query, my_playlists())
        same = own is not None and (fold(query) == fold(own["name"])
                                    or (len(fold(query).split()) >= 2 and fold(query) in fold(own["name"])))
        if same:  # a one-word mood ("chill") still searches all of Spotify, not their "Gotta drive chill"
            kind = "mine"  # "play Senfonik Metal playlist": theirs, not "This is Symphonic Metal" (2026-11-04)
    if kind == "liked":
        me = call("GET", "/me") or {}
        if not me.get("id"):
            return "Couldn't read the Spotify account's id."
        target = device_for(device)
        _play(target, {"context_uri": f"spotify:user:{me['id']}:collection"})
        return f"Playing your Liked Songs on {target['name']}. " + _playing_line()
    if not (query or "").strip():
        return "Say what to look for (query)."
    if kind == "mine":
        playlists = my_playlists()
        item = best_name(query, playlists)
        if item is None:
            names = ", ".join(p["name"] for p in playlists[:15])
            return f"None of your playlists is called {query!r}. Yours: {names}." if names else \
                "This account has no playlists."
        kind = "playlist"
    else:
        found = search(query, kind)
        if not found:
            return f"Spotify found no {kind} for {query!r}."
        item = found[0]
        artist = (item.get("artists") or [{}])[0] if kind == "track" else {}
        if artist.get("uri") and fold(artist.get("name")) == fold(query):
            kind, item = "artist", artist  # "Spotify'da Tarkan çal": the artist, not one of his songs
    target = device_for(device)
    _play(target, play_body(kind, item))
    expect = item["uri"] if kind == "track" else None
    name = _now_playing_name(expect)
    if not name and target.get("type", "").lower() == "computer" and _here(target):
        # the PC app took the command but plays nothing: open the link in it instead (it plays a track at once)
        _open_uri(item["uri"])
        name = _now_playing_name(expect)
    return f"Playing {label(kind, item)} on {target['name']}. " + _said(name)


def play_body(kind, item):
    """What PUT /me/player/play gets. A song plays as its album from that song on: the Spotify app on the PC
    took {"uris": [...]} with 204 and then played nothing, while a context (album, playlist, artist,
    liked songs) played at once (tried 2026-11-04, Spotify from the Microsoft Store)."""
    album = (item.get("album") or {}).get("uri")
    if kind == "track" and album:
        return {"context_uri": album, "offset": {"uri": item["uri"]}}
    if kind == "track":
        return {"uris": [item["uri"]]}
    return {"context_uri": item["uri"]}


def _here(device):
    return fold(device.get("name")) == fold(_computer_name())


def _open_uri(uri):
    os.startfile(uri)


def _said(name):
    return f"Now playing: {name}." if name else "(Couldn't confirm it started; check with status.)"


def _playing_line():
    return _said(_now_playing_name())


def queue(query, kind="track"):
    if not (query or "").strip():
        return "Say which song to queue (query)."
    found = search(query, "track")
    if not found:
        return f"Spotify found no song for {query!r}."
    item = found[0]
    call("POST", "/me/player/queue", {"uri": item["uri"]})
    return f"Added {label('track', item)} to the queue; it plays after this song."


def _current_track():
    item = (call("GET", "/me/player/currently-playing") or {}).get("item")
    if not item or item.get("type") != "track":
        raise SpotifyError("Nothing is playing on Spotify right now.")
    return item


def like(query=None):
    if query:
        found = search(query, "track")
        if not found:
            return f"Spotify found no song for {query!r}."
        item = found[0]
    else:
        item = _current_track()
    call("PUT", "/me/library", {"uris": item["uri"]})
    return f"Liked {label('track', item)} (it's in your Liked Songs)."


def add_to_playlist(playlist, query=None):
    if not (playlist or "").strip():
        return "Say which of your playlists (playlist)."
    playlists = my_playlists()
    target = best_name(playlist, playlists)
    if target is None:
        names = ", ".join(p["name"] for p in playlists[:15])
        return f"None of your playlists is called {playlist!r}. Yours: {names}."
    if query:
        found = search(query, "track")
        if not found:
            return f"Spotify found no song for {query!r}."
        item = found[0]
    else:
        item = _current_track()
    call("POST", f"/playlists/{target['id']}/items", body={"uris": [item["uri"]]})
    return f"Added {label('track', item)} to your playlist \"{target['name']}\"."


def list_playlists():
    playlists = my_playlists()
    if not playlists:
        return "This Spotify account has no playlists."
    return f"Your {len(playlists)} playlists: " + "; ".join(p["name"] for p in playlists) + "."


def connect():
    with _lock:
        if not client_id():
            raise SpotifyError(NO_CLIENT_ID)
        login()
    me = call("GET", "/me") or {}
    return f"Connected to Spotify as {me.get('display_name') or me.get('id') or 'you'}."


def run(action, query=None, kind=None, playlist=None, device=None):
    """The web actions of spotify_tools.spotify; always a sentence for the model."""
    try:
        if action == "find":
            return find_and_play(query, kind or "track", device)
        if action == "queue":
            return queue(query)
        if action == "like":
            return like(query)
        if action == "add_to_playlist":
            return add_to_playlist(playlist, query)
        if action == "playlists":
            return list_playlists()
        if action == "login":
            return connect()
        if action == "devices":
            names = [f"{d['name']} ({d.get('type', '').lower()}{', active' if d.get('is_active') else ''})"
                     for d in _devices()]
            return "Spotify devices online: " + "; ".join(names) + "." if names else "No Spotify device is online."
        return f"Unknown Spotify action {action!r}."
    except SpotifyError as e:
        return str(e)
    except requests.RequestException as e:
        return f"Couldn't reach Spotify: {e}"
