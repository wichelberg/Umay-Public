"""Disk scanner: finds what takes the space on C: as *items* (a Steam game, an installed program, a
Python install, a venv, a cache, a HuggingFace model, a project, one of the owner's folders, or an
unknown big folder), never file by file, and only items that rules can recognise reliably.
Read-only: nothing here changes the disk (disk_actions does, after the owner confirms in the panel),
and nothing here touches the UI: scan() returns a Scan that the panel shows and disk_memory stores.

One walk over the drive charges every folder to the deepest item that contains it, so an item's size
leaves out the items inside it (a project's venv is its own item). Sizes are what the files really
take: the allocated size from the directory listing (compressed, sparse and WOF files ask Windows),
each file counted once however many hard links it has: the item met first in the walk is charged,
the others keep it as `shared`. Junctions and symlinks aren't followed.

Last use comes from the best source each item has, kept in `used_source`: Steam's LastPlayed, the
Prefetch files (only when readable: they need admin rights), the project's last git commit, else the
newest file in it. NTFS last-access times are never used: Windows doesn't keep them reliably.

Dependencies are only the reliable ones: venv -> its base Python (home= in pyvenv.cfg), venv -> every
folder its .pth files add (another venv, an editable project), shortcut / startup entry / scheduled
task -> its exe, project code -> the HuggingFace models it names, Umay -> her own folders.
"""
import ctypes
import json
import os
import re
import struct
import subprocess
import sys
import time
import winreg
from array import array
from ctypes import wintypes
from dataclasses import asdict, dataclass, field
from pathlib import Path

import i18n
import paths

KINDS = ("game", "program", "python", "venv", "cache", "model", "mei", "project", "own", "folder", "system",
         "app", "link")
# Everything except the owner's own files can be downloaded or installed again.
NOT_REDOWNLOADABLE = {"project", "own"}
MIN_FOLDER_BYTES = 100 * 1024 ** 2  # unknown folders and loose container files below this aren't listed
CODE_SUFFIXES = (".py", ".toml", ".yaml", ".yml", ".json", ".cfg")
CODE_MAX_BYTES = 1024 ** 2
NO_CREATE_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# --- the items ---------------------------------------------------------------------------------


@dataclass
class Item:
    id: str                       # stable across scans (history, graph): steam:730, program:<reg key>, path:<path>
    kind: str
    name: str
    path: str = ""                # "" for a program the registry knows only by name
    parts: list = field(default_factory=list)   # more folders that belong to it (a game's shader cache, _MEI dirs)
    key: str = ""                 # identity for the knowledge memory: steam appid, product + publisher, model id
    size: int = 0                 # bytes on disk, hard links counted once
    size_source: str = "walk"     # walk | registry (EstimatedSize: the folder is unknown)
    shared: int = 0               # bytes not counted here: hard links to files another item was charged for
    files: int = 0
    newest: float = 0.0           # newest file's mtime
    last_used: float = 0.0        # 0: unknown / never
    used_source: str = ""         # steam | steam_never | prefetch | git | newest_file | install_date
    redownloadable: bool = True
    what: str = ""
    what_source: str = ""         # rule | exe | gemini | owner
    info: dict = field(default_factory=dict)
    needs: list = field(default_factory=list)   # paths this item needs to work
    dep_ids: list = field(default_factory=list)  # items those paths belong to (graph)
    broken: list = field(default_factory=list)   # needed paths that don't exist any more
    protected: str = ""           # why there's no delete button ("" = there is one)
    opaque: bool = False          # venvs inside it aren't items of their own (a game, a cache, Failsafe)
    container: bool = False       # the loose files of a folder whose sub-folders are items of their own

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Scan:
    at: float
    items: list
    total: int = 0
    used: int = 0
    free: int = 0
    stats: dict = field(default_factory=dict)

    def by_id(self):
        return {it.id: it for it in self.items}

    def to_dict(self):
        return {"at": self.at, "total": self.total, "used": self.used, "free": self.free, "stats": self.stats,
                "items": [it.to_dict() for it in self.items]}

    @classmethod
    def from_dict(cls, data):
        return cls(at=data["at"], items=[Item.from_dict(d) for d in data["items"]], total=data.get("total", 0),
                   used=data.get("used", 0), free=data.get("free", 0), stats=data.get("stats", {}))


def norm(p):
    return os.path.normcase(os.path.normpath(str(p)))


def under(p, root):
    """True if p is root or inside it."""
    p, root = norm(p), norm(root)
    return p == root or p.startswith(root.rstrip("\\") + "\\")


# --- where things are --------------------------------------------------------------------------


def _env_path(name, default):
    value = os.environ.get(name, "").strip()
    return Path(value) if value else Path(default)


@dataclass
class Places:
    """Every folder the scanner knows by rule. Tests build one over a fake tree."""
    drive: Path
    home: Path
    local: Path
    roaming: Path
    temp: Path
    program_data: Path
    program_files: list
    windows: Path
    documents: Path
    project_roots: list
    voice_home: Path
    failsafe: Path
    egitim_venv: Path
    wichelberg_app: Path
    umay_app: Path = None      # the installed Umay (exe folder), if known
    umay_source: Path = None   # this source checkout, when not frozen
    own_meipass: str = ""         # the running Umay's _MEI folder (frozen only)
    steam: Path = None
    hf_hub: Path = None
    torch_home: Path = None
    prefetch: Path = None

    @classmethod
    def real(cls):
        home = Path.home()
        local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        roaming = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
        windows = Path(os.environ.get("SystemRoot") or r"C:\Windows")
        documents = home / "Documents"
        hf_home = _env_path("HF_HOME", home / ".cache" / "huggingface")
        frozen = getattr(sys, "frozen", False)
        app = paths.app_dir() if frozen else documents / "Apps" / "Umay"
        return cls(
            drive=Path(os.environ.get("SystemDrive", "C:") + "\\"),
            home=home, local=local, roaming=roaming,
            temp=Path(os.environ.get("TEMP") or local / "Temp"),
            program_data=Path(os.environ.get("ProgramData") or r"C:\ProgramData"),
            program_files=[Path(os.environ.get(v) or d) for v, d in
                           (("ProgramFiles", r"C:\Program Files"), ("ProgramFiles(x86)", r"C:\Program Files (x86)"))],
            windows=windows,
            documents=documents,
            project_roots=[documents / "Projects", documents / "Work"],
            voice_home=paths.voice_home(),
            failsafe=documents / "Failsafe",
            egitim_venv=_env_path("UMAY_TORCH_VENV", documents / "Projects" / "Wichelberg_VoiceChanger" / "data"
                                  / "egitim" / "venv"),
            wichelberg_app=_env_path("UMAY_WICHELBERG_DIR", documents / "Apps" / "Wichelberg" / "_wichelberg"),
            umay_app=app if (app / "Umay.exe").is_file() else None,
            umay_source=None if frozen else Path(__file__).resolve().parent,
            own_meipass=getattr(sys, "_MEIPASS", "") if frozen else "",
            steam=_steam_dir(),
            hf_hub=_env_path("HF_HUB_CACHE", hf_home / "hub"),
            torch_home=_env_path("TORCH_HOME", home / ".cache" / "torch"),
            prefetch=windows / "Prefetch",
        )

    def caches(self):
        """(cache key, folder) of the known caches; Temp and the _MEI folders are handled apart."""
        uv = os.environ.get("UV_CACHE_DIR", "").strip()
        return [
            ("pip", self.local / "pip" / "cache"),
            ("uv", Path(uv) if uv else self.local / "uv" / "cache"),
            ("torch", self.torch_home),
            ("npm", self.local / "npm-cache"),
            ("dxcache", self.local / "NVIDIA" / "DXCache"),
            ("dxcache", self.local.parent / "LocalLow" / "NVIDIA" / "PerDriverVersion" / "DXCache"),
            ("crashdumps", self.local / "CrashDumps"),
            ("vscode_crashpad", self.roaming / "Code" / "Crashpad"),
            ("vscode_vsix", self.roaming / "Code" / "CachedExtensionVSIXs"),
            ("vortex_downloads", self.roaming / "Vortex" / "downloads"),
        ]


def _steam_dir():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            value = winreg.QueryValueEx(key, "SteamPath")[0]
        return Path(value) if Path(value).is_dir() else None
    except OSError:
        return None


# Folders whose sub-folders are items of their own; what those children are by default.
# (child kind, the child's folder names that are Windows' own and never get a delete button)
SYSTEM_C = {"windows", "recovery", "boot", "$winreagent", "$windows.~bt", "$windows.~ws", "$sysreset",
            "system volume information", "config.msi", "perflogs", "documents and settings", "msocache",
            "$getcurrent", "onedrivetemp"}
SYSTEM_PROGRAM_FILES = {"common files", "internet explorer", "modifiablewindowsapps", "reference assemblies",
                        "msbuild", "dotnet", "packagemanagement", "uninstall information", "windowspowershell",
                        "windowsapps", "windows defender", "windows defender advanced threat protection",
                        "windows mail", "windows media player", "windows nt", "windows photo viewer",
                        "windows portable devices", "windows security", "windows sidebar", "microsoft update health tools",
                        "microsoft.net", "windows kits", "microsoft sdks", "iis", "iis express"}
SYSTEM_PROGRAM_DATA = {"microsoft", "package cache", "packages", "usoprivate", "usoshared", "ssh",
                       "windowsholographicdevices", "regid.1991-06.com.microsoft", "softwaredistribution"}
SYSTEM_APPDATA = {"microsoft", "packages", "comms", "connecteddevicesplatform", "microsoftedge", "virtualstore",
                  "publishers"}
SYSTEM_USERS = {"default", "public", "all users", "default user"}
OWN_FOLDERS = {"desktop", "downloads", "documents", "pictures", "videos", "music", "saved games", "contacts",
               "favorites", "links", "searches", "3d objects", "onedrive"}


@dataclass
class Container:
    child_kind: str               # folder | own | project
    system: set = field(default_factory=set)
    installs: bool = False        # children are installed programs (Program Files)
    steam: str = ""               # common | workshop: a Steam library's folder (its children without a game are leftovers)


def _containers(pl):
    c = {
        pl.drive: Container("folder", SYSTEM_C, installs=True),
        pl.program_data: Container("folder", SYSTEM_PROGRAM_DATA),
        pl.drive / "Users": Container("folder", SYSTEM_USERS),
        pl.home: Container("own"),
        pl.local: Container("folder", SYSTEM_APPDATA),
        pl.roaming: Container("folder", SYSTEM_APPDATA),
        pl.local.parent / "LocalLow": Container("folder", SYSTEM_APPDATA),
        pl.local / "Programs": Container("folder", installs=True),
        pl.home / ".cache": Container("folder"),
        pl.documents: Container("own"),
        pl.documents / "Apps": Container("own"),
    }
    for pf in pl.program_files:
        c[pf] = Container("folder", SYSTEM_PROGRAM_FILES, installs=True)
    for root in pl.project_roots:
        c[root] = Container("project")
    return {norm(p): (Path(p), spec) for p, spec in c.items()}


# --- Windows: listing folders fast, file sizes, exe version info --------------------------------

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                 wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.GetCurrentThread.restype = wintypes.HANDLE
kernel32.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
kernel32.GetCompressedFileSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.GetCompressedFileSizeW.restype = wintypes.DWORD
INVALID_HANDLE = wintypes.HANDLE(-1).value
FILE_ID_BOTH_DIRECTORY_INFO, FILE_ID_BOTH_DIRECTORY_RESTART_INFO = 10, 11
ERROR_NO_MORE_FILES = 18
ATTR_DIRECTORY, ATTR_REPARSE, ATTR_COMPRESSED, ATTR_SPARSE = 0x10, 0x400, 0x800, 0x200
LINK_TAGS = {0xA0000003, 0xA000000C, 0x8000001B}   # junction, symlink, app execution alias
PACKED_TAGS = {0x80000017, 0x80000013}             # WOF (compact.exe), dedup: the listing's size is wrong
_ENTRY = struct.Struct("<II4q2qIII")               # FILE_ID_BOTH_DIR_INFO up to EaSize
_FILE_ID = struct.Struct("<q")
_BUFFER = ctypes.create_string_buffer(256 * 1024)
EPOCH_DIFF = 116444736000000000


def list_dir(path):
    """[(name, attributes, allocated bytes, size, mtime, file id, reparse tag)] of a folder, or None if it
    can't be opened. One handle per folder (no per-file open), so it also works for big trees."""
    handle = kernel32.CreateFileW("\\\\?\\" + str(path), 1, 7, None, 3, 0x02000000, None)
    if handle == INVALID_HANDLE or handle is None:
        return None
    out = []
    try:
        klass = FILE_ID_BOTH_DIRECTORY_RESTART_INFO
        buf = _BUFFER
        while kernel32.GetFileInformationByHandleEx(handle, klass, buf, len(buf)):
            klass = FILE_ID_BOTH_DIRECTORY_INFO
            raw = buf.raw
            off = 0
            while True:
                (nxt, _, _, _, mtime, _, eof, alloc, attrs, name_len, ea) = _ENTRY.unpack_from(raw, off)
                name = raw[off + 104:off + 104 + name_len].decode("utf-16-le", "replace")
                if name not in (".", ".."):
                    fid = _FILE_ID.unpack_from(raw, off + 96)[0]
                    out.append((name, attrs, alloc, eof, (mtime - EPOCH_DIFF) / 1e7, fid,
                                ea if attrs & ATTR_REPARSE else 0))
                if not nxt:
                    break
                off += nxt
        if ctypes.get_last_error() not in (ERROR_NO_MORE_FILES, 0):
            return out or None
    finally:
        kernel32.CloseHandle(handle)
    return out


def packed_size(path, fallback):
    """Bytes a compressed / sparse / WOF file really takes."""
    high = wintypes.DWORD(0)
    low = kernel32.GetCompressedFileSizeW("\\\\?\\" + str(path), ctypes.byref(high))
    if low == 0xFFFFFFFF and ctypes.get_last_error():
        return fallback
    return (high.value << 32) + low


_version = ctypes.WinDLL("version")
_version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
_version.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID]
_version.VerQueryValueW.argtypes = [wintypes.LPCVOID, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
                                    ctypes.POINTER(wintypes.UINT)]


def exe_info(path):
    """{CompanyName, ProductName, FileDescription} of an exe (only the ones it has), {} if none."""
    size = _version.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return {}
    data = ctypes.create_string_buffer(size)
    if not _version.GetFileVersionInfoW(str(path), 0, size, data):
        return {}
    ptr, length = ctypes.c_void_p(), wintypes.UINT()
    langs = ["040904b0", "040904e4", "000004b0"]
    if _version.VerQueryValueW(data, r"\VarFileInfo\Translation", ctypes.byref(ptr), ctypes.byref(length)) \
            and length.value >= 4:
        lang, codepage = struct.unpack("<HH", ctypes.string_at(ptr.value, 4))
        langs.insert(0, f"{lang:04x}{codepage:04x}")
    info = {}
    for field_name in ("CompanyName", "ProductName", "FileDescription"):
        for lang in langs:
            if _version.VerQueryValueW(data, f"\\StringFileInfo\\{lang}\\{field_name}", ctypes.byref(ptr),
                                       ctypes.byref(length)) and length.value > 1:
                value = ctypes.wstring_at(ptr.value, length.value).rstrip("\x00").strip()
                if value:
                    info[field_name] = value
                    break
    return info


# --- sources the rules read (each is replaced by a fake in the tests) ---------------------------


def parse_vdf(text):
    """Valve's KeyValues text (appmanifest_*.acf, libraryfolders.vdf, localconfig.vdf) as nested dicts."""
    root, stack, key = {}, [], None
    current = root
    for quoted, brace in re.findall(r'"((?:[^"\\]|\\.)*)"|([{}])', text):
        if brace == "{":
            child = {}
            current[key if key is not None else ""] = child
            stack.append(current)
            current, key = child, None
        elif brace == "}":
            current, key = (stack.pop() if stack else root), None
        elif key is None:
            key = quoted
        else:
            current[key] = quoted.replace("\\\\", "\\")
            key = None
    return root


def vget(d, *keys):
    """Case-insensitive path into a parsed VDF; None if missing."""
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = next((v for kk, v in d.items() if kk.lower() == k.lower()), None)
    return d


def steam_games(steam):
    """[{appid, name, folder, parts, size_on_disk, last_played}] from every Steam library's
    appmanifest_*.acf; last_played comes from the users' localconfig.vdf (0: never)."""
    if steam is None:
        return []
    libraries = [Path(steam)]
    try:
        vdf = parse_vdf((Path(steam) / "steamapps" / "libraryfolders.vdf").read_text(encoding="utf-8", errors="replace"))
        for entry in (vget(vdf, "libraryfolders") or {}).values():
            if isinstance(entry, dict) and entry.get("path"):
                libraries.append(Path(entry["path"]))
    except OSError:
        pass
    played = {}
    for config in (Path(steam) / "userdata").glob("*/config/localconfig.vdf"):
        try:
            apps = vget(parse_vdf(config.read_text(encoding="utf-8", errors="replace")),
                        "UserLocalConfigStore", "Software", "Valve", "Steam", "apps") or {}
        except OSError:
            continue
        for appid, data in apps.items():
            try:
                played[appid] = max(played.get(appid, 0), int(vget(data, "LastPlayed") or 0))
            except (TypeError, ValueError):
                pass
    games, seen = [], set()
    for lib in libraries:
        if norm(lib) in seen:
            continue
        seen.add(norm(lib))
        apps = lib / "steamapps"
        for acf in sorted(apps.glob("appmanifest_*.acf")):
            try:
                state = vget(parse_vdf(acf.read_text(encoding="utf-8", errors="replace")), "AppState") or {}
            except OSError:
                continue
            appid = str(state.get("appid") or acf.stem.split("_")[-1])
            installdir = state.get("installdir") or ""
            games.append({
                "appid": appid, "name": state.get("name") or f"Steam app {appid}",
                "folder": str(apps / "common" / installdir) if installdir else "",
                "parts": [str(apps / "shadercache" / appid), str(apps / "workshop" / "content" / appid)],
                "size_on_disk": int(state.get("SizeOnDisk") or 0), "last_played": played.get(appid, 0),
                "manifest": str(acf),
            })
    return games


UNINSTALL = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"


def registry_programs():
    """Installed programs from the registry's Uninstall keys (64-bit, 32-bit, the user's), without
    hidden components, updates and Steam's own per-game entries (games come from Steam)."""
    out = []
    for hive, flag, scope in ((winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY, "hklm64"),
                              (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY, "hklm32"),
                              (winreg.HKEY_CURRENT_USER, 0, "hkcu")):
        try:
            root = winreg.OpenKey(hive, UNINSTALL, 0, winreg.KEY_READ | flag)
        except OSError:
            continue
        with root:
            for i in range(winreg.QueryInfoKey(root)[0]):
                try:
                    sub = winreg.EnumKey(root, i)
                    with winreg.OpenKey(root, sub) as key:
                        values = {}
                        for j in range(winreg.QueryInfoKey(key)[1]):
                            name, value, _ = winreg.EnumValue(key, j)
                            values[name] = value
                except OSError:
                    continue
                values["_key"] = f"{scope}\\{sub}"
                out.append(values)
    return out


def python_installs():
    """[(version, folder, uninstall command or "")] of the base Pythons: PEP 514 registry entries and
    uv's managed Pythons."""
    out = []
    for hive, flag in ((winreg.HKEY_CURRENT_USER, 0), (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
                       (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY)):
        try:
            core = winreg.OpenKey(hive, r"Software\Python\PythonCore", 0, winreg.KEY_READ | flag)
        except OSError:
            continue
        with core:
            for i in range(winreg.QueryInfoKey(core)[0]):
                try:
                    tag = winreg.EnumKey(core, i)
                    with winreg.OpenKey(core, tag + r"\InstallPath") as key:
                        folder = winreg.QueryValueEx(key, "")[0]
                except OSError:
                    continue
                out.append((tag, folder.rstrip("\\")))
    uv_python = Path(os.environ.get("UV_PYTHON_INSTALL_DIR", "") or
                     Path(os.environ.get("APPDATA") or Path.home()) / "uv" / "python")
    if uv_python.is_dir():
        for d in uv_python.iterdir():
            if (d / "python.exe").is_file():
                match = re.search(r"(\d+\.\d+(?:\.\d+)?)", d.name)
                out.append((match.group(1) if match else d.name, str(d)))
    return out


def _lnk_target(path):
    """The local target of a .lnk (its LinkInfo's LocalBasePath + CommonPathSuffix), or ""."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return ""
    if len(data) < 76 or data[:4] != b"\x4c\x00\x00\x00":
        return ""
    flags = struct.unpack_from("<I", data, 20)[0]
    off = 76
    if flags & 0x1:  # HasLinkTargetIDList
        off += 2 + struct.unpack_from("<H", data, off)[0]
    if not flags & 0x2:  # HasLinkInfo
        return ""
    try:
        (size, header, info_flags, _, base_off, _, suffix_off) = struct.unpack_from("<7I", data, off)
        if not info_flags & 0x1:
            return ""
        if header >= 0x24:
            base_u, suffix_u = struct.unpack_from("<2I", data, off + 28)
            base = _wstr(data, off + base_u)
            suffix = _wstr(data, off + suffix_u)
        else:
            base = data[off + base_off:data.index(b"\x00", off + base_off)].decode("mbcs", "replace")
            suffix = data[off + suffix_off:data.index(b"\x00", off + suffix_off)].decode("mbcs", "replace")
    except (struct.error, ValueError):
        return ""
    return os.path.join(base, suffix) if suffix else base


def _wstr(data, off):
    end = off
    while end + 1 < len(data) and data[end:end + 2] != b"\x00\x00":
        end += 2
    return data[off:end].decode("utf-16-le", "replace")


def _command_exe(command):
    """The program a command line runs (its first token, quoted or up to .exe)."""
    command = os.path.expandvars((command or "").strip())
    if command.startswith('"'):
        return command[1:].split('"', 1)[0]
    match = re.match(r"(.+?\.(?:exe|bat|cmd|com))(?:\s|$)", command, re.I)
    return match.group(1) if match else command.split(" ")[0]


def links(pl):
    """Shortcuts (Start menu, desktop, Startup), Run entries and scheduled tasks: [{source, name, where,
    target}]. Only ones whose target is a file path."""
    out = []
    folders = [("shortcut", pl.roaming / "Microsoft" / "Windows" / "Start Menu" / "Programs"),
               ("shortcut", pl.program_data / "Microsoft" / "Windows" / "Start Menu" / "Programs"),
               ("shortcut", pl.home / "Desktop"), ("shortcut", pl.drive / "Users" / "Public" / "Desktop")]
    for source, folder in folders:
        for lnk in folder.rglob("*.lnk") if folder.is_dir() else []:
            if "startup" in (p.lower() for p in lnk.parts):
                source_here = "startup"
            else:
                source_here = source
            target = _lnk_target(lnk)
            if target:
                out.append({"source": source_here, "name": lnk.stem, "where": str(lnk), "target": target})
    for hive, flag, label in ((winreg.HKEY_CURRENT_USER, 0, "HKCU"),
                              (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY, "HKLM"),
                              (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY, "HKLM32")):
        try:
            key = winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_READ | flag)
        except OSError:
            continue
        with key:
            for j in range(winreg.QueryInfoKey(key)[1]):
                name, value, _ = winreg.EnumValue(key, j)
                if isinstance(value, str) and value.strip():
                    out.append({"source": "startup", "name": name, "where": f"{label}\\...\\Run",
                                "target": _command_exe(value)})
    out += _scheduled_tasks()
    return [x for x in out if re.match(r"^[A-Za-z]:\\", x["target"] or "")]


def _scheduled_tasks():
    script = ("Get-ScheduledTask | Where-Object { $_.TaskPath -notlike '\\Microsoft\\*' } | ForEach-Object { "
              "$t = $_; $t.Actions | Where-Object { $_.Execute } | ForEach-Object { "
              "[pscustomobject]@{n = $t.TaskPath + $t.TaskName; e = $_.Execute} } } | ConvertTo-Json -Compress")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True,
                             timeout=60, creationflags=NO_CREATE_WINDOW).stdout.strip()
        rows = json.loads(out) if out else []
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    if isinstance(rows, dict):
        rows = [rows]
    return [{"source": "task", "name": r.get("n", ""), "where": "Task Scheduler", "target": _command_exe(r.get("e"))}
            for r in rows if r.get("e")]


def prefetch_times(folder):
    """{EXE NAME: last run} from Windows' Prefetch files (their mtime is the last run), or None when the
    folder can't be read (it needs admin rights)."""
    if folder is None:
        return None
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return None
    times = {}
    for e in entries:
        if e.name.lower().endswith(".pf") and "-" in e.name:
            exe = e.name.rsplit("-", 1)[0].upper()
            try:
                times[exe] = max(times.get(exe, 0), e.stat().st_mtime)
            except OSError:
                pass
    return times


class Sources:
    """The scanner's view of Steam, the registry, shortcuts and git. Tests swap in fakes."""

    def steam_games(self, pl):
        return steam_games(pl.steam)

    def programs(self):
        return registry_programs()

    def pythons(self):
        return python_installs()

    def links(self, pl):
        return links(pl)

    def prefetch(self, pl):
        return prefetch_times(pl.prefetch)

    def git_last_commit(self, folder):
        if not (Path(folder) / ".git").exists():
            return 0
        try:
            out = subprocess.run(["git", "-C", str(folder), "log", "-1", "--format=%ct"], capture_output=True,
                                 text=True, timeout=20, creationflags=NO_CREATE_WINDOW).stdout.strip()
            return float(out) if out else 0
        except (OSError, subprocess.SubprocessError, ValueError):
            return 0

    def exe_info(self, path):
        return exe_info(path)

    def disk_usage(self, pl):
        import shutil

        usage = shutil.disk_usage(str(pl.drive))
        return usage.total, usage.used, usage.free


# --- program rules ------------------------------------------------------------------------------

SYSTEM_PROGRAM = re.compile(
    r"visual c\+\+|\.net|redistributable|runtime|webview2|microsoft edge|update health|gameinput|directx|"
    r"windows (sdk|software development|driver|app certification|subsystem|pc health)|\bdriver\b|chipset|"
    r"management engine|physx|hd audio|frameview|microsoft visual studio installer", re.I)
SYSTEM_PUBLISHER = re.compile(r"^(microsoft|intel|realtek|advanced micro devices|nvidia)", re.I)
UPDATE_TYPES = {"update", "hotfix", "security update"}


def is_system_program(name, publisher):
    """Windows' own pieces and drivers: no delete button (Settings > Apps is the place for them)."""
    return bool(SYSTEM_PUBLISHER.match(publisher or "") and SYSTEM_PROGRAM.search(name or ""))


def _program_folder(values, pl, containers):
    """The program's own folder: InstallLocation, else the folder of its icon / uninstaller when that is
    an ordinary install folder. "" when unknown."""
    candidates = [values.get("InstallLocation")]
    for name in ("DisplayIcon", "UninstallString"):
        exe = _command_exe(str(values.get(name) or "").split(",")[0])
        if exe.lower().endswith(".exe"):
            candidates.append(os.path.dirname(exe))
    for i, folder in enumerate(candidates):
        folder = str(folder or "").strip().strip('"').rstrip("\\")
        if not folder or not os.path.isabs(folder) or not os.path.isdir(folder):
            continue
        n = norm(folder)
        if n in containers or n == norm(pl.drive) or under(folder, pl.windows) \
                or under(folder, pl.program_data / "Package Cache"):
            continue
        if i > 0 and not any(under(folder, p) for p in (*pl.program_files, pl.local / "Programs")):
            continue  # an icon somewhere else isn't proof of the install folder
        return folder
    return ""


# --- the scan -----------------------------------------------------------------------------------


class Scanner:
    def __init__(self, places=None, sources=None, lang="tr", cache=None, progress=None):
        self.pl = places or Places.real()
        self.src = sources or Sources()
        self.lang = lang
        self.cache = cache if cache is not None else {}
        self.progress = progress or (lambda text: None)
        self.items = []
        self.roots = {}            # norm(folder) -> item index
        self.containers = _containers(self.pl)
        self.loose = {}            # container norm -> item index
        self.code_files = {}       # project item index -> [(path, size, mtime)]
        self.exe_names = {}        # item index -> {EXE NAME}  (only when Prefetch is readable)
        self.prefetch = None
        self.ids, self.owners, self.allocs = array("q"), array("i"), array("q")
        self.stats = {"dirs": 0, "files": 0, "unreadable": 0}

    def t(self, key, **kw):
        return i18n.get(self.lang, key, **kw)

    def add(self, item, folders=()):
        """Registers an item and the folders charged to it (first come, first served)."""
        idx = len(self.items)
        self.items.append(item)
        for folder in [item.path, *item.parts, *folders]:
            if folder and norm(folder) not in self.roots:
                self.roots[norm(folder)] = idx
        return idx

    # -- items known before the walk --

    def register(self):
        pl = self.pl
        self.prefetch = self.src.prefetch(pl)
        self.add(Item("system:windows", "system", "Windows", str(pl.windows), key="system:windows", opaque=True))
        self.add(Item("system:recycle", "system", self.t("disk_name_recycle"), str(pl.drive / "$Recycle.Bin"),
                      key="system:recycle", opaque=True, redownloadable=False))
        if (pl.drive / "Windows.old").is_dir():
            self.add(Item("system:windows.old", "system", "Windows.old", str(pl.drive / "Windows.old"),
                          key="system:windows.old", opaque=True))
        if pl.failsafe.is_dir():
            self.add(Item("path:" + norm(pl.failsafe), "own", "Failsafe", str(pl.failsafe), key="own:failsafe",
                          redownloadable=False, opaque=True))
        if pl.umay_app is not None:
            self.add(Item("app:umay", "app", "Umay", str(pl.umay_app), key="app:umay", opaque=True))
        if pl.voice_home.is_dir():
            self.add(Item("path:" + norm(pl.voice_home), "app", self.t("disk_name_voice"), str(pl.voice_home),
                          key="app:umay-voice"))
        self._register_mei()
        for key, folder in pl.caches():
            if folder.is_dir() and norm(folder) not in self.roots:
                self.add(Item("path:" + norm(folder), "cache", self.t(f"disk_cache_{key}"), str(folder),
                              key=f"cache:{key}", opaque=True, info={"cache": key}))
        if pl.temp.is_dir() and norm(pl.temp) not in self.roots:
            self.add(Item("path:" + norm(pl.temp), "cache", self.t("disk_cache_temp"), str(pl.temp),
                          key="cache:temp", info={"cache": "temp"}))
        self._register_hf()
        self._register_steam()
        self._register_pythons()
        self._register_programs()

    def _register_mei(self):
        own = norm(self.pl.own_meipass) if self.pl.own_meipass else ""
        leftovers = []
        try:
            entries = list(os.scandir(self.pl.temp))
        except OSError:
            entries = []
        for e in entries:
            if not (e.is_dir(follow_symlinks=False) and re.fullmatch(r"_MEI\d+", e.name, re.I)):
                continue
            if norm(e.path) == own:
                self.add(Item("mei:own", "mei", self.t("disk_name_mei_own"), e.path, key="mei:own", opaque=True))
            elif os.path.isfile(os.path.join(e.path, "base_library.zip")):
                leftovers.append(e.path)
        if leftovers:
            self.add(Item("mei:leftovers", "mei", self.t("disk_name_mei", count=len(leftovers)), leftovers[0],
                          parts=leftovers[1:], key="mei:leftovers", opaque=True, info={"count": len(leftovers)}))

    def _register_hf(self):
        hub = self.pl.hf_hub
        if hub is None or not hub.is_dir():
            return
        self.add(Item("path:" + norm(hub), "cache", self.t("disk_cache_hf"), str(hub), key="cache:hf", opaque=True,
                      container=True, protected=self.t("disk_protect_container")))  # the models are items
        for d in sorted(hub.iterdir()):
            m = re.fullmatch(r"(models|datasets|spaces)--(.+)", d.name)
            if d.is_dir() and m:
                model_id = m.group(2).replace("--", "/")
                self.add(Item("path:" + norm(d), "model", model_id if m.group(1) == "models"
                              else f"{m.group(1)[:-1]}: {model_id}", str(d), key=f"model:{model_id.lower()}",
                              opaque=True, info={"model_id": model_id, "repo_type": m.group(1)[:-1]}))

    def _register_steam(self):
        for g in self.src.steam_games(self.pl):
            if not g["folder"] or not under(g["folder"], self.pl.drive):
                continue
            apps = Path(g["folder"]).parent.parent  # <library>\steamapps
            for sub, kind in ((apps / "common", "common"), (apps / "workshop" / "content", "workshop"),
                              (apps / "content", "depot")):
                self.containers.setdefault(norm(sub), (sub, Container("folder", steam=kind)))
            item = Item(f"steam:{g['appid']}", "game", g["name"], g["folder"],
                        parts=[p for p in g["parts"] if os.path.isdir(p)], key=f"steam:{g['appid']}", opaque=True,
                        info={"appid": g["appid"], "size_on_disk": g["size_on_disk"], "manifest": g.get("manifest", "")})
            if g["last_played"]:
                item.last_used, item.used_source = float(g["last_played"]), "steam"
            else:
                item.used_source = "steam_never"
            if g["appid"] == "228980":  # Steamworks Common Redistributables: Steam's own shared installers
                item.protected = self.t("disk_protect_steam_common")
            self.add(item)

    def _register_pythons(self):
        for version, folder in self.src.pythons():
            if not os.path.isdir(folder) or norm(folder) in self.roots:
                continue
            self.add(Item("path:" + norm(folder), "python", f"Python {version}", folder, key=f"python:{version}",
                          opaque=True, info={"version": version}))

    def _register_programs(self):
        seen = {}
        for v in self.src.programs():
            name = str(v.get("DisplayName") or "").strip()
            if not name or v.get("SystemComponent") == 1 or v.get("ParentKeyName") \
                    or str(v.get("ReleaseType") or "").lower() in UPDATE_TYPES \
                    or re.match(r"^\w+\\Steam App \d+$", v["_key"]):
                continue
            publisher = str(v.get("Publisher") or "").strip()
            ident = f"{name.lower()}|{publisher.lower()}"
            if ident in seen:
                continue
            seen[ident] = True
            folder = _program_folder(v, self.pl, self.containers)
            info = {"publisher": publisher, "version": str(v.get("DisplayVersion") or ""),
                    "install_date": str(v.get("InstallDate") or ""), "uninstall": str(v.get("UninstallString") or ""),
                    "registry": v["_key"]}
            python = self._python_for(name, publisher)
            if python is not None:  # python.org's installer: the Python item gets its uninstaller
                python.info.update(uninstall=info["uninstall"], registry=info["registry"])
                continue
            item = Item(f"program:{v['_key'].lower()}", "program", name, folder, key=f"program:{ident}",
                        opaque=True, info=info)
            if is_system_program(name, publisher):
                item.protected = self.t("disk_protect_system_program")
            if folder and norm(folder) in self.roots:
                other = self.items[self.roots[norm(folder)]]
                if other.kind in ("program", "game", "python"):
                    item.info["same_folder_as"] = other.name
                    folder = item.path = ""
            if not folder:
                try:
                    item.size, item.size_source = int(v.get("EstimatedSize") or 0) * 1024, "registry"
                except (TypeError, ValueError):
                    item.size_source = "registry"
            self.add(item)

    def _python_for(self, name, publisher):
        m = re.match(r"python (\d+\.\d+)", name, re.I)
        if not m or "python software foundation" not in publisher.lower():
            return None
        for it in self.items:
            if it.kind == "python" and it.info.get("version", "").startswith(m.group(1)) \
                    and not it.info.get("uninstall") and under(it.path, self.pl.home):
                return it
        return None

    # -- items met on the way --

    def _loose_item(self, key, folder):
        idx = self.loose.get(key)
        if idx is None:
            item = Item("loose:" + key, "folder", self.t("disk_name_loose", folder=self._short(folder)), str(folder),
                        key="", container=True, redownloadable=False)
            item.protected = self.t("disk_protect_container")
            idx = len(self.items)
            self.items.append(item)
            self.loose[key] = idx
        return idx

    def _child_item(self, folder, spec, listing):
        name = os.path.basename(folder)
        low = name.lower()
        kind = spec.child_kind
        if kind == "own" and spec is self.containers.get(norm(self.pl.home), (None, None))[1] \
                and low not in OWN_FOLDERS and not low.startswith("onedrive"):
            kind = "folder"
        if spec.steam:  # no appmanifest owns it: left behind by an uninstalled game (mods, saves, downloads)
            item = Item("path:" + norm(folder), "folder", self.t(f"disk_name_steam_{spec.steam}", name=name), folder,
                        key="", redownloadable=False, info={"steam_leftover": spec.steam})
            item.what, item.what_source = self.t(f"disk_what_steam_{spec.steam}"), "rule"
        elif kind == "project":
            item = Item("path:" + norm(folder), "project", name, folder, key=f"project:{low}", redownloadable=False)
        elif kind == "own":
            item = Item("path:" + norm(folder), "own", name, folder, key=f"own:{low}", redownloadable=False)
        else:
            item = Item("path:" + norm(folder), "folder", self._short(folder), folder,
                        key=f"folder:{self._short(folder).lower()}", redownloadable=False,
                        info={"installs": spec.installs})
        if low in spec.system:
            item.kind, item.redownloadable, item.opaque = "system", True, True
            item.protected = self.t("disk_protect_system")
        return self.add(item)

    def _new_venv(self, folder):
        parent = os.path.basename(os.path.dirname(folder))
        item = Item("path:" + norm(folder), "venv", f"{parent}\\{os.path.basename(folder)}", folder,
                    key=f"venv:{parent.lower()}\\{os.path.basename(folder).lower()}", opaque=True)
        return self.add(item)

    def _short(self, folder):
        """A folder's name for the list: relative to the user's folder when inside it."""
        folder = str(folder)
        if under(folder, self.pl.home) and norm(folder) != norm(self.pl.home):
            return "~\\" + os.path.relpath(folder, self.pl.home)
        return folder

    # -- the walk --

    def walk(self):
        stack = [(str(self.pl.drive), -1, None)]
        prefetch = self.prefetch is not None
        items, roots, containers = self.items, self.roots, self.containers
        ids, owners, allocs = self.ids, self.owners, self.allocs
        last_report = time.monotonic()
        while stack:
            folder, owner, parent_spec = stack.pop()
            listing = list_dir(folder)
            self.stats["dirs"] += 1
            if listing is None:
                self.stats["unreadable"] += 1
                continue
            key = norm(folder)
            spec = containers.get(key)
            idx = roots.get(key)
            if idx is not None:
                owner = idx
            elif spec is not None:
                owner = self._loose_item(key, spec[0])
            elif (owner < 0 or not items[owner].opaque) and any(
                    e[0].lower() == "pyvenv.cfg" and not e[1] & ATTR_DIRECTORY for e in listing):
                owner = self._new_venv(folder)
            elif parent_spec is not None:
                owner = self._child_item(folder, parent_spec, listing)
            it = items[owner] if owner >= 0 else None
            spec_here = spec[1] if spec is not None else None
            base = folder.rstrip("\\") + "\\"
            collect_code = it is not None and it.kind == "project"
            for name, attrs, alloc, eof, mtime, fid, tag in listing:
                if attrs & ATTR_DIRECTORY:
                    if tag in LINK_TAGS:
                        continue
                    stack.append((base + name, owner, spec_here))
                    continue
                if it is None or tag in LINK_TAGS:
                    continue
                if attrs & (ATTR_COMPRESSED | ATTR_SPARSE) or tag in PACKED_TAGS:
                    alloc = packed_size(base + name, alloc)
                it.size += alloc
                it.files += 1
                if it.container and alloc >= MIN_FOLDER_BYTES:  # what makes a folder's loose files big
                    top = it.info.setdefault("top_files", [])
                    top.append([name, alloc])
                    top.sort(key=lambda x: -x[1])
                    del top[3:]
                if mtime > it.newest:
                    it.newest = mtime
                low = name.lower()
                if low.endswith(".exe"):
                    if eof > it.info.get("_exe_size", 0):
                        it.info["_exe_size"], it.info["_exe"] = eof, base + name
                    if prefetch:
                        self.exe_names.setdefault(owner, set()).add(name.upper())
                elif collect_code and low.endswith(CODE_SUFFIXES) and eof <= CODE_MAX_BYTES:
                    self.code_files.setdefault(owner, []).append((base + name, eof, mtime))
                if alloc > 0:
                    ids.append(fid)
                    owners.append(owner)
                    allocs.append(alloc)
            self.stats["files"] += len(listing)
            if time.monotonic() - last_report > 2:
                last_report = time.monotonic()
                self.progress(self.t("disk_progress", dirs=self.stats["dirs"], gb=sum(x.size for x in items) / 1e9))

    def dedupe_hardlinks(self):
        """Every file id met more than once is a hard link: only its first item keeps the bytes."""
        if not self.ids:
            return
        import numpy as np

        ids = np.frombuffer(self.ids, dtype=np.int64)
        order = np.argsort(ids, kind="stable")
        sorted_ids = ids[order]
        repeat = np.zeros(len(ids), dtype=bool)
        repeat[1:] = sorted_ids[1:] == sorted_ids[:-1]
        owners = np.frombuffer(self.owners, dtype=np.int32)[order][repeat]
        allocs = np.frombuffer(self.allocs, dtype=np.int64)[order][repeat]
        sums = np.bincount(owners, weights=allocs, minlength=len(self.items)) if len(owners) else []
        for owner, total in enumerate(sums):
            if total:
                self.items[owner].size -= int(total)
                self.items[owner].shared += int(total)
        self.stats["hardlinked_bytes"] = int(allocs.sum())

    # -- after the walk --

    def analyse(self):
        for idx, it in enumerate(self.items):
            exe = it.info.pop("_exe", "")
            it.info.pop("_exe_size", None)
            if it.kind == "venv":
                self._venv(it)
            elif it.kind == "project":
                self._project(idx, it)
            elif it.kind == "folder" and not it.container and exe:
                self._folder_exe(it, exe)
            elif it.kind == "program":
                self._program_use(idx, it)
            if it.kind in NOT_REDOWNLOADABLE:
                it.redownloadable = False
            if it.container and norm(it.path) == norm(self.pl.drive):  # pagefile.sys, hiberfil.sys, swapfile.sys
                top = ", ".join(f"{n} {s / 1e9:.1f} GB" for n, s in it.info.get("top_files", []))
                it.kind, it.name, it.redownloadable = "system", self.t("disk_name_root"), True
                it.what, it.what_source = self.t("disk_what_root", files=top or "—"), "rule"
                it.protected = self.t("disk_protect_root")
            if not it.what:
                rule = RULE_WHAT.get(it.kind)
                if it.kind == "cache":
                    rule = "disk_what_cache_" + it.info.get("cache", "hf")
                elif it.container:
                    rule = "disk_what_loose"
                elif it.id == "mei:own":
                    rule = "disk_what_mei_own"
                if rule:
                    it.what, it.what_source = self.t(rule), "rule"
            if not it.used_source and it.newest:
                it.last_used, it.used_source = it.newest, "newest_file"
        self._umay_needs()
        self._links()

    def _venv(self, it):
        cfg = {}
        try:
            for line in Path(it.path, "pyvenv.cfg").read_text(encoding="utf-8", errors="replace").splitlines():
                if "=" in line:
                    k, _, v = line.partition("=")
                    cfg[k.strip().lower()] = v.strip()
        except OSError:
            pass
        version = cfg.get("version") or cfg.get("version_info") or "?"
        home = cfg.get("home", "")
        site = Path(it.path) / "Lib" / "site-packages"
        packages = self._packages(site)
        it.info.update(python=version, home=home, packages=packages, package_count=len(packages),
                       system_site=cfg.get("include-system-site-packages", "false").lower() == "true")
        if home:
            it.needs.append(home)
        pth = []
        for f in site.glob("*.pth") if site.is_dir() else []:
            try:
                lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in lines:
                line = line.strip()
                if not line or line.startswith(("#", "import ", "import\t")):
                    continue
                target = line if os.path.isabs(line) else os.path.normpath(os.path.join(site, line))
                pth.append(target)
                it.needs.append(target)
        it.info["pth"] = pth
        notable = [p for p in NOTABLE_PACKAGES if p in packages]
        it.what = self.t("disk_what_venv", python=version.rsplit(".", 1)[0] if version.count(".") > 1 else version,
                         count=len(packages), notable=", ".join(notable[:4]) or "—")
        it.what_source = "rule"

    def _packages(self, site):
        """{name: version} from site-packages' *.dist-info folders (cached by the folder's mtime)."""
        try:
            stamp = os.stat(site).st_mtime
        except OSError:
            return {}
        cached = self.cache.setdefault("venv", {}).get(norm(site))
        if cached and cached[0] == stamp:
            return cached[1]
        packages = {}
        for d in os.scandir(site):
            if d.name.endswith(".dist-info"):
                name, _, version = d.name[:-len(".dist-info")].partition("-")
                packages[name.lower().replace("_", "-")] = version
        self.cache["venv"][norm(site)] = [stamp, packages]
        return packages

    def _project(self, idx, it):
        when = self.src.git_last_commit(it.path)
        if when:
            it.last_used, it.used_source = when, "git"
        hf = {it2.info["model_id"].lower(): it2.path for it2 in self.items if it2.kind == "model"}
        found = set()
        code_cache = self.cache.setdefault("code", {})
        for path, size, mtime in self.code_files.get(idx, []):
            cached = code_cache.get(norm(path))
            if cached and cached[0] == size and cached[1] == mtime:
                ids = cached[2]
            else:
                try:
                    text = Path(path).read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                ids = sorted({m.lower() for m in MODEL_ID.findall(text)})
                code_cache[norm(path)] = [size, mtime, ids]
            found.update(i for i in ids if i in hf)
        it.info["models"] = sorted(found)
        it.needs += [hf[m] for m in sorted(found)]
        it.what, it.what_source = self.t("disk_what_project"), "rule"

    def _folder_exe(self, it, exe):
        cache = self.cache.setdefault("exe", {})
        try:
            st = os.stat(exe)
        except OSError:
            return
        cached = cache.get(norm(exe))
        if cached and cached[0] == st.st_size and cached[1] == st.st_mtime:
            info = cached[2]
        else:
            info = self.src.exe_info(exe)
            cache[norm(exe)] = [st.st_size, st.st_mtime, info]
        if not info:
            return
        it.info["exe"] = {"file": os.path.basename(exe), **info}
        product = info.get("ProductName") or info.get("FileDescription") or ""
        company = info.get("CompanyName", "")
        if it.info.get("steam_leftover"):  # the rule says what it is; app_<id> gets the game's name from its exe
            if product and it.info["steam_leftover"] != "common":
                it.name += f" ({product})"
        elif product and it.info.get("installs"):  # in AppData the biggest exe is often a helper's or a plug-in's
            it.key = f"product:{product.lower()}|{company.lower()}"
            it.what = " · ".join(x for x in (info.get("FileDescription") or product, company) if x)
            it.what_source = "exe"
            it.redownloadable = True  # an installed product (Program Files, Programs, C:\)

    def _program_use(self, idx, it):
        if self.prefetch is None:
            return
        names = self.exe_names.get(idx, set()) - GENERIC_EXES
        times = [self.prefetch[n] for n in names if n in self.prefetch]
        if times:
            it.last_used, it.used_source = max(times), "prefetch"

    def _umay_needs(self):
        voice = self.pl.voice_home
        needs = [str(voice / "env"), str(voice / "tts_env"), str(voice / "models"), str(voice / "tts_models"),
                 str(self.pl.wichelberg_app)]
        for it in self.items:
            if it.kind == "app" and it.id == "app:umay" or (
                    it.kind == "project" and self.pl.umay_source and norm(it.path) == norm(self.pl.umay_source)):
                it.needs += [n for n in needs if n not in it.needs]

    def _links(self):
        for x in self.src.links(self.pl):
            item = Item(f"link:{x['source']}:{x['where']}:{x['name']}".lower(), "link", x["name"],
                        x["where"] if os.path.isabs(x["where"]) else "", key="", needs=[x["target"]],
                        info={"source": x["source"], "target": x["target"], "where": x["where"]})
            item.protected = self.t("disk_protect_link")
            item.what, item.what_source = self.t(f"disk_what_link_{x['source']}"), "rule"
            self.items.append(item)

    def run(self):
        started = time.monotonic()
        self.register()
        self.progress(self.t("disk_progress_start"))
        self.walk()
        self.dedupe_hardlinks()
        self.analyse()
        build_graph(self.items)
        protect(self.items, self.pl, self.lang)
        needed = {d for it in self.items for d in it.dep_ids}
        kept = [it for it in self.items if it.size >= MIN_FOLDER_BYTES or it.id in needed
                or it.kind not in ("folder", "system", "own") or it.id in ("system:windows", "system:recycle")]
        total, used, free = self.src.disk_usage(self.pl)
        self.stats.update(seconds=round(time.monotonic() - started, 1), prefetch=self.prefetch is not None,
                          items=len(kept))
        return Scan(time.time(), kept, total, used, free, dict(self.stats))


NOTABLE_PACKAGES = ["torch", "tensorflow", "jax", "transformers", "diffusers", "onnxruntime", "onnxruntime-gpu",
                    "onnxruntime-directml", "ctranslate2", "faster-whisper", "openai-whisper", "chatterbox-tts",
                    "opencv-python", "pyinstaller", "django", "flask", "fastapi", "pandas", "numpy", "mediapipe",
                    "bpy", "pyqt5", "pyside6", "pygame", "selenium", "playwright"]
GENERIC_EXES = {"UNINS000.EXE", "UNINSTALL.EXE", "UNINST.EXE", "SETUP.EXE", "UPDATE.EXE", "UPDATER.EXE",
                "LAUNCHER.EXE", "CRASHPAD_HANDLER.EXE", "CRASHREPORTER.EXE", "HELPER.EXE", "INSTALLER.EXE"}
RULE_WHAT = {"game": "disk_what_game", "model": "disk_what_model", "mei": "disk_what_mei",
             "python": "disk_what_python", "own": "disk_what_own", "system": "disk_what_system",
             "app": "disk_what_app"}
MODEL_ID = re.compile(r"""["']([A-Za-z0-9][\w.-]*/[\w.-]+)["']""")


# --- dependency graph and protection -------------------------------------------------------------


def _owner_index(items):
    """norm(folder) -> item for every item folder (containers' loose files only claim their own files)."""
    index = {}
    for it in items:
        if it.container or it.kind == "link":
            continue
        for folder in [it.path, *it.parts]:
            if folder:
                index.setdefault(norm(folder), it)
    return index


def owner_of(path, index):
    """The item whose folder holds path (the deepest one), or None."""
    p = norm(path)
    while True:
        if p in index:
            return index[p]
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent


def build_graph(items, exists=os.path.exists):
    """Fills dep_ids (the items an item's needed paths belong to) and broken (needed paths that are
    gone). Run again after a delete: whatever lost what it needs turns red."""
    index = _owner_index(items)
    for it in items:
        it.dep_ids, it.broken = [], []
        for need in it.needs:
            if not exists(need):
                it.broken.append(need)
                continue
            owner = owner_of(need, index)
            if owner is not None and owner is not it and owner.id not in it.dep_ids:
                it.dep_ids.append(owner.id)


def inside(item, items):
    """Items whose folders are inside this one's (they go with it)."""
    if item.container or not item.path:
        return []
    mine = [item.path, *item.parts]
    return [o for o in items if o is not item and o.kind != "link" and not o.container and o.path
            and any(under(o.path, m) for m in mine)]


def dependents(item, items):
    """What breaks if this item goes: the items inside it, then everything that needs it or them."""
    going = [item] + inside(item, items)
    ids = {g.id for g in going}
    needing = [o for o in items if o.id not in ids and ids.intersection(o.dep_ids)]
    return going[1:], needing


def protect(items, pl, lang="tr"):
    """Sets `protected` (no delete button) by the owner's rules."""
    t = lambda key, **kw: i18n.get(lang, key, **kw)  # noqa: E731
    rules = [(pl.windows, t("disk_protect_windows")), (pl.voice_home, t("disk_protect_voice")),
             (pl.egitim_venv, t("disk_protect_egitim")), (pl.failsafe, t("disk_protect_failsafe"))]
    if pl.own_meipass:
        rules.append((Path(pl.own_meipass), t("disk_protect_own_mei")))
    if pl.umay_app is not None:
        rules.append((pl.umay_app, t("disk_protect_umay")))
    by_id = {it.id: it for it in items}
    for it in items:
        if it.protected or not it.path:
            continue
        for folder, reason in rules:
            if under(it.path, folder):
                it.protected = reason
                break
        if not it.protected and it.kind == "system":
            it.protected = t("disk_protect_system")
    for it in items:  # a venv another venv reaches through a .pth
        for target in it.info.get("pth", []) if it.kind == "venv" else []:
            owner = next((by_id[d] for d in it.dep_ids if by_id.get(d) is not None
                          and by_id[d].kind == "venv" and under(target, by_id[d].path)), None)
            if owner is not None and not owner.protected:
                owner.protected = t("disk_protect_pth", venv=it.name)
    guards = [it for it in items if it.protected and it.path and not it.container and it.kind != "link"]
    for it in items:
        if it.protected or not it.path:
            continue
        holder = next((g for g in guards if g is not it and any(under(it.path, p) for p in [g.path, *g.parts])), None)
        if holder is not None:
            it.protected = t("disk_protect_inside", name=holder.name)


def duplicate_venvs(items):
    """Groups of venvs with the same Python version and the same packages (name and version)."""
    groups = {}
    for it in items:
        if it.kind == "venv" and it.info.get("packages"):
            sig = (it.info.get("python"), tuple(sorted(it.info["packages"].items())))
            groups.setdefault(sig, []).append(it)
    return [g for g in groups.values() if len(g) > 1]


def scan(places=None, sources=None, lang="tr", cache=None, progress=None):
    """Walks the drive and returns a Scan (read-only). cache: a dict kept between scans (disk_memory
    stores it) so package lists, exe info and project code are only read again when they changed."""
    return Scanner(places, sources, lang, cache, progress).run()


def set_background_priority():
    """Low CPU and disk priority for the calling thread (the daily scan runs while the PC is idle)."""
    try:
        kernel32.SetThreadPriority(kernel32.GetCurrentThread(), 0x00010000)  # THREAD_MODE_BACKGROUND_BEGIN
    except Exception:
        pass
