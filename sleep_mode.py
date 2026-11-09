"""Sleep mode: the owner hands over a job before going to bed and Umay sees it through alone.

The owner confirms once when the job starts; after that nothing waits for permission.

1. Backup. Every project in Documents\\Projects and Documents\\Work (not only the job's) is
   copied to Documents\\Failsafe\\<date>_<job>\\ before anything starts, so whatever changes has
   its "before" copy. If the disk can't hold it all, only the job's folders are copied and the
   report says so. While working, sleep_hook.ps1 (a Claude Code PreToolUse hook) copies any
   project folder not copied yet before its first change, and any file outside those roots
   before Edit/Write changes it. .venv, venv, node_modules and __pycache__ are left out
   (requirements.txt rebuilds them).
2. Work. Claude Code runs headless in "auto" permission mode: it doesn't ask, while a
   safety check still stops destructive or out-of-scope actions. It's told that a failed
   first attempt is part of the job: investigate, find its own solution, and explain why
   in the report. If it stops without saying the job is done, it's resumed and told to
   keep going (up to MAX_ROUNDS times).
3. Report. Each backed-up folder is compared with its copy (added / changed / deleted files);
   copies of folders that didn't change are deleted again. The report, with Claude Code's
   own account of what it did and why, is saved in every changed folder and in the
   Failsafe folder, and sent on Telegram.
4. The PC shuts down, with the usual 60-second warning that /iptal cancels.

The Telegram bridge folder (claude_comm, holds the bot token) and .env files are off-limits.
The PC is kept awake while the job runs.

The owner may also tell a Claude Code session directly "I'm going to sleep, sleep mode". That
session then follows the same rules through this file's command line (see the bottom):
`start` turns sleep mode on and backs up the job's folders, the global hook
~/.claude/hooks/umay_sleep_gate.py then runs sleep_hook.ps1 before each change, and
`finish` writes the report, sends it on Telegram and shuts the PC down.
"""
import argparse
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import claude_bridge
import i18n
import paths
import pc_tools
import system_tools

LANGUAGE_NAMES = {"tr": "Turkish", "en": "English"}
SKIP_DIRS = {".venv", "venv", "node_modules", "__pycache__"}
DONE_MARKER = "UMAY_SLEEP_DONE"
MAX_ROUNDS = 5
ROUND_TIMEOUT_SECONDS = 12 * 60 * 60  # only for a real lock-up: a training run may take hours
REPORT_LIST_LIMIT = 40
HOOK_TOOLS = "Edit|Write|MultiEdit|NotebookEdit|Bash|PowerShell"

ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001

FLAG_FILE = Path.home() / ".claude" / "umay_sleep_mode.json"  # sleep mode is on in Claude Code
COMM_SCRIPT = Path.home() / "Documents/Projects/Claude_Communication_Wichelberg/claude_comm.py"
CLI_SHUTDOWN_SECONDS = 60
FULL_BACKUP_MARGIN = 1.1
FULL_BACKUP_SPARE_BYTES = 2 * 2**30  # disk space left free after the full backup

_job = {"running": False}
_lock = threading.Lock()
_cli = {"language": None}  # set when run from the command line, where there's no Telegram listener


def _telegram():
    import telegram_listener  # imported late: it imports ai_assistant, which imports this module
    return telegram_listener


def _lang():
    return _cli["language"] or _telegram().LANGUAGE


class ToolError(Exception):
    pass


def _roots():
    raw = os.environ.get("UMAY_SLEEP_ROOTS", "").strip()
    if raw:
        dirs = [Path(os.path.expandvars(os.path.expanduser(p.strip()))) for p in raw.split(";") if p.strip()]
    else:
        dirs = [Path.home() / "Documents" / "Projects", Path.home() / "Documents" / "Work"]
    return [d.resolve() for d in dirs if d.is_dir()]


def _failsafe_dir():
    raw = os.environ.get("UMAY_FAILSAFE_DIR", "").strip()
    return Path(os.path.expandvars(raw)) if raw else Path.home() / "Documents" / "Failsafe"


def _comm_script():
    return Path(os.environ.get("CLAUDE_COMM_PATH", "").strip() or COMM_SCRIPT).resolve()


def _forbidden():
    comm = None if _cli["language"] else _telegram()._link.get("comm")
    folder = Path(comm.__file__).resolve().parent if comm else _comm_script().parent
    return [folder]


def _hook_script():
    return paths.resource_dir() / "sleep_hook.ps1"


# --- folders and backups ---------------------------------------------------------------------

def _project_of(path):
    """(root, top-level folder) for a path inside one of the roots, or None."""
    for root in _roots():
        if path != root and path.is_relative_to(root):
            return root, root / path.relative_to(root).parts[0]
    return None


def _check_folder(folder):
    p = Path(os.path.expandvars(os.path.expanduser(folder.strip().strip('"'))))
    if not p.is_absolute():
        raise ToolError(f"Use an absolute folder path inside: {', '.join(map(str, _roots()))}")
    p = p.resolve()
    if _project_of(p) is None:
        raise ToolError(f"Sleep mode only works inside: {', '.join(map(str, _roots()))}. Not: {p}")
    for bad in _forbidden():
        if p == bad or p.is_relative_to(bad):
            raise ToolError(f"{p} is off-limits in sleep mode (Telegram bridge and its secrets).")
    return p


def _size(folder):
    total = 0
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return total


def _backed_up(job_dir):
    record = job_dir / "backed_up.txt"
    if not record.exists():
        return []
    return [line.split("|") for line in record.read_text(encoding="utf-8-sig").splitlines() if "|" in line]


def _backup(project, root, job_dir):
    """Copies a project folder (or a loose file in a root) into the job's Failsafe folder once, like
    sleep_hook.ps1 does. A copy only counts once it's in backed_up.txt: a half-made one (e.g. the
    process was stopped) is made again."""
    target = job_dir / f"{root.name}_{project.name}"
    if not project.exists() or str(project) in {source for source, _ in _backed_up(job_dir)}:
        return
    shutil.rmtree(target, ignore_errors=True)
    if project.is_file():
        target.mkdir(parents=True)
        shutil.copy2(project, target)
    else:
        result = subprocess.run(
            ["robocopy", str(project), str(target), "/E", "/XD", *SKIP_DIRS, "/R:1", "/W:1",
             "/NFL", "/NDL", "/NJH", "/NJS", "/NP"],
            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode >= 8:
            shutil.rmtree(target, ignore_errors=True)
            raise ToolError(f"Backing up {project} failed (robocopy exit code {result.returncode}).")
    with open(job_dir / "backed_up.txt", "a", encoding="utf-8") as f:
        f.write(f"{project}|{target}\n")


def _all_projects():
    """(root, path) of every folder and loose file in the roots, except the off-limits ones."""
    found = []
    for root in _roots():
        for path in sorted(root.iterdir()):
            if not any(path == bad or path.is_relative_to(bad) for bad in _forbidden()):
                found.append((root, path))
    return found


def _projects_size():
    return sum(_size(p) if p.is_dir() else p.stat().st_size for _, p in _all_projects())


def _backup_all(folders, job_dir):
    """The owner's choice: before anything starts, every project in the roots is copied, so nothing can
    change tonight without its "before" copy, however it's changed (sleep_hook.ps1 only sees paths a
    tool call names). Copies of projects that didn't change are deleted again by _report.

    If the disk can't hold it all, only the job's folders are copied and the hook copies the others
    before their first change, as before. Returns a note for the report ("" if all went well)."""
    job_dir.mkdir(parents=True, exist_ok=True)
    need, free = _projects_size(), shutil.disk_usage(job_dir).free
    if need * FULL_BACKUP_MARGIN + FULL_BACKUP_SPARE_BYTES > free:
        for folder in folders:
            root, project = _project_of(folder)
            _backup(project, root, job_dir)
        return i18n.get(_lang(), "sleep_full_backup_no_space", need=f"{need / 2**30:.1f}",
                        free=f"{free / 2**30:.1f}")
    failed = []
    for root, project in _all_projects():
        try:
            _backup(project, root, job_dir)
        except (ToolError, OSError):
            failed.append(str(project))  # the hook tries again before its first change, and blocks if it fails
    for folder in folders:
        if str(_project_of(folder)[1]) in failed:
            raise ToolError(f"Backing up the job's folder {folder} failed; nothing was changed.")
    return i18n.get(_lang(), "sleep_full_backup_failed", folders=", ".join(failed)) if failed else ""


def _files(folder):
    found = {}
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            full = Path(dirpath) / name
            found[full.relative_to(folder).as_posix()] = full
    return found


def _same(a, b):
    try:
        sa, sb = a.stat(), b.stat()
        if sa.st_size != sb.st_size:
            return False
        if int(sa.st_mtime) == int(sb.st_mtime):
            return True
        with open(a, "rb") as fa, open(b, "rb") as fb:
            while True:
                ca, cb = fa.read(1 << 20), fb.read(1 << 20)
                if ca != cb:
                    return False
                if not ca:
                    return True
    except OSError:
        return False


def _changes(source, backup):
    """(added, changed, deleted) relative paths between the backup and the folder now."""
    if source.is_file():  # a single file right inside a root was backed up
        return [], ([] if _same(backup / source.name, source) else [source.name]), []
    before = _files(backup)
    after = _files(source) if source.exists() else {}
    added = sorted(set(after) - set(before))
    deleted = sorted(set(before) - set(after))
    changed = sorted(p for p in set(before) & set(after) if not _same(before[p], after[p]))
    return added, changed, deleted


# --- Claude Code -----------------------------------------------------------------------------

def _rules(job_dir):
    language = LANGUAGE_NAMES.get(_lang(), "English")
    roots = ", ".join(str(r) for r in _roots())
    return f"""You are working alone overnight in Umay's sleep mode; the owner is asleep and can't answer.
- Make your own decisions; never wait for permission or ask questions. Work only inside: {roots}.
- The owner's idea may not work as described. A failed first attempt is part of the job: investigate why,
  find your own solution, try again, and keep going until there is a working, verified result.
- Every project folder you change, including ones outside the job's folder, is copied automatically to
  {job_dir} right before your first change in it. This happens silently; don't do it yourself.
  Never touch .env files or {', '.join(str(f) for f in _forbidden())}.
- Install missing Python packages from the project's requirements.txt into its virtualenv if needed.
- Long jobs (e.g. training): start them in the background with their output going to a log file, then
  keep checking the log until they finish; don't end your turn while they still run.
- The owner's instructions about reaching them on Telegram / claude_comm don't apply in sleep mode:
  never run claude_comm.py, never send messages or wait for answers. Umay sends your report.
- When the job is completely done and verified, end your final message with a report in {language}:
  what you did, which files you changed, every problem you hit and why you chose each solution
  (so the owner learns from it), and how to run or check the result. Then put {DONE_MARKER} on the
  very last line. Only write {DONE_MARKER} when the job is really finished."""


def _settings(job_dir):
    # A copy next to the backups: the bundled one lives in the exe's temp folder, which is gone if
    # Umay restarts while Claude Code is still working.
    script = job_dir / "sleep_hook.ps1"
    shutil.copyfile(_hook_script(), script)
    hook = (f'powershell -NoProfile -ExecutionPolicy Bypass -File "{script}" '
            f'-Failsafe "{job_dir}" -Roots "{";".join(map(str, _roots()))}" '
            f'-Forbidden "{";".join(map(str, _forbidden()))}"')
    # The timeout is for big folders: a hook that runs out of time lets the change through unbacked.
    settings = {
        "hooks": {"PreToolUse": [{"matcher": HOOK_TOOLS,
                                  "hooks": [{"type": "command", "command": hook, "timeout": 1800}]}]},
        "permissions": {"deny": ["Read(**/.env)", "Edit(**/.env)", "Read(**/.env.*)", "Edit(**/.env.*)"]},
    }
    path = job_dir / "claude_settings.json"
    path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return path


def _claude(exe, prompt, cwd, job_dir, settings, session_id):
    cmd = [exe, "-p", prompt, "--output-format", "json", "--permission-mode", "auto",
           "--settings", str(settings), "--append-system-prompt", _rules(job_dir),
           "--add-dir", *map(str, _roots())]
    if session_id:
        cmd += ["--resume", session_id]
    proc = subprocess.run(cmd, cwd=cwd, env=claude_bridge.clean_env(), capture_output=True, stdin=subprocess.DEVNULL,
                          timeout=ROUND_TIMEOUT_SECONDS, creationflags=subprocess.CREATE_NO_WINDOW)
    stdout = proc.stdout.decode("utf-8", errors="replace").strip()
    try:
        return json.loads(stdout.splitlines()[-1])
    except (ValueError, IndexError):
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(stderr or stdout or f"exit code {proc.returncode}")


def _work(task, cwd, job_dir, log):
    """Runs Claude Code until it says the job is done. Returns (done, final text)."""
    exe = claude_bridge._claude_exe()
    if not exe:
        return False, "Claude Code (claude.exe) not found."
    settings = _settings(job_dir)
    session_id, text = None, ""
    prompt = task
    for round_number in range(1, MAX_ROUNDS + 1):
        try:
            data = _claude(exe, prompt, cwd, job_dir, settings, session_id)
        except subprocess.TimeoutExpired:
            text = f"Round {round_number} ran longer than {ROUND_TIMEOUT_SECONDS // 3600} hours and was stopped."
            log(text)
            prompt = "You were stopped for taking too long. Continue the job from where you are and finish it."
            continue
        except (OSError, RuntimeError) as e:
            text = f"Claude Code failed in round {round_number}: {e}"
            log(text)
            if not session_id:
                return False, text
            prompt = "Something went wrong. Continue the job from where you are and finish it."
            continue
        session_id = data.get("session_id") or session_id
        previous, text = text, (data.get("result") or "").strip()
        log(f"Round {round_number}: {text[-300:]}")
        if DONE_MARKER in text:
            return True, text.replace(DONE_MARKER, "").strip()
        if data.get("is_error") and text == previous:
            return False, f"Claude Code keeps failing with the same error, so I stopped: {text}"
        prompt = (f"You stopped, but the job isn't finished ({DONE_MARKER} is missing). Don't give up: if "
                  "something failed, investigate why, try another way, and keep going until it works. Then "
                  f"write the report and end with {DONE_MARKER}.")
    return False, text


# --- the job ---------------------------------------------------------------------------------

def _report(task, folders, job_dir, started, done, claude_text, backup_note=""):
    """Writes the report into every changed folder and the job folder. Returns its text."""
    lines = [i18n.get(_lang(), "sleep_report_title"), "",
             i18n.get(_lang(), "sleep_report_task", task=task),
             i18n.get(_lang(), "sleep_report_time", start=f"{started:%Y-%m-%d %H:%M}",
                      end=f"{datetime.now():%H:%M}"),
             i18n.get(_lang(), "sleep_report_done" if done else "sleep_report_not_done"), ""]
    if backup_note:
        lines += [backup_note, ""]
    changed_folders = []
    lines.append(i18n.get(_lang(), "sleep_report_changes"))
    for source, backup in _backed_up(job_dir):
        source, backup = Path(source), Path(backup)
        added, changed, deleted = _changes(source, backup)
        if not (added or changed or deleted):
            shutil.rmtree(backup, ignore_errors=True)  # nothing changed: no need to keep the copy
            continue
        changed_folders.append(source)
        lines.append(f"\n### {source}")
        lines.append(i18n.get(_lang(), "sleep_report_backup", backup=backup))
        for key, items in (("sleep_report_added", added), ("sleep_report_changed", changed),
                           ("sleep_report_deleted", deleted)):
            if items:
                shown = items[:REPORT_LIST_LIMIT]
                more = f" (+{len(items) - len(shown)})" if len(items) > len(shown) else ""
                lines.append(i18n.get(_lang(), key, count=len(items)) + more)
                lines.extend(f"- {item}" for item in shown)
    if not changed_folders:
        lines.append(i18n.get(_lang(), "sleep_report_no_changes"))
    lines += ["", i18n.get(_lang(), "sleep_report_claude"), "", claude_text or "-"]
    text = "\n".join(lines)

    name = f"SLEEP_REPORT_{started:%Y-%m-%d_%H%M}.md"
    for folder in changed_folders or [f for f in folders if f.exists()]:
        try:
            (folder / name).write_text(text, encoding="utf-8")
        except OSError:
            pass
    (job_dir / name).write_text(text, encoding="utf-8")
    return text


def _say(text):
    """The owner is asleep: everything goes to Telegram (printed if Telegram isn't set up)."""
    if _telegram()._link:
        _telegram().send_alert(text)
    else:
        print(text)


def _run_job(task, folders, job_dir):
    started = datetime.now()
    import health_monitor  # late, like _telegram(): it imports telegram_listener
    health_monitor.ensure_running()  # the owner's rule: the PC's health is watched while working alone
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)  # no sleep meanwhile
    log_path = job_dir / "log.txt"

    def log(message):
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%H:%M:%S}] {message}\n")

    try:
        done, claude_text, backup_note = False, "", ""
        try:
            backup_note = _backup_all(folders, job_dir)
            log(f"Backed up. {backup_note}".strip())
            cwd = folders[0]
            cwd.mkdir(parents=True, exist_ok=True)
            done, claude_text = _work(task, cwd, job_dir, log)
        except Exception as e:
            claude_text = f"Sleep mode stopped: {e}"
            log(claude_text)
        report = _report(task, folders, job_dir, started, done, claude_text, backup_note)
        _say(report)
    finally:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        with _lock:
            _job["running"] = False
    _shut_down()


def _shut_down():
    """Plans the shutdown through system_tools (60 s warning, /iptal cancels), without asking."""
    notify = lambda key, **kwargs: _say(i18n.render(_lang(), key, **kwargs))
    with pc_tools.channel(lambda keys, **kwargs: True, notify):
        system_tools.schedule_shutdown("shutdown", 0)


def start_sleep_mode(task: str, folders: list) -> str:
    def run():
        if not task.strip():
            raise ToolError("Tell me the job first.")
        checked = []
        for folder in folders or []:
            p = _check_folder(folder)
            if p not in checked:
                checked.append(p)
        if not checked:
            raise ToolError("Which project folder is the job in? Give at least one folder inside "
                            + ", ".join(map(str, _roots())))
        with _lock:
            if _job["running"]:
                raise ToolError("A sleep-mode job is already running.")
        size = _projects_size() / 2**20  # everything in the roots is backed up (_backup_all)
        slug = re.sub(r"[^\w-]+", "_", checked[0].name)[:40]
        job_dir = _failsafe_dir() / f"{datetime.now():%Y-%m-%d_%H%M}_{slug}"
        if not pc_tools.confirm("confirm_sleep_mode", task=task, folders="\n".join(f"• {p}" for p in checked),
                                size=f"{size:.0f}", failsafe=job_dir):
            return pc_tools._declined()
        with _lock:
            if _job["running"]:
                raise ToolError("A sleep-mode job is already running.")
            _job["running"] = True
        job_dir.mkdir(parents=True, exist_ok=True)
        threading.Thread(target=_run_job, args=(task, checked, job_dir), daemon=True).start()
        return (f"Sleep mode started. Backups go to {job_dir}. The report will come on Telegram, then the PC "
                "shuts down (60 s warning, /iptal cancels). Good night.")
    try:
        return run()
    except (ToolError, OSError) as e:
        return str(e)


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
        "start_sleep_mode",
        "Sleep mode: when the user says to finish a job while they sleep. Backs up the job's project folders "
        "to Documents\\Failsafe, then Claude Code does the job alone with no further questions (only inside "
        "Documents\\Projects and Documents\\Work, backing up any other project folder before changing it), "
        "sends a detailed report on Telegram, saves it in the changed folders and shuts the PC down. "
        "Pass the user's job in full, in their words. The user confirms once before it starts.",
        {
            "task": {"type": "string", "description": "The job, in full detail as the user described it."},
            "folders": {"type": "array", "items": {"type": "string"},
                        "description": "Absolute path(s) of the project folder(s) the job is in; the first is "
                                       "where Claude Code starts."},
        },
        ["task", "folders"],
    ),
]

TOOL_FUNCTIONS = {"start_sleep_mode": start_sleep_mode}


# --- command line: sleep mode inside an interactive Claude Code session ------------------------
#
#   python sleep_mode.py start --task "..." --folder <project folder> [--folder ...]
#   python sleep_mode.py finish --summary-file <file or -> [--not-done]
#   python sleep_mode.py status
#
# `start` writes FLAG_FILE; while it exists (and is less than a day old) the global hook
# ~/.claude/hooks/umay_sleep_gate.py hands every file-changing tool call to the night's copy of
# sleep_hook.ps1. Until the full backup is done ("ready": false) the hook lets nothing through but
# this file. `finish` removes the flag, so the next session works normally again.

FLAG_MAX_AGE_SECONDS = 24 * 60 * 60  # an older flag is from a session that never finished


def _flag():
    try:
        if time.time() - FLAG_FILE.stat().st_mtime > FLAG_MAX_AGE_SECONDS:
            return None
        return json.loads(FLAG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_flag(state):
    FLAG_FILE.parent.mkdir(parents=True, exist_ok=True)
    FLAG_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def cli_start(task, folders):
    if not task.strip():
        raise ToolError("Tell me the job first.")
    checked = []
    for folder in folders:
        p = _check_folder(folder)
        if p not in checked:
            checked.append(p)
    state = _flag()
    fresh = state is None
    if state:  # already on tonight (or an interrupted start): keep the same Failsafe folder
        job_dir = Path(state["failsafe"])
        state["folders"] += [str(p) for p in checked if str(p) not in state["folders"]]
    else:
        slug = re.sub(r"[^\w-]+", "_", checked[0].name)[:40]
        job_dir = _failsafe_dir() / f"{datetime.now():%Y-%m-%d_%H%M}_{slug}"
        job_dir.mkdir(parents=True, exist_ok=True)
        hook = job_dir / "sleep_hook.ps1"  # a copy, like _settings() makes
        shutil.copyfile(_hook_script(), hook)
        state = {"task": task, "folders": [str(p) for p in checked], "failsafe": str(job_dir),
                 "hook": str(hook), "roots": [str(r) for r in _roots()],
                 "forbidden": [str(f) for f in _forbidden()], "started": datetime.now().isoformat(timespec="seconds"),
                 "backup_note": ""}
    was_ready, state["ready"] = state.get("ready", False), False
    _write_flag(state)  # from here nothing may change until the backup is done
    try:
        note = _backup_all([Path(f) for f in state["folders"]], job_dir)
    except BaseException:
        if fresh:
            FLAG_FILE.unlink(missing_ok=True)
        else:
            state["ready"] = was_ready
            _write_flag(state)
        raise
    state["backup_note"], state["ready"] = note or state["backup_note"], True
    _write_flag(state)
    return (f"Sleep mode is on. Failsafe folder: {job_dir}\n"
            + (f"BACKUP WARNING: {note}\n" if note else "Every project in the roots is backed up.\n")
            + "Files outside the roots are backed up before Edit/Write changes them.")


def _shutdown_logged(since):
    """True once Windows has logged (event 1074) a shutdown planned after `since` (UTC)."""
    stamp = since.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    query = f"*[System[(EventID=1074) and TimeCreated[@SystemTime>='{stamp}']]]"
    for _ in range(10):
        result = subprocess.run(["wevtutil", "qe", "System", f"/q:{query}", "/c:1", "/f:text"],
                                capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        if "1074" in result.stdout:
            return True
        time.sleep(1)
    return False


def _cli_shut_down(seconds=CLI_SHUTDOWN_SECONDS):
    """Plans a forced shutdown (the owner's choice) and checks Windows really has it. Returns (ok, error)."""
    since = datetime.now(timezone.utc).replace(microsecond=0)
    comment = i18n.get(_lang(), "sleep_cli_shutdown_comment")
    result = subprocess.run(["shutdown", "/s", "/f", "/t", str(seconds), "/c", comment],
                            capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout).strip() or f"shutdown exit code {result.returncode}"
    if not _shutdown_logged(since):
        return False, "shutdown.exe accepted it, but Windows didn't log a planned shutdown (event 1074)"
    return True, ""


def _cli_telegram(subject, body, attachment):
    try:
        result = subprocess.run([sys.executable, str(_comm_script()), "send", "--subject", subject,
                                 "--attach", str(attachment), "--body-file", "-"],
                                input=body.encode("utf-8"), capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        return str(e)
    return None if result.returncode == 0 else result.stderr.decode("utf-8", errors="replace").strip()


def cli_finish(summary, done):
    """Report, Telegram, shutdown. Returns (shut down planned, text for Claude Code)."""
    state = _flag() or (json.loads(FLAG_FILE.read_text(encoding="utf-8")) if FLAG_FILE.exists() else None)
    if not state:
        raise ToolError("Sleep mode isn't on: run `start` first.")
    job_dir, started = Path(state["failsafe"]), datetime.fromisoformat(state["started"])
    report = _report(state["task"], [Path(f) for f in state["folders"]], job_dir, started, done, summary,
                     state.get("backup_note", ""))
    report_file = job_dir / f"SLEEP_REPORT_{started:%Y-%m-%d_%H%M}.md"
    FLAG_FILE.unlink(missing_ok=True)  # before the shutdown: the next session must work normally
    shut_down, error = _cli_shut_down()
    status = i18n.get(_lang(), "sleep_cli_shutdown_ok" if shut_down else "sleep_cli_shutdown_failed", error=error)
    body = report if len(report) <= 3000 else report[:3000] + "…\n\n" + i18n.get(_lang(), "sleep_cli_report_cut")
    telegram_error = _cli_telegram(i18n.get(_lang(), "sleep_report_title").lstrip("# "), f"{body}\n\n{status}",
                                   report_file)
    lines = [f"Report: {report_file}",
             "Shutdown: planned and confirmed by Windows (60 s)." if shut_down else f"Shutdown FAILED: {error}",
             "Telegram: sent." if telegram_error is None else f"Telegram FAILED: {telegram_error}"]
    return shut_down, "\n".join(lines)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Umay's sleep mode for an interactive Claude Code session.")
    parser.add_argument("--lang", default="en", choices=sorted(LANGUAGE_NAMES))
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", help="turn sleep mode on and back up the job's folders")
    start.add_argument("--task", required=True, help="the job, in the owner's words")
    start.add_argument("--folder", action="append", required=True, help="project folder of the job (repeatable)")
    finish = commands.add_parser("finish", help="write the report, send it on Telegram, shut the PC down")
    finish.add_argument("--summary-file", required=True,
                        help="your account: what you did, problems and why you solved them so ('-' = stdin)")
    finish.add_argument("--not-done", action="store_true", help="the job isn't finished")
    commands.add_parser("status", help="is sleep mode on?")
    args = parser.parse_args()
    _cli["language"] = args.lang
    try:
        if args.command == "start":
            print(cli_start(args.task, args.folder))
        elif args.command == "finish":
            raw = sys.stdin.buffer.read() if args.summary_file == "-" else Path(args.summary_file).read_bytes()
            shut_down, text = cli_finish(raw.decode("utf-8-sig").strip(), not args.not_done)
            print(text)
            sys.exit(0 if shut_down else 1)
        else:
            state = _flag()
            print(json.dumps(state, indent=2, ensure_ascii=False) if state else "Sleep mode is off.")
    except (ToolError, OSError) as e:
        print(f"ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
