"""Tools that let Umay work with files in allowed folders and launch apps.

Every action that changes something (create, write, move, delete, open,
launch) is confirmed first: by `confirm_handler` (ai_assistant wires it to a
yes/no dialog), or by a per-thread handler set with `channel` (the Telegram
listener asks with buttons). Until one is wired, everything is denied.

Background jobs (script runs, scheduled shutdowns) report back later through
the notify handler of the channel they were started from.
"""
import difflib
import fnmatch
import json
import os
import re
import shutil
import subprocess
import threading
import time
import unicodedata
import winreg
from contextlib import contextmanager
from itertools import count
from pathlib import Path

from send2trash import send2trash

MAX_LIST_ENTRIES = 200
MAX_SEARCH_RESULTS = 50
MAX_READ_BYTES = 1_000_000
MAX_READ_CHARS = 50_000
APP_CACHE_SECONDS = 600

# organize_downloads: subfolder -> file types sorted into it. Other types stay where they are.
ORGANIZE_CATEGORIES = {
    "Images": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".heic", ".tif", ".tiff", ".ico"},
    "Videos": {".mp4", ".mkv", ".mov", ".avi", ".webm", ".wmv", ".m4v"},
    "Audio": {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac", ".opus"},
    "Documents": {".pdf", ".doc", ".docx", ".txt", ".rtf", ".odt", ".md", ".xls", ".xlsx", ".csv", ".ods",
                  ".ppt", ".pptx", ".odp", ".epub"},
    "Archives": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".tgz", ".iso"},
    "Installers": {".exe", ".msi", ".msix", ".msixbundle", ".appx", ".appinstaller"},
    "Code": {".py", ".ipynb", ".js", ".ts", ".json", ".html", ".css", ".java", ".c", ".cpp", ".h", ".cs", ".r",
             ".sql", ".sh", ".ps1", ".bat", ".yml", ".yaml", ".xml"},
}
# Unfinished downloads, and files changed this recently (may still be written or in use), stay put.
ORGANIZE_SKIP_SUFFIXES = {".crdownload", ".part", ".partial", ".opdownload", ".download", ".tmp"}
ORGANIZE_RECENT_SECONDS = 10 * 60
ORGANIZE_SAMPLE_NAMES = 3

# Set by ai_assistant. Keys may be one i18n key or a list of them (see i18n.render).
#   confirm_handler(keys, **kwargs) -> bool    notify_handler(keys, **kwargs)
confirm_handler = lambda key, **kwargs: False
notify_handler = lambda key, **kwargs: print(key, kwargs)
# Set by telegram_listener: where PC-started jobs report when Umay's window is closed.
fallback_notify = print
_local = threading.local()


@contextmanager
def channel(confirm, notify, photo=None):
    """Uses these handlers instead of the defaults for tool calls made on this thread.
    photo(jpeg_bytes, caption) sends an image to the user (only Telegram has one)."""
    _local.confirm, _local.notify, _local.photo = confirm, notify, photo
    try:
        yield
    finally:
        _local.confirm = _local.notify = _local.photo = None


def current_photo_sender():
    """photo(jpeg_bytes, caption) for this thread's channel, or None (Umay's PC window)."""
    return getattr(_local, "photo", None)


def new_request():
    """A new request from the owner (ai_assistant._send_message): nothing untrusted read yet."""
    _local.request = {"untrusted": False, "urls": set()}


def request_state():
    """This thread's request: {"untrusted": bool, "urls": addresses seen in what was read}."""
    state = getattr(_local, "request", None)
    if state is None:
        new_request()
        state = _local.request
    return state


def mark_untrusted(urls=()):
    """Something from outside (a web page, search results, WhatsApp text) was read in this request. Until
    the next request, every confirm says so first (plan 20b, strict mode: a small model can be talked into
    things by a page, so the owner sees the warning before saying yes). urls: links seen in it."""
    state = request_state()
    state["untrusted"] = True
    state["urls"].update(u for u in urls if u)


def untrusted():
    return request_state()["untrusted"]


def confirm(key, **kwargs):
    handler = getattr(_local, "confirm", None) or confirm_handler
    if untrusted():
        keys = [key] if isinstance(key, str) else list(key)
        key = ["confirm_untrusted_note"] + keys
    return handler(key, **kwargs)


def current_notifier():
    """The notify handler for this thread's channel, for jobs that report back later."""
    return getattr(_local, "notify", None) or (lambda key, **kwargs: notify_handler(key, **kwargs))


def _allowed_dirs():
    raw = os.environ.get("UMAY_ALLOWED_DIRS", "").strip()
    if raw:
        dirs = [Path(os.path.expandvars(os.path.expanduser(p.strip()))) for p in raw.split(";") if p.strip()]
    else:
        home = Path.home()
        dirs = [home / "Desktop", home / "Downloads", home / "Documents"]
    return [d.resolve() for d in dirs if d.is_dir()]


ALLOWED_DIRS = _allowed_dirs()
_ALLOWED_TEXT = ", ".join(str(d) for d in ALLOWED_DIRS)


class ToolError(Exception):
    pass


def _resolve(path, allow_root=True):
    """Resolves a user/model-given path and makes sure it's inside an allowed folder."""
    p = Path(os.path.expandvars(os.path.expanduser(path.strip().strip('"'))))
    if not p.is_absolute():
        raise ToolError(f"Use an absolute path inside one of the allowed folders: {_ALLOWED_TEXT}")
    p = p.resolve()
    for root in ALLOWED_DIRS:
        if p == root:
            if not allow_root:
                raise ToolError(f"Refusing to change the allowed folder itself: {root}")
            return p
        if p.is_relative_to(root):
            return p
    raise ToolError(f"Access denied. Only these folders are allowed: {_ALLOWED_TEXT}")


def _run(fn):
    """Turns ToolError/OSError into a plain message the model can read."""
    try:
        return fn()
    except ToolError as e:
        return str(e)
    except OSError as e:
        return f"Failed: {e}"


def _declined():
    return "The user declined this action. Don't retry it unless they ask again."


# Files that never leave the PC (WhatsApp, uploads to a site), whatever folder they're in.
SECRET_NAMES = (".env", ".env.*", "*.pem", "*.key", "*.pfx", "*.p12", "*.ppk", "*.kdbx", "*.kdb",
                "id_rsa*", "id_ed25519*", "id_ecdsa*", "credentials*", "*.ovpn", "spotify_token*")
SECRET_FOLDERS = {".ssh", ".gnupg", ".aws", ".azure", ".claude", ".state", "claude_communication_wichelberg"}


def _shareable_file(path):
    """A file that may be sent off the PC: inside the allowed folders and not a secret."""
    file = _resolve(path, allow_root=False)
    if not file.is_file():
        raise ToolError(f"Not a file: {file}")
    name = file.name.lower()
    if any(fnmatch.fnmatch(name, pattern) for pattern in SECRET_NAMES) or \
            any(part.lower() in SECRET_FOLDERS for part in file.parts):
        raise ToolError(f"Refusing to send {file.name}: it looks like a secret (keys, passwords, tokens).")
    return file


def ask_everywhere(key, **kwargs):
    """The owner's "ask before every step, reading included" tools (web control, reading WhatsApp): asked
    where the request came from. A Telegram request on Telegram (its channel's buttons); one from the PC
    aloud (confirm_handler, voice_confirm), never as text on the screen (the owner's choice, 2026-11-02:
    until then both were asked at once)."""
    return confirm(key, **kwargs)


def list_directory(path: str) -> str:
    def run():
        p = _resolve(path)
        if not p.is_dir():
            raise ToolError(f"Not a folder: {p}")
        entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        lines = []
        for e in entries[:MAX_LIST_ENTRIES]:
            if e.is_dir():
                lines.append(f"[DIR]  {e.name}")
            else:
                lines.append(f"[FILE] {e.name} ({e.stat().st_size} bytes)")
        if len(entries) > MAX_LIST_ENTRIES:
            lines.append(f"... and {len(entries) - MAX_LIST_ENTRIES} more")
        return f"{p}\n" + ("\n".join(lines) if lines else "(empty)")
    return _run(run)


def search_files(pattern: str, folder: str = "") -> str:
    def run():
        if ".." in pattern or Path(pattern).is_absolute():
            raise ToolError("Pattern must be a simple glob like '*.pdf'; use `folder` to pick where to search.")
        roots = [_resolve(folder)] if folder else ALLOWED_DIRS
        results = []
        for root in roots:
            for match in root.rglob(pattern):
                results.append(str(match))
                if len(results) >= MAX_SEARCH_RESULTS:
                    return "\n".join(results) + f"\n(stopped at {MAX_SEARCH_RESULTS} results)"
        return "\n".join(results) if results else "No matching files found."
    return _run(run)


def read_file(path: str) -> str:
    def run():
        p = _resolve(path)
        if not p.is_file():
            raise ToolError(f"Not a file: {p}")
        if p.stat().st_size > MAX_READ_BYTES:
            raise ToolError(f"File is too large to read ({p.stat().st_size} bytes).")
        text = p.read_text(encoding="utf-8", errors="replace")
        if len(text) > MAX_READ_CHARS:
            return text[:MAX_READ_CHARS] + f"\n... (truncated, {len(text)} chars total)"
        return text
    return _run(run)


def create_folder(path: str) -> str:
    def run():
        p = _resolve(path, allow_root=False)
        if p.exists():
            raise ToolError(f"Already exists: {p}")
        if not confirm("confirm_create_folder", path=p):
            return _declined()
        p.mkdir(parents=True)
        return f"Created folder: {p}"
    return _run(run)


def write_file(path: str, content: str) -> str:
    def run():
        p = _resolve(path, allow_root=False)
        if p.is_dir():
            raise ToolError(f"That's a folder, not a file: {p}")
        key = "confirm_overwrite_file" if p.exists() else "confirm_write_file"
        if not confirm(key, path=p, chars=len(content)):
            return _declined()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} characters to {p}"
    return _run(run)


def move_path(source: str, destination: str) -> str:
    def run():
        src = _resolve(source, allow_root=False)
        dst = _resolve(destination, allow_root=True)
        if not src.exists():
            raise ToolError(f"Doesn't exist: {src}")
        if dst.is_dir():
            dst = dst / src.name
        if dst.exists():
            raise ToolError(f"Destination already exists: {dst}")
        if not confirm("confirm_move", source=src, destination=dst):
            return _declined()
        shutil.move(str(src), str(dst))
        return f"Moved {src} -> {dst}"
    return _run(run)


def delete_path(path: str) -> str:
    def run():
        p = _resolve(path, allow_root=False)
        if not p.exists():
            raise ToolError(f"Doesn't exist: {p}")
        if not confirm("confirm_delete", path=p):
            return _declined()
        send2trash(str(p))
        return f"Sent to the Recycle Bin: {p}"
    return _run(run)


def _organize_plan(folder):
    """{category: [files]} for the files directly in `folder`, and how many are left in place."""
    category_of = {ext: cat for cat, exts in ORGANIZE_CATEGORIES.items() for ext in exts}
    plan, skipped, now = {}, 0, time.time()
    for f in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
        if not f.is_file() or f.name.startswith(".") or f.name.lower() == "desktop.ini":
            continue
        suffix = f.suffix.lower()
        category = category_of.get(suffix)
        if category is None or suffix in ORGANIZE_SKIP_SUFFIXES or now - f.stat().st_mtime < ORGANIZE_RECENT_SECONDS:
            skipped += 1
            continue
        plan.setdefault(category, []).append(f)
    return plan, skipped


def _free_name(path):
    """`path`, or `name (2).ext`, `name (3).ext`... if it's taken. Nothing is ever overwritten."""
    if not path.exists():
        return path
    for i in count(2):
        candidate = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not candidate.exists():
            return candidate


def organize_downloads(folder: str = "", preview: bool = False) -> str:
    def run():
        target = _resolve(folder or str(Path.home() / "Downloads"))
        if not target.is_dir():
            raise ToolError(f"Not a folder: {target}")
        plan, skipped = _organize_plan(target)
        left = (f"\nLeft in place: {skipped} files (other types, unfinished downloads, or changed in the "
                f"last {ORGANIZE_RECENT_SECONDS // 60} minutes).") if skipped else ""
        if not plan:
            return f"Nothing to sort in {target}.{left}"
        lines = []
        for category, files in plan.items():
            names = ", ".join(f.name for f in files[:ORGANIZE_SAMPLE_NAMES])
            more = f" +{len(files) - ORGANIZE_SAMPLE_NAMES}" if len(files) > ORGANIZE_SAMPLE_NAMES else ""
            lines.append(f"{category}/ <- {len(files)}: {names}{more}")
        summary = "\n".join(lines)
        total = sum(len(files) for files in plan.values())
        if preview:
            return f"Plan for {target} (nothing moved yet):\n{summary}{left}"
        if not confirm("confirm_organize", folder=target, count=total, plan=summary):
            return _declined()
        moved, failed = 0, []
        for category, files in plan.items():
            destination = target / category
            destination.mkdir(exist_ok=True)
            for f in files:
                try:
                    shutil.move(str(f), str(_free_name(destination / f.name)))
                    moved += 1
                except OSError as e:
                    failed.append(f"{f.name}: {e}")
        result = f"Sorted {moved} of {total} files in {target} into subfolders:\n{summary}{left}"
        if failed:
            result += "\nCouldn't move (maybe open in another app):\n" + "\n".join(failed[:10])
        return result
    return _run(run)


def open_path(path: str) -> str:
    def run():
        p = _resolve(path)
        if not p.exists():
            raise ToolError(f"Doesn't exist: {p}")
        if not confirm("confirm_open", path=p):
            return _declined()
        os.startfile(str(p))
        return f"Opened: {p}"
    return _run(run)


# --- Applications ------------------------------------------------------------

_app_cache = {"time": 0.0, "apps": {}}


def _normalize(text):
    """For loose name matching: case, spaces/punctuation and accents ignored ("Yılmaz" = "yilmaz")."""
    text = unicodedata.normalize("NFKD", text.casefold().replace("ı", "i"))
    return re.sub(r"[\W_]", "", "".join(c for c in text if not unicodedata.combining(c)))


def _start_menu_apps():
    """Every app in the Start menu (desktop, Store and Steam shortcuts) as {app_id: name}."""
    command = (
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
        "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW,
        ).stdout.decode("utf-8", errors="replace")
        items = json.loads(out or "[]")
    except Exception:
        return {}
    if isinstance(items, dict):
        items = [items]
    return {i["AppID"]: i["Name"] for i in items if i.get("AppID") and i.get("Name")}


def _steam_games():
    """Installed Steam games as {steam://rungameid/<id>: name}, including ones without a Start menu shortcut."""
    import steam_tools  # imports pc_tools itself

    try:
        return {f"steam://rungameid/{g['appid']}": g["name"] for g in steam_tools.installed_games()}
    except steam_tools.ToolError:
        return {}


def _installed_apps():
    if time.time() - _app_cache["time"] > APP_CACHE_SECONDS:
        apps = _steam_games()
        apps.update(_start_menu_apps())
        _app_cache.update(time=time.time(), apps=apps)
    return _app_cache["apps"]


def find_applications(name: str) -> str:
    import window_tools  # imports system_tools, which imports this module

    query = _normalize(name)
    if not query:
        return "Give an app name to search for."
    opened = window_tools.open_app(name)  # "not installed" sent the 4b away from an app that was on the screen
    if opened:
        return (f"{opened} is already open, so there's nothing to launch. window_control brings it to the front, "
                "then look_at_screen shows it.")
    scored = []
    for app_id, app_name in _installed_apps().items():
        candidate = _normalize(app_name)
        if query in candidate:
            score = 1.0 + len(query) / max(len(candidate), 1)
        else:
            score = difflib.SequenceMatcher(None, query, candidate).ratio()
        if score >= 0.6:
            scored.append((score, app_name, app_id))
    if not scored:
        return f"No installed app matches '{name}'."
    scored.sort(reverse=True)
    return "\n".join(f"- {app_name} | app_id: {app_id}" for _, app_name, app_id in scored[:8])


def _exe_file(text):
    """The program a path means, or None if `text` isn't an .exe path. "Documents\\x.exe" is under the home folder,
    and a name typed or heard with stray spaces ("rustdesk-1.5. 1-x86_64.exe") still finds its file."""
    text = text.strip().strip('"')
    if not text.lower().endswith(".exe"):
        return None
    path = Path(text) if Path(text).is_absolute() else Path.home() / text
    if not path.exists() and path.parent.is_dir():
        wanted = _normalize(path.name)
        path = next((f for f in path.parent.glob("*.exe") if _normalize(f.name) == wanted), path)
    path = _resolve(str(path))
    if not path.is_file():
        raise ToolError(f"Doesn't exist: {path}")
    return path


def _steam_folder(app_id):
    """The folder a Steam game runs from, or None."""
    import steam_tools  # imports pc_tools itself

    appid = app_id.rsplit("/", 1)[-1]
    try:
        game = next((g for g in steam_tools.installed_games() if g["appid"] == appid), None)
    except steam_tools.ToolError:
        return None
    return game["library"] / "steamapps" / "common" / game["installdir"] if game and game["installdir"] else None


def launch_application(app_id: str) -> str:
    def run():
        import window_tools  # imports system_tools, which imports this module

        exe = _exe_file(app_id)
        apps = {} if exe else _installed_apps()
        if not exe and app_id not in apps:
            return ("Unknown app_id. Call find_applications first and use an app_id from its results, or give the "
                    "full path of an .exe file.")
        name = exe.stem if exe else apps[app_id]
        if not confirm("confirm_launch", name=exe.name if exe else name):
            return _declined()
        if exe:
            return window_tools.start_and_wait(name, lambda: os.startfile(str(exe), cwd=str(exe.parent)), exe.parent)
        if "://" in app_id:
            folder = _steam_folder(app_id) if app_id.startswith("steam://") else None
            return window_tools.start_and_wait(name, lambda: os.startfile(app_id), folder)
        return window_tools.start_and_wait(
            name, lambda: subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app_id}"]))
    return _run(run)


# --- Tool schemas ------------------------------------------------------------

def _tool(name, description, properties, required):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


_PATH_NOTE = f" Absolute paths only, inside: {_ALLOWED_TEXT}."
_CONFIRM_NOTE = " Asks the user first; if they decline, don't retry."

TOOLS = [
    _tool(
        "list_directory",
        "Lists the files and subfolders in a folder on the user's PC." + _PATH_NOTE,
        {"path": {"type": "string", "description": "Absolute folder path."}},
        ["path"],
    ),
    _tool(
        "search_files",
        "Recursively searches for files by glob pattern (e.g. '*.pdf', '*invoice*') in the allowed folders, "
        "or only in `folder` if given." + _PATH_NOTE,
        {
            "pattern": {"type": "string", "description": "Glob pattern, e.g. '*.pdf'."},
            "folder": {"type": "string", "description": "Optional absolute folder to limit the search to."},
        },
        ["pattern"],
    ),
    _tool(
        "read_file",
        "Reads a text file on the user's PC so you can see its text. To open a file for the user, use open_path."
        + _PATH_NOTE,
        {"path": {"type": "string", "description": "Absolute file path."}},
        ["path"],
    ),
    _tool(
        "create_folder",
        "Creates a new folder (and missing parent folders)." + _PATH_NOTE + _CONFIRM_NOTE,
        {"path": {"type": "string", "description": "Absolute path of the folder to create."}},
        ["path"],
    ),
    _tool(
        "write_file",
        "Writes text to a file, creating it or replacing its contents." + _PATH_NOTE + _CONFIRM_NOTE,
        {
            "path": {"type": "string", "description": "Absolute file path."},
            "content": {"type": "string", "description": "The full text to write."},
        },
        ["path", "content"],
    ),
    _tool(
        "move_path",
        "Moves or renames a file or folder. If destination is an existing folder, the item is moved into it."
        + _PATH_NOTE + _CONFIRM_NOTE,
        {
            "source": {"type": "string", "description": "Absolute path of the file/folder to move."},
            "destination": {"type": "string", "description": "Absolute new path, or an existing folder to move into."},
        },
        ["source", "destination"],
    ),
    _tool(
        "delete_path",
        "Sends a file or folder to the Recycle Bin (recoverable). Use directly for 'delete X': build the path from "
        "the folder the user named, don't read or search first." + _PATH_NOTE + _CONFIRM_NOTE,
        {"path": {"type": "string", "description": "Absolute path of the file/folder to delete."}},
        ["path"],
    ),
    _tool(
        "organize_downloads",
        "Tidies a folder (default: the user's Downloads) by moving the files directly in it into subfolders by "
        "type: " + ", ".join(ORGANIZE_CATEGORIES) + ". Nothing is deleted or overwritten; other file types, "
        "unfinished downloads and files changed in the last 10 minutes stay put. Use preview=true to show the "
        "plan without moving anything. The user confirms the whole plan once; if they decline, don't retry."
        + _PATH_NOTE,
        {
            "folder": {"type": "string", "description": "Absolute folder path; empty = Downloads."},
            "preview": {"type": "boolean", "description": "Only show what would be moved."},
        },
        [],
    ),
    _tool(
        "open_path",
        "Opens a file or folder with its default program (e.g. a PDF in the PDF viewer, a folder in Explorer). "
        "Use for 'open <file or folder>'."
        + _PATH_NOTE + _CONFIRM_NOTE,
        {"path": {"type": "string", "description": "Absolute path of the file/folder to open."}},
        ["path"],
    ),
    _tool(
        "find_applications",
        "Searches the apps and games installed on the user's PC (Start menu and Steam library) by name. "
        "Only to start an app ('open / start / run <app>', also WhatsApp or Discord): call this first, then "
        "launch_application with the app_id.",
        {"name": {"type": "string", "description": "App or game name, e.g. 'Once Human', 'Discord'."}},
        ["name"],
    ),
    _tool(
        "launch_application",
        "Launches an installed app or game, or runs an .exe file, and waits for its window. Its answer says what "
        "really happened: report that, not 'done'." + _PATH_NOTE + _CONFIRM_NOTE,
        {"app_id": {"type": "string", "description": "The app_id from find_applications results, or the full path "
                                                     "of an .exe file."}},
        ["app_id"],
    ),
]

TOOL_FUNCTIONS = {
    "list_directory": list_directory,
    "search_files": search_files,
    "read_file": read_file,
    "create_folder": create_folder,
    "write_file": write_file,
    "move_path": move_path,
    "delete_path": delete_path,
    "organize_downloads": organize_downloads,
    "open_path": open_path,
    "find_applications": find_applications,
    "launch_application": launch_application,
}
