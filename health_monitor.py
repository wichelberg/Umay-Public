"""Watches the PC's health (GPU temperature, RAM, CPU load, free disk space) and warns the owner.

A value has to stay past its limit for a while before it counts (short spikes are normal).
Then the owner gets a popup with sound on the PC and a Telegram message, both with two
buttons: end the program causing it, or leave it alone. Without an answer, Telegram
reminders follow every minute from minute 3 to 9. At minute 10 the value is measured again
and, if it's still past the limit, the process using most of that resource is ended without
asking (the owner chose this: a hot laptop matters more than unsaved work). Windows, other
users' processes and Umay herself are never ended (system_tools' protection rules).

A full disk is only reported: ending a program doesn't free space.

Limits come from .env (UMAY_HEALTH_*); UMAY_HEALTH=off turns the monitor off.
"""
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import psutil

import alert
import i18n
import paths
import system_tools
import telegram_listener

LANGUAGE = telegram_listener.LANGUAGE
POLL_SECONDS = 30
FIRST_REMINDER_MINUTES = 3
REMINDERS = 7  # one a minute: minutes 3..9
ACT_AFTER_MINUTES = 10
RECHECK_SECONDS = 30  # how often an open alarm checks whether things got back to normal
QUIET_MINUTES = 30  # after an alarm ends, the same check stays quiet this long
DISK_QUIET_HOURS = 6
CPU_SAMPLE_SECONDS = 2
GPU_COUNTER_TIMEOUT = 60
SOUND = "sounds/alert.mp3"

_state = {"running": False}
_active = set()  # checks with an open alarm
_quiet_until = {}  # check key -> monotonic time
_lock = threading.Lock()


def _setting(name, default):
    raw = os.environ.get(f"UMAY_HEALTH_{name}", "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


# --- readings: each returns (value, details for the message) or None if unavailable ----------

def _nvidia_smi():
    return shutil.which("nvidia-smi")


def _read_gpu_temp():
    exe = _nvidia_smi()
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW,
        ).stdout
        temps = [float(line) for line in out.split() if line.strip()]
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return (max(temps), {}) if temps else None


def _read_ram():
    return psutil.virtual_memory().percent, {}


def _read_cpu():
    return psutil.cpu_percent(interval=None), {}


def _read_disk():
    """The fixed drive with the least free space, as percent free."""
    worst = None
    for part in psutil.disk_partitions(all=False):
        if "cdrom" in part.opts or not part.fstype:
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        free = 100 - usage.percent
        if worst is None or free < worst[0]:
            worst = (free, {"drive": part.mountpoint.rstrip("\\")})
    return worst


# --- who is causing it -----------------------------------------------------------------------

def _gpu_usage_by_pid():
    """GPU engine utilization per process from Windows' performance counters (takes ~10 s)."""
    script = ("(Get-Counter '\\GPU Engine(*)\\Utilization Percentage').CounterSamples | "
              "Where-Object CookedValue -gt 0 | ForEach-Object { $_.InstanceName + '=' + $_.CookedValue }")
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True,
            timeout=GPU_COUNTER_TIMEOUT, creationflags=subprocess.CREATE_NO_WINDOW,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    usage = {}
    for line in out.splitlines():
        match = re.match(r"pid_(\d+)_.*=([\d.,]+)$", line.strip())
        if match:
            pid = int(match.group(1))
            usage[pid] = usage.get(pid, 0) + float(match.group(2).replace(",", "."))
    return usage


def _cpu_usage_by_pid():
    procs = list(psutil.process_iter())
    for p in procs:
        try:
            p.cpu_percent(None)
        except psutil.Error:
            pass
    time.sleep(CPU_SAMPLE_SECONDS)
    usage = {}
    for p in procs:
        try:
            usage[p.pid] = p.cpu_percent(None)
        except psutil.Error:
            pass
    return usage


def _ram_usage_by_pid():
    usage = {}
    for p in psutil.process_iter():
        try:
            usage[p.pid] = p.memory_info().rss
        except psutil.Error:
            pass
    return usage


def _culprit(usage_by_pid):
    """The process using most of the resource that Umay is allowed to end, or None."""
    own, user = system_tools._own_pids(), psutil.Process().username()
    for pid, amount in sorted(usage_by_pid().items(), key=lambda item: item[1], reverse=True):
        if amount <= 0:
            break
        try:
            proc = psutil.Process(pid)
            if not system_tools._protected_reason(proc, own, user):
                proc.name()  # still alive and readable
                return proc
        except psutil.Error:
            continue
    return None


# --- checks ----------------------------------------------------------------------------------

@dataclass
class Check:
    key: str
    read: Callable
    limit: float
    sustain_minutes: float
    usage_by_pid: Optional[Callable]  # None: nothing to end (disk)
    below: bool = False  # True when the value is bad below the limit (free disk space)
    quiet_minutes: float = QUIET_MINUTES

    def past(self, value):
        return value < self.limit if self.below else value >= self.limit

    def describe(self, value, details):
        return i18n.get(LANGUAGE, f"health_{self.key}", value=value, limit=self.limit, **details)


def _checks():
    sustain = _setting("SUSTAIN_MINUTES", 2)
    checks = [
        Check("ram", _read_ram, _setting("RAM_PERCENT", 92), sustain, _ram_usage_by_pid),
        Check("cpu", _read_cpu, _setting("CPU_PERCENT", 95), _setting("CPU_MINUTES", 5), _cpu_usage_by_pid),
        Check("disk", _read_disk, _setting("DISK_FREE_PERCENT", 5), 0, None, below=True,
              quiet_minutes=DISK_QUIET_HOURS * 60),
    ]
    if _nvidia_smi():
        checks.insert(0, Check("gpu_temp", _read_gpu_temp, _setting("GPU_TEMP", 87), sustain, _gpu_usage_by_pid))
    return checks


def start():
    """Starts the monitor in the background (unless UMAY_HEALTH=off)."""
    if os.environ.get("UMAY_HEALTH", "").strip().lower() in ("off", "0", "false", "no"):
        return
    ensure_running()


def ensure_running():
    """Starts the monitor if it isn't running, whatever UMAY_HEALTH says. Sleep mode calls this:
    the owner wants the PC's health watched whenever Umay works unattended."""
    with _lock:
        if _state["running"]:
            return
        _state["running"] = True
    psutil.cpu_percent(interval=None)  # the first reading only sets the baseline
    threading.Thread(target=_monitor, args=(_checks(),), daemon=True).start()


def _monitor(checks):
    since = {}  # check key -> monotonic time it went past its limit
    while True:
        now = time.monotonic()
        for check in checks:
            try:
                reading = check.read()
            except Exception as e:  # a broken sensor must not stop the other checks
                print(f"Health: reading {check.key} failed: {e}")
                reading = None
            if reading is None or not check.past(reading[0]):
                since.pop(check.key, None)
                continue
            since.setdefault(check.key, now)
            with _lock:
                if (now - since[check.key] < check.sustain_minutes * 60 or check.key in _active
                        or now < _quiet_until.get(check.key, 0)):
                    continue
                _active.add(check.key)
            threading.Thread(target=_alarm, args=(check, *reading), daemon=True).start()
        time.sleep(POLL_SECONDS)


# --- one alarm -------------------------------------------------------------------------------

def _alarm(check, value, details):
    try:
        _run_alarm(check, value, details)
    except Exception as e:
        print(f"Health: alarm for {check.key} failed: {e}")
    finally:
        with _lock:
            _active.discard(check.key)
            _quiet_until[check.key] = time.monotonic() + check.quiet_minutes * 60


def _say(text, pending=None):
    telegram_listener.send_alert(text, pending=pending)


def _run_alarm(check, value, details):
    what = check.describe(value, details)
    sound = str(paths.resource_dir() / SOUND)
    token = f"health-{check.key}-{time.time()}"

    if check.usage_by_pid is None:  # nothing to end: just tell
        text = i18n.get(LANGUAGE, "health_alert_report", what=what)
        alert.show_choice(token, text, sound, ["OK"], None)
        _say(text)
        return

    culprit = _culprit(check.usage_by_pid)
    name = f"{culprit.name()} (PID {culprit.pid})" if culprit else i18n.get(LANGUAGE, "health_unknown_culprit")
    text = i18n.get(LANGUAGE, "health_alert", what=what, culprit=name, minutes=ACT_AFTER_MINUTES)
    end_label = i18n.get(LANGUAGE, "health_button_end")
    keep_label = i18n.get(LANGUAGE, "health_button_keep")

    local = {}  # the popup's answer
    alert.show_choice(token, text, sound, [end_label, keep_label], lambda i: local.setdefault("end", i == 0))
    pending = telegram_listener.send_alert(text, end_label, keep_label)
    try:
        decision = _wait_for_answer(check, local, pending)
    finally:
        telegram_listener.forget_alert(pending)
        alert.close(token)

    if decision == "recovered":
        _say(i18n.get(LANGUAGE, "health_recovered", what=_describe_now(check)))
    elif decision == "keep":
        _say(i18n.get(LANGUAGE, "health_kept", minutes=int(check.quiet_minutes)))
    else:  # "end" (the owner said so) or "timeout" (no answer, still past the limit)
        _end_culprit(check, decision)


def _wait_for_answer(check, local, pending):
    started = last_check = time.monotonic()
    reminders = 0
    while True:
        if "end" in local:
            return "end" if local["end"] else "keep"
        if pending and pending["event"].is_set():
            return "end" if pending["ok"] else "keep"
        elapsed = time.monotonic() - started
        if time.monotonic() - last_check >= RECHECK_SECONDS:
            last_check = time.monotonic()
            reading = check.read()
            if reading is not None and not check.past(reading[0]):
                return "recovered"
        if reminders < REMINDERS and elapsed >= (FIRST_REMINDER_MINUTES + reminders) * 60:
            left = ACT_AFTER_MINUTES - FIRST_REMINDER_MINUTES - reminders
            _say(i18n.get(LANGUAGE, "health_reminder", what=_describe_now(check), minutes=left), pending)
            reminders += 1
        if elapsed >= ACT_AFTER_MINUTES * 60:
            return "timeout"
        time.sleep(1)


def _describe_now(check):
    reading = check.read()
    return check.describe(*reading) if reading else check.key


def _end_culprit(check, decision):
    if decision == "timeout":
        reading = check.read()
        if reading is not None and not check.past(reading[0]):
            _say(i18n.get(LANGUAGE, "health_recovered", what=check.describe(*reading)))
            return
    culprit = _culprit(check.usage_by_pid)  # measured again: it may have changed meanwhile
    if culprit is None:
        _say(i18n.get(LANGUAGE, "health_nothing_to_end", what=_describe_now(check)))
        return
    name = f"{culprit.name()} (PID {culprit.pid})"
    try:
        alive = system_tools.kill_tree(culprit)
    except (system_tools.ToolError, psutil.Error) as e:
        _say(i18n.get(LANGUAGE, "health_end_failed", culprit=name, error=e))
        return
    reason = i18n.get(LANGUAGE, "health_reason_" + ("asked" if decision == "end" else "timeout"),
                      minutes=ACT_AFTER_MINUTES)
    key = "health_end_failed" if alive else "health_ended"
    _say(i18n.get(LANGUAGE, key, culprit=name, reason=reason, error=f"{alive} still running"))
    time.sleep(POLL_SECONDS)
    _say(i18n.get(LANGUAGE, "health_after", what=_describe_now(check)))
