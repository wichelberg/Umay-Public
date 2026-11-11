"""Deleting a disk item the way its kind is meant to go, and the _MEI clean-up Umay does when she
starts.

plan(item) only says how (nothing runs): a Steam game -> steam://uninstall/<appid>; a program (or a
python.org Python) -> its own uninstaller; the pip / uv cache -> `pip cache purge` / `uv cache clean`;
a venv and every other re-downloadable folder -> deleted for good; Temp -> its files older than a day;
the owner's own files and projects (and unknown folders) -> the Recycle Bin. Protected items,
shortcuts and folders that only hold other items have no plan. execute() does it; the panel asks the
owner first and lists what goes with the item and what breaks.

clean_mei(): the exe is built with PyInstaller --onefile, which unpacks itself into Temp\\_MEI<n> and
deletes that folder on a normal exit, but not when the process is ended by force. At start Umay
deletes the leftovers: only PyInstaller's (base_library.zip inside), at least an hour old, never her
own, and never one that is still in use: a running exe's loaded DLLs can't be opened for writing,
and renaming the folder fails while a file in it is open.
"""
import ctypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import winreg
from dataclasses import dataclass, field
from pathlib import Path

from disk_tools import NO_CREATE_WINDOW, norm, under

MEI_MIN_AGE_SECONDS = 3600
TEMP_MIN_AGE_SECONDS = 86400
TOMB = ".umay-delete"
# PyInstaller's unpack folder: _MEI and a hex number ("_MEI000070f02" since PyInstaller 6). With \d+ only the
# all-digit ones were cleaned: 177 folders of ~0.5 GB, 25 GB of Temp, piled up by 2026-11-11.
MEI_NAME = r"_MEI[0-9a-f]+"
DEFAULT_BIN_SHARE = 0.05  # Windows' default Recycle Bin size: 5% of the drive


@dataclass
class Plan:
    mode: str                     # url | uninstaller | command | delete | trash | contents
    item_id: str
    name: str
    command: object = None        # url / uninstall string / argv list
    paths: list = field(default_factory=list)
    size: int = 0

    @property
    def runs_elsewhere(self):
        """Steam or an uninstaller finishes in its own window: the result isn't known here."""
        return self.mode in ("url", "uninstaller")

    def describe(self):
        if isinstance(self.command, list):
            return " ".join(f'"{c}"' if " " in c else c for c in self.command)
        return str(self.command or "")


def _python_command():
    py = shutil.which("py")
    if py:
        return [py, "-3"]
    python = shutil.which("python")
    return [python] if python else None


def _uv_command():
    uv = shutil.which("uv")
    if uv:
        return uv
    for candidate in (Path.home() / ".local" / "bin" / "uv.exe", Path.home() / ".cargo" / "bin" / "uv.exe"):
        if candidate.is_file():
            return str(candidate)
    return None


def plan(item, python=None, uv=None):
    """How this item would be deleted (Plan), or None when it has no delete. python / uv: the commands
    to use (found on PATH when not given; tests pass their own)."""
    if item.protected or item.container or item.kind in ("link", "system", "app"):
        return None
    folders = [p for p in [item.path, *item.parts] if p]
    base = dict(item_id=item.id, name=item.name, paths=folders, size=item.size)
    if item.kind == "game":
        appid = item.info.get("appid")
        return Plan("url", command=f"steam://uninstall/{appid}", **base) if appid else None
    if item.kind in ("program", "python") and item.info.get("uninstall"):
        return Plan("uninstaller", command=item.info["uninstall"], **base)
    if item.kind == "program":
        return None  # no uninstaller: deleting its folder would leave Windows thinking it's installed
    cache = item.info.get("cache")
    if cache == "pip":
        python = python if python is not None else _python_command()
        if python:
            return Plan("command", command=[*python, "-m", "pip", "cache", "purge"], **base)
    if cache == "uv":
        uv = uv if uv is not None else _uv_command()
        if uv:
            return Plan("command", command=[uv, "cache", "clean"], **base)
    if cache == "temp":
        return Plan("contents", **base)
    if not folders:
        return None
    if item.redownloadable and item.kind != "folder":
        return Plan("delete", **base)
    return Plan("trash", **base)  # also every unknown folder: an exe's version info isn't enough to delete for good


# --- doing it -----------------------------------------------------------------------------------


class Refused(Exception):
    """A path the delete must never touch (the safety net under the protection rules)."""


def check_path(path, pl):
    """Raises Refused unless path is an ordinary folder that holds nothing protected."""
    if not path or not os.path.isabs(path):
        raise Refused(f"not a full path: {path!r}")
    p = norm(path)
    if p == norm(pl.drive) or os.path.dirname(p) == p:
        raise Refused("a drive root")
    never = [pl.windows, pl.home, pl.documents, pl.local, pl.roaming, pl.program_data, *pl.program_files,
             pl.temp, pl.drive / "Users", *pl.project_roots]
    if any(p == norm(n) for n in never):
        raise Refused(f"{path} is a top folder")
    keep = [pl.windows, pl.voice_home, pl.egitim_venv, pl.failsafe]
    if pl.umay_app is not None:
        keep.append(pl.umay_app)
    if pl.own_meipass:
        keep.append(Path(pl.own_meipass))
    for k in keep:
        if under(path, k) or under(k, path):
            raise Refused(f"{path} is or holds {k}")


class Runner:
    """What execute() does to the PC; tests swap in a fake one."""

    def open_url(self, url):
        os.startfile(url)

    def start(self, command):
        subprocess.Popen(command)  # the uninstaller's own window

    def run(self, argv):
        done = subprocess.run(argv, capture_output=True, text=True, timeout=900, creationflags=NO_CREATE_WINDOW)
        if done.returncode:
            raise RuntimeError((done.stderr or done.stdout).strip()[-400:] or f"exit code {done.returncode}")

    def delete(self, path):
        rmtree(path)

    def trash(self, path):
        recycle(path)

    def in_bin(self, path, since):
        return in_recycle_bin(path, since)

    def free(self, drive):
        return shutil.disk_usage(str(drive)).free


# --- the Recycle Bin ---------------------------------------------------------------------------------
# Not send2trash: its SHFileOperation flags (FOF_NOCONFIRMATION without FOF_WANTNUKEWARNING) let Windows
# delete for good, without asking, whatever doesn't fit in the bin. On 2026-10-21 a 10.8 GB folder sent
# to the "Recycle Bin" wasn't found there afterwards. Now: enough free room in the bin is checked first,
# Windows warns before it would ever delete for good, and the bin is checked for the item afterwards.

FO_DELETE = 3
FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI, FOF_WANTNUKEWARNING = 0x4, 0x10, 0x40, 0x400, 0x4000


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", ctypes.c_void_p), ("wFunc", ctypes.c_uint), ("pFrom", ctypes.c_wchar_p),
                ("pTo", ctypes.c_wchar_p), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", ctypes.c_int),
                ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", ctypes.c_wchar_p)]


def recycle(path):
    """Moves path to the Recycle Bin. Never deletes for good without Windows asking first."""
    op = _SHFILEOPSTRUCTW(None, FO_DELETE, os.path.abspath(path) + "\0\0", None,
                          FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_WANTNUKEWARNING | FOF_SILENT | FOF_NOERRORUI,
                          0, None, None)
    code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if op.fAnyOperationsAborted:
        raise RuntimeError("cancelled")
    if code:
        raise RuntimeError(f"SHFileOperation error 0x{code:x}")


def _bin_folders(drive=None):
    """The readable per-user folders of the drives' Recycle Bins (only the owner's own one is readable)."""
    drives = [str(drive)] if drive else [f"{c}:\\" for c in "CDEFGHIJ" if os.path.isdir(f"{c}:\\")]
    for d in drives:
        root = os.path.join(d, "$Recycle.Bin")
        try:
            entries = list(os.scandir(root))
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir() and os.listdir(e.path) is not None:
                    yield e.path
            except OSError:
                continue


def recycle_bin_used(drive):
    """Bytes already in this drive's Recycle Bin (the owner's part of it)."""
    total = 0
    for folder in _bin_folders(drive):
        for dirpath, _, names in os.walk(folder):
            for name in names:
                try:
                    total += os.path.getsize(os.path.join(dirpath, name))
                except OSError:
                    pass
    return total


def _bin_entry_path(data):
    """The original path stored in a $I file (Windows 10+: version 2), or ""."""
    if len(data) < 28 or int.from_bytes(data[:8], "little") != 2:
        return ""
    length = int.from_bytes(data[24:28], "little")
    return data[28:28 + 2 * length].decode("utf-16-le", "replace").rstrip("\0")


def in_recycle_bin(path, since=0):
    """True if the Recycle Bin got path at `since` or later (a $I file that names it)."""
    want = norm(path)
    for folder in _bin_folders(os.path.splitdrive(os.path.abspath(path))[0] + "\\"):
        for e in os.scandir(folder):
            if not e.name.startswith("$I"):
                continue
            try:
                if e.stat().st_mtime < since:
                    continue
                with open(e.path, "rb") as f:
                    if norm(_bin_entry_path(f.read())) == want:
                        return True
            except OSError:
                continue
    return False


def rmtree(path):
    """Deletes a folder for good (read-only files too); a junction inside is removed, never followed."""
    def retry(func, p, _):
        os.chmod(p, 0o666)
        func(p)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)


def recycle_bin_limit(drive, total):
    """Bytes the drive's Recycle Bin holds (its MaxCapacity setting, else Windows' default 5%)."""
    buf = ctypes.create_unicode_buffer(64)
    try:
        if ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(str(drive), buf, 64):
            guid = re.search(r"\{[^}]+\}", buf.value).group(0)
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer"
                                rf"\BitBucket\Volume\{guid}") as key:
                return int(winreg.QueryValueEx(key, "MaxCapacity")[0]) * 1024 ** 2
    except (OSError, AttributeError, ValueError):
        pass
    return int(total * DEFAULT_BIN_SHARE)


@dataclass
class Result:
    ok: bool
    started: bool = False         # handed to Steam / an uninstaller
    freed: int = 0
    error: str = ""
    skipped: list = field(default_factory=list)
    bin_free: int = 0             # with too_big_for_bin: room left in the Recycle Bin


def execute(p, pl, runner=None, bin_limit=None, bin_used=None):
    """Does the plan. pl: disk_tools.Places (the safety net checks every folder against it)."""
    runner = runner or Runner()
    before = runner.free(pl.drive)
    try:
        if p.mode == "url":
            runner.open_url(p.command)
            return Result(True, started=True)
        if p.mode == "uninstaller":
            runner.start(p.command)
            return Result(True, started=True)
        if p.mode == "command":
            runner.run(p.command)
        elif p.mode == "delete":
            for path in p.paths:
                check_path(path, pl)
            skipped = []
            for path in p.paths:
                if re.fullmatch(MEI_NAME, os.path.basename(path), re.I):
                    if not _remove_mei(path):
                        skipped.append(path)
                elif os.path.exists(path):
                    runner.delete(path)
            return Result(True, freed=max(0, runner.free(pl.drive) - before), skipped=skipped)
        elif p.mode == "trash":
            for path in p.paths:
                check_path(path, pl)
            limit = bin_limit if bin_limit is not None else recycle_bin_limit(pl.drive, shutil.disk_usage(str(pl.drive)).total)
            room = limit - (bin_used if bin_used is not None else recycle_bin_used(pl.drive))
            if p.size > room:  # Windows would delete it for good: never
                return Result(False, error="too_big_for_bin", bin_free=max(0, room))
            since = time.time() - 2
            for path in p.paths:
                runner.trash(path)
            lost = [path for path in p.paths if not os.path.exists(path) and not runner.in_bin(path, since)]
            if lost:
                return Result(False, error="not_in_bin", skipped=lost)
        elif p.mode == "contents":
            for path in p.paths:
                if norm(path) != norm(pl.temp):
                    check_path(path, pl)
            skipped = clean_old_files(p.paths[0], own=pl.own_meipass, runner=runner)
            return Result(True, freed=max(0, runner.free(pl.drive) - before), skipped=skipped)
        else:
            return Result(False, error=f"unknown plan {p.mode}")
    except Refused as e:
        return Result(False, error=f"refused: {e}")
    except Exception as e:
        return Result(False, error=str(e))
    return Result(True, freed=max(0, runner.free(pl.drive) - before))


def clean_old_files(folder, own="", min_age=None, now=None, runner=None):
    """Deletes what lies in folder (Temp) and is older than min_age (a day); in-use files and the _MEI
    folders (a separate item) stay. Returns what couldn't be deleted."""
    runner = runner or Runner()
    now = now or time.time()
    min_age = TEMP_MIN_AGE_SECONDS if min_age is None else min_age
    skipped = []
    for e in os.scandir(folder):
        if re.fullmatch(MEI_NAME, e.name, re.I) or (own and norm(e.path) == norm(own)):
            continue
        try:
            st = e.stat(follow_symlinks=False)
            if now - max(st.st_mtime, st.st_ctime) < min_age:
                continue
            if e.is_dir(follow_symlinks=False):
                runner.delete(e.path)
            else:
                os.remove(e.path)
        except OSError:
            skipped.append(e.path)
    return skipped


# --- _MEI leftovers ---------------------------------------------------------------------------------


def in_use(folder):
    """True if a running program still uses a file in folder. A loaded DLL / .pyd can't be opened for
    writing (renaming its folder would still work, so that alone isn't proof); nothing is written."""
    for dirpath, _, names in os.walk(folder):
        for name in names:
            try:
                os.close(os.open(os.path.join(dirpath, name), os.O_RDWR | getattr(os, "O_BINARY", 0)))
            except FileNotFoundError:
                continue
            except OSError:
                return True
    return False


def _remove_mei(path):
    """Deletes a _MEI folder unless it's in use: no file may be loaded or open (in_use), and renaming
    it must work (that fails while a file in it is open without delete sharing)."""
    if in_use(path):
        return False
    tomb = path if path.endswith(TOMB) else path + TOMB
    if tomb != path:
        try:
            os.rename(path, tomb)
        except OSError:
            return False
    shutil.rmtree(tomb, ignore_errors=True)
    return not os.path.exists(tomb)


def clean_mei(temp=None, own=None, min_age=MEI_MIN_AGE_SECONDS, now=None):
    """Deletes PyInstaller's leftover Temp\\_MEI<n> folders. Returns (removed, kept): kept are the ones
    still in use (or that couldn't be deleted)."""
    temp = Path(temp or tempfile.gettempdir())
    own = getattr(sys, "_MEIPASS", "") if own is None else own
    now = now or time.time()
    removed, kept = [], []
    try:
        entries = list(os.scandir(temp))
    except OSError:
        return removed, kept
    for e in entries:
        m = re.fullmatch(MEI_NAME + "(" + re.escape(TOMB) + ")?", e.name, re.I)
        if not m or not e.is_dir(follow_symlinks=False):
            continue
        if own and norm(e.path) == norm(own):
            continue
        tombstone = bool(m.group(1))  # half-deleted last time: base_library.zip may be gone already
        if not tombstone and not os.path.isfile(os.path.join(e.path, "base_library.zip")):
            continue
        try:
            st = e.stat(follow_symlinks=False)
        except OSError:
            continue
        if now - max(st.st_ctime, st.st_mtime) < min_age:
            continue
        (removed if _remove_mei(e.path) else kept).append(e.path)
    return removed, kept
