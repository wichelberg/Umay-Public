"""Notices when Windows undoes the owner's trimming: an update turning hibernation or Windows AI back on, a program
starting with Windows again, a removed program installed again (the owner, 2026-11-11, after freeing 77 GB of disk
and ~20 GB of commit: "will a Windows update bring these back? Umay could check").

The first check takes a baseline (what is off now stays off); every later one compares with it:
- hibernation: C:\\hiberfil.sys there again (it was gone);
- Windows AI: the WindowsAI policy (DisableAIDataAnalysis) gone again;
- Windows AI Components Host (service WSAIFabricSvc, which starts the WorkloadsSessionHost processes: ~3 GB of RAM,
  measured 2026-11-15): switched from Disabled to on again. Nothing needs it (not the eyes, not the NPU server);
- startup: an entry that was off is on again, or a new one starts with Windows (Run keys and the Startup folders, by
  Task Manager's own on/off: StartupApproved);
- programs: one whose name matches UMAY_WINDOWS_UNWANTED (a regex, e.g. "GiMATE|WebAdvisor") installed again.

Each finding is told once (companion, "important": aloud at the PC, Telegram when away), again only after it was
fixed and came back. Told aloud it is easy to miss, so it also stays in the state file as an alert (alerts()) while
it lasts: the HUD shows a "!" above the muted-microphone spot, bottom right, and a click lists what came back (the
owner's ask, 2026-11-15). Marked seen there (hud.json), it is shown again only when it comes back after a fix. It only reads: putting things right needs admin rights, so it names the fix (FIX_SCRIPT if it
exists). Every CHECK_HOURS, first FIRST_CHECK_MINUTES after Umay starts. State: %LOCALAPPDATA%\\Umay\\windows_watch.json.
.env: UMAY_WINDOWS_WATCH (off = never), UMAY_WINDOWS_UNWANTED.
"""
import json
import os
import re
import threading
import time
import winreg
from datetime import datetime
from pathlib import Path

import paths

CHECK_HOURS = 6
FIRST_CHECK_MINUTES = 10
HIBERFIL = Path(os.environ.get("SystemDrive", "C:") + "\\") / "hiberfil.sys"
AI_KEY = r"Software\Policies\Microsoft\Windows\WindowsAI"
AI_FABRIC_KEY = r"SYSTEM\CurrentControlSet\Services\WSAIFabricSvc"
FIX_SCRIPT = Path.home() / "Desktop" / "windows_trim_admin.ps1"
RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN32 = r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run"
APPROVED = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved"
UNINSTALL = (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
             r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall")
STARTUP_FOLDERS = (Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs\Startup",
                   Path(os.environ.get("ProgramData", "")) / r"Microsoft\Windows\Start Menu\Programs\StartUp")


def enabled():
    return os.environ.get("UMAY_WINDOWS_WATCH", "").strip().lower() != "off"


def _state_path():
    return paths.voice_home().parent / "windows_watch.json"


def _log(text):
    try:
        with open(paths.voice_home().parent / "windows_watch.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}\n")
    except OSError:
        pass


# --- reading Windows (each one alone, so the tests give their own) -----------------------------------------------

def _values(root, key):
    """{name: data} of a registry key's values ({} when it isn't there)."""
    out = {}
    try:
        with winreg.OpenKey(root, key) as k:
            i = 0
            while True:
                try:
                    name, data, _ = winreg.EnumValue(k, i)
                except OSError:
                    return out
                out[name] = data
                i += 1
    except OSError:
        return out


def _off(approved, name):
    """Task Manager turned it off: StartupApproved's first byte is odd (2 = on, 3 = off)."""
    data = approved.get(name)
    return isinstance(data, (bytes, bytearray)) and len(data) > 0 and data[0] & 1 == 1


def startup():
    """(on, off): the names of what starts with Windows now and of what Task Manager turned off, from the Run keys
    (this user's, the machine's, 32-bit) and the Startup folders."""
    on, off = set(), set()
    for root, key, approved_key in ((winreg.HKEY_CURRENT_USER, RUN, APPROVED + r"\Run"),
                                    (winreg.HKEY_LOCAL_MACHINE, RUN, APPROVED + r"\Run"),
                                    (winreg.HKEY_LOCAL_MACHINE, RUN32, APPROVED + r"\Run32")):
        approved = _values(root, approved_key)
        for name in _values(root, key):
            if name:
                (off if _off(approved, name) else on).add(name)
    for root, folder in ((winreg.HKEY_CURRENT_USER, STARTUP_FOLDERS[0]), (winreg.HKEY_LOCAL_MACHINE, STARTUP_FOLDERS[1])):
        approved = _values(root, APPROVED + r"\StartupFolder")
        try:
            for f in folder.iterdir():
                if f.suffix.lower() == ".lnk":
                    (off if _off(approved, f.name) else on).add(f.name)
        except OSError:
            pass
    return on, off


def programs():
    """Installed programs' names (Programs and Features)."""
    names = set()
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for key in UNINSTALL:
            try:
                with winreg.OpenKey(root, key) as k:
                    for i in range(winreg.QueryInfoKey(k)[0]):
                        try:
                            with winreg.OpenKey(k, winreg.EnumKey(k, i)) as sub:
                                names.add(str(winreg.QueryValueEx(sub, "DisplayName")[0]))
                        except OSError:
                            continue
            except OSError:
                continue
    return names


def ai_policy():
    """Windows AI's data analysis (Recall) is turned off by policy."""
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        if _values(root, AI_KEY).get("DisableAIDataAnalysis") == 1:
            return True
    return False


def ai_fabric_off():
    """True: the Windows AI Components Host service is Disabled (Start = 4); False: it can start; None: no such service."""
    start = _values(winreg.HKEY_LOCAL_MACHINE, AI_FABRIC_KEY).get("Start")
    return None if start is None else start == 4


def read():
    """What the checks compare: {hiberfil, ai_policy, ai_fabric_off, startup (on), startup_off, programs}."""
    on, off = startup()
    return {"hiberfil": HIBERFIL.exists(), "ai_policy": ai_policy(), "ai_fabric_off": ai_fabric_off(),
            "startup": sorted(on), "startup_off": sorted(off), "programs": sorted(programs())}


# --- the rules (pure: tests/test_windows_watch.py) ----------------------------------------------------------------

def findings(baseline, now, unwanted=""):
    """{key: text} of what Windows undid since the baseline. unwanted: a regex of programs that must stay removed."""
    out = {}
    if now["hiberfil"] and not baseline["hiberfil"]:
        try:
            size = f" ({HIBERFIL.stat().st_size / 2 ** 30:.1f} GB)"
        except OSError:
            size = ""
        out["hiberfil"] = f"the hibernation file is back on the disk{size}"
    if baseline["ai_policy"] and not now["ai_policy"]:
        out["ai_policy"] = "Windows AI (Recall) is allowed again"
    if baseline.get("ai_fabric_off") and now.get("ai_fabric_off") is False:
        out["ai_fabric"] = "Windows AI Components Host is on again (~3 GB RAM)"
    for name in sorted(set(now["startup"]) - set(baseline["startup"])):
        again = name in baseline.get("startup_off", [])
        out["startup:" + name] = f"{name} starts with Windows {'again' if again else 'now (new)'}"
    if unwanted.strip():
        try:
            pattern = re.compile(unwanted, re.IGNORECASE)
        except re.error:
            pattern = None
        for name in now["programs"]:
            if pattern and pattern.search(name):
                out["program:" + name] = f"{name} is installed again"
    return out


def alert_list(old, found, at):
    """The alerts the HUD shows: one per finding still there, keeping when it was first found ({key, text, at})."""
    kept = {a.get("key"): a for a in old or [] if isinstance(a, dict) and a.get("key") in found}
    return [kept.get(k) or {"key": k, "text": found[k], "at": at} for k in sorted(found)]


def alert_id(alert):
    """Seen is remembered per finding and per time it was found: one that comes back after a fix is new."""
    return f"{alert.get('key')}@{alert.get('at')}"


def unseen(alerts_, seen):
    """The alerts whose id isn't in `seen` (the ids the owner marked seen in the HUD)."""
    seen = set(seen or ())
    return [a for a in alerts_ or [] if alert_id(a) not in seen]


def message(new):
    """One line for the owner about the new findings (texts)."""
    fix = (f" Running {FIX_SCRIPT.name} on the Desktop again puts it right." if FIX_SCRIPT.exists()
           else " Putting it right needs admin rights.")
    return "Windows undid some of the trimming: " + "; ".join(new) + "." + fix


# --- the running check --------------------------------------------------------------------------------------------

def _load():
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def alerts():
    """For the HUD (its own process, reads the file): [{key, text, at}] of what Windows undid and isn't put right."""
    return [a for a in _load().get("alerts", []) if isinstance(a, dict)]


def _save(state):
    try:
        _state_path().write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def check(tell, now=None):
    """One check: the first one takes the baseline; later ones tell what's new (tell(text) -> delivered or not).
    Returns the findings' keys."""
    state = _load()
    now = read() if now is None else now
    if not state.get("baseline"):
        state = {"baseline": now, "taken": datetime.now().isoformat(timespec="seconds"), "told": []}
        _save(state)
        _log(f"baseline taken: hiberfil {now['hiberfil']}, AI policy {now['ai_policy']}, "
             f"{len(now['startup'])} startup entries on")
        return []
    if "ai_fabric_off" not in state["baseline"] and now.get("ai_fabric_off"):
        state["baseline"]["ai_fabric_off"] = True  # the baseline is older than this check: it learns it once it is off
    found = findings(state["baseline"], now, os.environ.get("UMAY_WINDOWS_UNWANTED", ""))
    state["alerts"] = alert_list(state.get("alerts"), found, datetime.now().isoformat(timespec="seconds"))
    told = set(state.get("told", [])) & set(found)  # fixed since: told again if it comes back
    new = {k: v for k, v in found.items() if k not in told}
    if new:
        _log("found: " + "; ".join(new.values()))
        if tell(message(new.values())):
            told |= set(new)
    state["told"] = sorted(told)
    _save(state)
    return sorted(found)


_started = {"on": False}


def start(tell):
    """The background check (main.py). tell(text) -> True when it reached the owner."""
    if _started["on"] or not enabled():
        return
    _started["on"] = True

    def loop():
        time.sleep(FIRST_CHECK_MINUTES * 60)
        while True:
            try:
                check(tell)
            except Exception as e:  # never stops Umay: the next round tries again
                _log(f"error: {type(e).__name__}: {e}")
            time.sleep(CHECK_HOURS * 3600)
    threading.Thread(target=loop, name="windows-watch", daemon=True).start()
