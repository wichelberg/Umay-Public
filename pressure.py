"""Memory pressure: how short of RAM the PC is, for the HUD and for a log the owner (and Claude Code) can read afterwards.

Why: the 35B model and the NPU's server together outgrew the RAM (2026-11-16): Windows pushed the NPU server's memory out
to the disk, a tool choice took 6-33 s instead of 2.5, and the HUD said nothing because the model's file pages don't
count as used (health_monitor / hud_data leave them out). "Available RAM" alone isn't the signal either: it reads
0.3-0.6 GB in benchmark runs that were fast. What costs time is Windows reading pages back from the disk, so pressure
needs both: little RAM free AND pages coming in from the disk (the "Pages Input/sec" counter, read through PDH: no
process is started, a reading is ~1 ms).

The log: one line every LOG_SECONDS in %LOCALAPPDATA%\\Umay\\pressure\\pressure-YYYY-MM-DD.log (~110 bytes a line, ~0.6 MB a
day), files older than KEEP_DAYS days are deleted. The raw values are logged, not the level: the thresholds below are a first
guess to be set from a week of these lines. UMAY_PRESSURE_LOG=off stops the log.
"""
import ctypes
import os
import threading
import time
from ctypes import wintypes
from datetime import datetime, timedelta
from pathlib import Path

import psutil

import llama_server

LOG_SECONDS = 15
KEEP_DAYS = 7
LOW_AVAIL_GB = 4.0          # first guess: under this the RAM is short
PAGING_WARN = 100.0         # pages read in from the disk per second (the slow period measured 118-694)
PAGING_HIGH = 500.0
GB = 2 ** 30

_lock = threading.Lock()
_state = {"on": False, "faults": {}}
_pdh = {"query": None, "counter": None, "tried": False}


class PerformanceInformation(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("CommitTotal", ctypes.c_size_t), ("CommitLimit", ctypes.c_size_t),
                ("CommitPeak", ctypes.c_size_t), ("PhysicalTotal", ctypes.c_size_t), ("PhysicalAvailable", ctypes.c_size_t),
                ("SystemCache", ctypes.c_size_t), ("KernelTotal", ctypes.c_size_t), ("KernelPaged", ctypes.c_size_t),
                ("KernelNonpaged", ctypes.c_size_t), ("PageSize", ctypes.c_size_t), ("HandleCount", wintypes.DWORD),
                ("ProcessCount", wintypes.DWORD), ("ThreadCount", wintypes.DWORD)]


class CounterValue(ctypes.Structure):
    _fields_ = [("CStatus", wintypes.DWORD), ("doubleValue", ctypes.c_double)]


def commit_gb():
    """(committed, the commit limit) in GB: what the programs have promised themselves, against RAM plus the page file.
    Over the physical RAM means Windows has to page. None when the call fails."""
    try:
        info = PerformanceInformation()
        info.cb = ctypes.sizeof(info)
        if not ctypes.windll.psapi.GetPerformanceInfo(ctypes.byref(info), info.cb):
            return None
        return info.CommitTotal * info.PageSize / GB, info.CommitLimit * info.PageSize / GB
    except (OSError, AttributeError):
        return None


def pagein_per_s():
    """Pages per second Windows read from the disk into RAM (PDH "\\Memory\\Pages Input/sec"). None until the second
    reading (a rate needs two) or when PDH isn't there."""
    with _lock:
        try:
            pdh = ctypes.WinDLL("pdh")
            if not _pdh["tried"]:
                _pdh["tried"] = True
                query, counter = ctypes.c_void_p(), ctypes.c_void_p()
                if pdh.PdhOpenQueryW(None, 0, ctypes.byref(query)) == 0 and \
                        pdh.PdhAddEnglishCounterW(query, "\\Memory\\Pages Input/sec", 0, ctypes.byref(counter)) == 0:
                    _pdh["query"], _pdh["counter"] = query, counter
            if _pdh["query"] is None:
                return None
            if pdh.PdhCollectQueryData(_pdh["query"]) != 0:
                return None
            value = CounterValue()
            if pdh.PdhGetFormattedCounterValue(_pdh["counter"], 0x200, None, ctypes.byref(value)) != 0 or value.CStatus != 0:
                return None
            return float(value.doubleValue)
        except (OSError, AttributeError):
            return None


def level(avail_gb, pagein):
    """0 fine, 1 short of RAM and paging, 2 short and paging hard. Needs BOTH: a low "available" alone is normal while
    a model's file cache fills the RAM."""
    if avail_gb is None or pagein is None or avail_gb >= LOW_AVAIL_GB:
        return 0
    return 2 if pagein >= PAGING_HIGH else 1 if pagein >= PAGING_WARN else 0


def _process_numbers():
    """Working set and private memory (GB) of the llama-server and flm processes, and flm's page faults a second.
    memory_info() only: memory_full_info() walks every page and took 300-440 ms on the 35B (2026-11-16)."""
    out = {"llama_ws": 0.0, "llama_priv": 0.0, "flm_ws": 0.0, "flm_priv": 0.0, "flm_faults": None}
    now, faults = time.monotonic(), 0
    for p in psutil.process_iter(["name"]):
        try:
            name = (p.info["name"] or "").lower()
            if "llama-server" in name:
                info = p.memory_info()
                out["llama_ws"] += info.rss / GB
                out["llama_priv"] += getattr(info, "private", 0) / GB
            elif name == "flm.exe":
                info = p.memory_info()
                out["flm_ws"] += info.rss / GB
                out["flm_priv"] += getattr(info, "private", 0) / GB
                faults += getattr(info, "num_page_faults", 0)
        except (psutil.Error, OSError):
            pass
    before = _state["faults"].get("flm")
    if before and now > before[0] and faults >= before[1]:
        out["flm_faults"] = (faults - before[1]) / (now - before[0])
    _state["faults"]["flm"] = (now, faults)
    return out


def reading():
    """The raw numbers, one dict: available / total RAM, the commit, the page file's use, pages read in a second, the
    two servers' memory."""
    memory = psutil.virtual_memory()
    commit = commit_gb()
    r = {"avail": memory.available / GB, "total": memory.total / GB, "pagefile_pct": psutil.swap_memory().percent,
         "commit": commit[0] if commit else None, "commit_limit": commit[1] if commit else None,
         "pagein": pagein_per_s(), "game": bool(llama_server.hold["on"])}
    r.update(_process_numbers())
    r["level"] = level(r["avail"], r["pagein"])
    return r


def _f(value, digits=1):
    return "?" if value is None else f"{value:.{digits}f}"


def line(r):
    return (f"{datetime.now():%H:%M:%S} avail={_f(r['avail'])} commit={_f(r['commit'])}/{_f(r['commit_limit'])} "
            f"pagefile={_f(r['pagefile_pct'], 0)}% pagein={_f(r['pagein'], 0)}/s llama_ws={_f(r['llama_ws'])} "
            f"llama_priv={_f(r['llama_priv'])} flm_ws={_f(r['flm_ws'])} flm_priv={_f(r['flm_priv'])} "
            f"flm_faults={_f(r['flm_faults'], 0)}/s game={int(r['game'])} level={r['level']}")


def log_dir():
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Umay" / "pressure"


def prune(today=None):
    """Deletes the log files older than KEEP_DAYS days. Returns the names it deleted."""
    today = today or datetime.now()
    gone = []
    for path in log_dir().glob("pressure-*.log"):
        try:
            day = datetime.strptime(path.stem.split("pressure-")[1], "%Y-%m-%d")
        except (ValueError, IndexError):
            continue
        if today - day > timedelta(days=KEEP_DAYS):
            try:
                path.unlink()
                gone.append(path.name)
            except OSError:
                pass
    return gone


def write_line(text):
    folder = log_dir()
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / f"pressure-{datetime.now():%Y-%m-%d}.log", "a", encoding="utf-8") as f:
        f.write(text + "\n")


def _loop():
    last_prune = 0.0
    while True:
        try:
            if time.monotonic() - last_prune > 6 * 3600:
                prune()
                last_prune = time.monotonic()
            write_line(line(reading()))
        except Exception as e:  # noqa: BLE001  a sensor that fails must not end the log
            try:
                write_line(f"{datetime.now():%H:%M:%S} reading failed: {type(e).__name__}: {str(e)[:80]}")
            except OSError:
                pass
        time.sleep(LOG_SECONDS)


def start():
    """Starts the log thread (health_monitor.start does), once. UMAY_PRESSURE_LOG=off: none."""
    if os.environ.get("UMAY_PRESSURE_LOG", "").strip().lower() in ("off", "0", "false", "no"):
        return False
    with _lock:
        if _state["on"]:
            return False
        _state["on"] = True
    threading.Thread(target=_loop, name="pressure-log", daemon=True).start()
    return True
