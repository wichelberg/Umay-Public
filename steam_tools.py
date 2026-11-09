"""Tools for the user's installed Steam games: list/sort them and uninstall one.

Everything comes from Steam's own files: libraryfolders.vdf lists the library
folders, and each installed game has steamapps/appmanifest_<appid>.acf with its
name, folder, size and last-played time.

Uninstalling closes Steam (it must not be running while its files change),
deletes the game folder, its manifest and its shader cache / workshop files,
then starts Steam again. Games are far too big for the Recycle Bin, so this is
permanent; it's always confirmed first.
"""
import os
import re
import shutil
import stat
import subprocess
import time
import winreg
from datetime import datetime
from pathlib import Path

import pc_tools

STEAM_SHUTDOWN_WAIT_SECONDS = 60
SKIP_APPIDS = {"228980"}  # Steamworks Common Redistributables: not a game
SORT_KEYS = ("size", "last_played", "name")


class ToolError(Exception):
    pass


def _steam_dir():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            return Path(winreg.QueryValueEx(key, "SteamPath")[0])
    except OSError:
        raise ToolError("Steam doesn't seem to be installed.")


def _libraries():
    vdf = _steam_dir() / "steamapps" / "libraryfolders.vdf"
    try:
        text = vdf.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [_steam_dir()]
    return [Path(p.replace("\\\\", "\\")) for p in re.findall(r'"path"\s+"([^"]+)"', text)]


def _field(text, key):
    match = re.search(rf'"{key}"\s+"([^"]*)"', text)
    return match.group(1) if match else ""


def installed_games():
    """[dict(appid, name, installdir, size, last_played, library, manifest)] for every readable library."""
    games = []
    for library in _libraries():
        for manifest in (library / "steamapps").glob("appmanifest_*.acf"):
            try:
                text = manifest.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            appid = _field(text, "appid")
            if not appid or appid in SKIP_APPIDS:
                continue
            games.append({
                "appid": appid,
                "name": _field(text, "name") or appid,
                "installdir": _field(text, "installdir"),
                "size": int(_field(text, "SizeOnDisk") or 0),
                "last_played": int(_field(text, "LastPlayed") or 0),
                "library": library,
                "manifest": manifest,
            })
    return games


def _gb(size):
    return f"{size / 1024 ** 3:.1f} GB"


def _played(timestamp):
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d") if timestamp else "never"


def list_steam_games(sort_by: str = "size") -> str:
    try:
        games = installed_games()
    except ToolError as e:
        return str(e)
    if not games:
        return "No installed Steam games found."
    if sort_by == "last_played":
        games.sort(key=lambda g: g["last_played"], reverse=True)
    elif sort_by == "name":
        games.sort(key=lambda g: g["name"].lower())
    else:
        games.sort(key=lambda g: g["size"], reverse=True)
    lines = [
        f"{i}. {g['name']}: {_gb(g['size'])}, last played {_played(g['last_played'])}, on {g['library'].drive or g['library']}"
        for i, g in enumerate(games, 1)
    ]
    total = sum(g["size"] for g in games)
    return "\n".join(lines) + f"\nTotal: {len(games)} games, {_gb(total)}."


def _steam_running():
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq steam.exe", "/NH"], capture_output=True, text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    ).stdout
    return "steam.exe" in out.lower()


def _stop_steam(steam_exe):
    subprocess.run([str(steam_exe), "-shutdown"], creationflags=subprocess.CREATE_NO_WINDOW)
    deadline = time.monotonic() + STEAM_SHUTDOWN_WAIT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(2)
        if not _steam_running():
            return
    raise ToolError("Steam didn't close in time (maybe a game is running or it's updating), so nothing was deleted.")


def _remove_tree(path):
    def make_writable(func, target, _):  # read-only files would otherwise stop the delete
        os.chmod(target, stat.S_IWRITE)
        func(target)
    if path.exists():
        shutil.rmtree(path, onexc=make_writable)


def uninstall_steam_game(name: str) -> str:
    def run():
        games = installed_games()
        query = pc_tools._normalize(name)
        matches = [g for g in games if query and query in pc_tools._normalize(g["name"])]
        exact = [g for g in matches if pc_tools._normalize(g["name"]) == query]
        matches = exact or matches
        if not matches:
            raise ToolError(f"No installed Steam game matches '{name}'.")
        if len(matches) > 1:
            raise ToolError("Several games match; ask which one: " + ", ".join(g["name"] for g in matches))
        game = matches[0]

        common = (game["library"] / "steamapps" / "common").resolve()
        folder = (common / game["installdir"]).resolve() if game["installdir"] else None
        if folder is None or folder.parent != common:
            raise ToolError(f"Refusing to delete: unexpected install folder for {game['name']}.")
        if not pc_tools.confirm("confirm_uninstall_game", name=game["name"], size=_gb(game["size"]), folder=folder):
            return pc_tools._declined()

        steam_exe = _steam_dir() / "steam.exe"
        was_running = _steam_running()
        if was_running:
            _stop_steam(steam_exe)
        try:
            steamapps = game["library"] / "steamapps"
            _remove_tree(folder)
            _remove_tree(steamapps / "shadercache" / game["appid"])
            _remove_tree(steamapps / "workshop" / "content" / game["appid"])
            for leftover in (game["manifest"], steamapps / "workshop" / f"appworkshop_{game['appid']}.acf"):
                if leftover.exists():
                    leftover.unlink()
        finally:
            if was_running:
                subprocess.Popen([str(steam_exe), "-silent"])
        return f"Uninstalled {game['name']} and freed about {_gb(game['size'])}." + (
            " Steam was restarted." if was_running else "")
    try:
        return run()
    except (ToolError, pc_tools.ToolError) as e:
        return str(e)
    except OSError as e:
        return f"Failed while deleting: {e}. Some files may be left; Steam can repair/uninstall the rest."


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
        "list_steam_games",
        "Lists the Steam games installed on the PC with size on disk, last played date and drive.",
        {"sort_by": {"type": "string", "enum": list(SORT_KEYS), "description": "Default: size (largest first)."}},
        [],
    ),
    _tool(
        "uninstall_steam_game",
        "Permanently uninstalls an installed Steam game (deletes its files; Steam is closed and reopened). "
        "The user is asked to confirm first; if they decline, don't retry.",
        {"name": {"type": "string", "description": "The game's name as shown by list_steam_games."}},
        ["name"],
    ),
]

TOOL_FUNCTIONS = {
    "list_steam_games": list_steam_games,
    "uninstall_steam_game": uninstall_steam_game,
}
