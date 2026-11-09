"""Claude Code in a terminal on the PC: "Umay, open Claude Code and fix the news panel" opens a Windows Terminal
window with an interactive Claude Code session that starts on the task at once, so the owner can watch it,
answer its questions and go on talking to it. (`/c` on Telegram is the other way: headless, its answer comes
back as text, claude_bridge.py.) The owner's own Claude Code instructions make it report on Telegram when done.

- The folder: a project named in the task (a folder of Documents\\Projects or Documents\\Work whose name is in
  it: "in Umay", "Umay projesinde"), else CLAUDE_CODE_CWD (Documents\\Projects); CLAUDE_CODE_EXTRA_DIRS added
  as for /c.
- UMAY_CLAUDE_PERMISSION_MODE (auto, like sleep mode: it doesn't stop at every command; empty = Claude
  Code's own default, which asks).
- The task goes through a file (%LOCALAPPDATA%\\Umay\\claude_tasks), not the command line: Windows Terminal
  reads ";" as its own separator, and quotes break on the way through PowerShell.
- Without Windows Terminal: a plain console window.
- The terminal's first-run screens (pick a theme, trust this folder) would hold every task: prepare() marks
  them done in ~/.claude.json before each launch (a copy of the file is kept as .claude.json.umay-backup).
- asked() catches "open Claude Code and ..." before any model sees it (like "call me": the small local model
  could get it wrong); the open_claude_code tool covers other ways of saying it.
"""
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import claude_bridge
import paths

PERMISSION_MODE = "auto"
TITLE = "Claude Code · Umay"
SPOKEN_TAIL = re.compile(r"\n\n\[.*\Z", re.S)  # voice.SPOKEN_HINT / the language reminder after a message
ASK_EN = re.compile(
    r"^\W*(?:(?:hey|ok|okay)\W+)?(?:umay\W+)?(?:(?:can|could|would|will) you\s+)?(?:please\s+)?"
    r"(?:i\s+(?:need|want)\s+you\s+to\s+|i'?d\s+like\s+you\s+to\s+)?"
    r"(?:open|start|launch|run|use|ask|tell|get|have)\s+(?:the\s+)?claude(?:\s+code)?\b"
    r"(?:\s+(?:and|to|for|with|in order to)\b)?[\s,:.-]*(?P<task>.*)$", re.I | re.S)
ASK_TR = re.compile(r"\bclaude(?:\s*code)?(?:'?\s*(?:u|yu|a|ya|da|de|yla|ile|i))?\s+"
                    r"(?:aç|başlat|çalıştır)\w*(?:\s+ve)?[\s,:.-]*", re.I)
ADDRESS = re.compile(r"^\W*(?:hey\s+)?umay\s*[,!:]\s*", re.I)  # "Umay, ..."
TR_DO = re.compile(r"\bclaude(?:\s*code)?'?\s*(?:a|ya)\b", re.I)  # "Claude Code'a şunu yaptır"


def asked(text):
    """The task when the owner asks for Claude Code ("" for just opening it), else None."""
    text = SPOKEN_TAIL.sub("", text or "").strip()
    m = ASK_EN.match(text)
    if m:
        return m.group("task").strip()
    m = ASK_TR.search(text)
    if m:
        return _tidy(text[:m.start()] + " " + text[m.end():])
    if TR_DO.search(text) and re.search(r"\b(yaptır|söyle|ver)\w*", text, re.I):
        return _tidy(TR_DO.sub("", text))
    return None


def _tidy(text):
    return re.sub(r"\s+", " ", ADDRESS.sub("", text)).strip(" ,.:-")


def _roots():
    roots = [claude_bridge._work_dir()] + [Path(d) for d in claude_bridge._extra_dirs()]
    return [r for r in roots if r.is_dir()]


def project_for(task, roots=None):
    """The project folder named in the task (the longest name found as a word), else None."""
    task = ADDRESS.sub("", task or "").lower()  # "Umay, ..." is her name, not the project
    best = None
    for root in roots if roots is not None else _roots():
        try:
            folders = [p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")]
        except OSError:
            continue
        for folder in folders:
            names = {folder.name.lower(), folder.name.lower().replace("_", " ").replace("-", " ")}
            if any(re.search(r"(?<![\w])" + re.escape(n) + r"(?![a-z0-9_])", task) for n in names if len(n) >= 3):
                if best is None or len(folder.name) > len(best.name):
                    best = folder
    return best


def command(exe, task_file, folder, permission_mode, extra_dirs, wt=None):
    """The command that opens the window: PowerShell runs claude with the task read from its file, and stays
    open after it ends."""
    def q(s):
        return "'" + str(s).replace("'", "''") + "'"
    args = [q(exe)]
    # The task first: after "--add-dir <dirs...>" (it takes any number) it was read as one more folder, and
    # Claude Code opened with an empty prompt (2026-11-04).
    if task_file is not None:
        args.append(f"(Get-Content -Raw -Encoding UTF8 {q(task_file)})")
    if permission_mode:
        args += ["--permission-mode", permission_mode]
    if extra_dirs:
        args += ["--add-dir"] + [q(d) for d in extra_dirs]
    script = "& " + " ".join(args)
    shell = ["powershell", "-NoLogo", "-NoExit", "-ExecutionPolicy", "Bypass", "-Command", script]
    if wt:
        return [wt, "-w", "new", "--title", TITLE, "-d", str(folder)] + shell
    return shell


def _config_keys(folder):
    """How Claude Code names a folder in ~/.claude.json's "projects": the CLI "C:/Users/.../Umay" (its path with
    "/", the drive as given), the VS Code extension "c:/Users/.../Umay". Both, so either finds it trusted."""
    text = str(folder).replace("\\", "/").rstrip("/")
    if len(text) > 1 and text[1] == ":":
        return [text[0].upper() + text[1:], text[0].lower() + text[1:]]
    return [text]


def prepare(config, folders):
    """The terminal's first-run screens don't stop a task: onboarding done (else "Choose the text style" waits
    for a key, 2026-11-04: every task she opened sat there, nothing ran, nothing came on Telegram) and the
    folders trusted (else "Do you trust the files in this folder?"). The owner only ever used the VS Code
    extension, which needs neither. Returns True if `config` was changed."""
    changed = False
    if not config.get("hasCompletedOnboarding"):
        config["hasCompletedOnboarding"] = True
        config.setdefault("theme", "dark")  # (the CLI drops the key again when it's its default)
        changed = True
    projects = config.setdefault("projects", {})
    for folder in folders:
        for key in _config_keys(folder):
            entry = projects.setdefault(key, {})
            if not entry.get("hasTrustDialogAccepted"):
                entry["hasTrustDialogAccepted"] = True
                changed = True
    return changed


def _prepare_cli(folder):
    """prepare() on the real ~/.claude.json (a copy kept beside it the first time); never stops the launch."""
    import json

    path = Path.home() / ".claude.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
        if not prepare(config, [folder] + _roots()):
            return
        backup = path.with_name(".claude.json.umay-backup")
        if not backup.exists():
            shutil.copy2(path, backup)
        tmp = path.with_name(".claude.json.umay-tmp")
        tmp.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except (OSError, ValueError) as e:
        print(f"Claude Code: couldn't prepare ~/.claude.json: {e!r}")


def open_session(task, project=""):
    """Opens Claude Code in a terminal on `task`; returns what to tell the owner."""
    exe = claude_bridge._claude_exe()
    if not exe:
        return "I can't find Claude Code (claude.exe). Set CLAUDE_CODE_PATH in .env."
    folder = None
    if project:
        folder = Path(project) if Path(project).is_absolute() and Path(project).is_dir() else project_for(project)
    folder = folder or project_for(task) or claude_bridge._work_dir()
    _prepare_cli(folder)
    task_file = None
    if task:
        folder_dir = paths.voice_home().parent / "claude_tasks"
        folder_dir.mkdir(parents=True, exist_ok=True)
        task_file = folder_dir / f"{time.strftime('%Y-%m-%d_%H-%M-%S')}.txt"
        task_file.write_text(task.replace('"', "'"), encoding="utf-8")  # PowerShell 5 drops inner double quotes
    mode = os.environ.get("UMAY_CLAUDE_PERMISSION_MODE")
    mode = PERMISSION_MODE if mode is None else mode.strip()
    extra = [d for d in claude_bridge._extra_dirs() if Path(d) != Path(folder)]
    wt = shutil.which("wt")
    cmd = command(exe, task_file, folder, mode, extra, wt)
    try:
        subprocess.Popen(cmd, cwd=str(folder), env=claude_bridge.clean_env(),
                         creationflags=0 if wt else subprocess.CREATE_NEW_CONSOLE)
    except OSError as e:
        return f"I couldn't open Claude Code: {e}"
    where = folder.name if folder != claude_bridge._work_dir() else "your Projects folder"
    if not task:
        return f"I opened Claude Code in a terminal, in {where}. Tell it what to do there."
    return (f"I opened Claude Code in a terminal, in {where}, and gave it the task. It will message you on "
            "Telegram when it's done.")


def open_claude_code(task: str = "", project: str = "") -> str:
    """The tool."""
    return open_session(task.strip(), project.strip())


TOOLS = [{
    "type": "function",
    "function": {
        "name": "open_claude_code",
        "description": (
            "Opens Claude Code (Anthropic's coding agent) in a terminal window on this PC and gives it a task, "
            "e.g. a code change in one of the user's projects. Use whenever the user says Claude Code or Claude, "
            "for any task (a bug fix, a login problem), or to open it. Pass their task in their own words."),
        "parameters": {"type": "object", "properties": {
            "task": {"type": "string", "description": "What Claude Code should do, as the user said it."},
            "project": {"type": "string", "description": "The project's folder name if the user named one."},
        }, "required": ["task"]},
    },
}]
TOOL_FUNCTIONS = {"open_claude_code": open_claude_code}
