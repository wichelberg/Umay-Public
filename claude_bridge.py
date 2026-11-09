"""Hands bigger jobs to a headless Claude Code session (`claude -p`) and returns its answer.

Consecutive jobs resume the same Claude Code session so it remembers the
conversation; reset() starts a fresh one. It runs in acceptEdits mode: it can
read and edit files, but shell commands outside the user's allowlist are denied
since nobody is there to approve them.
"""
import glob
import json
import os
import shutil
import subprocess
from pathlib import Path

import paths

TIMEOUT_SECONDS = 30 * 60

_state = {"session_id": None}


def _claude_exe():
    configured = os.environ.get("CLAUDE_CODE_PATH", "").strip()
    if configured:
        return configured
    found = shutil.which("claude")
    if found:
        return found
    # The VS Code extension ships its own CLI; pick the most recently installed one.
    bundled = glob.glob(str(Path.home() / ".vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude.exe"))
    return max(bundled, key=os.path.getmtime) if bundled else None


# Set by a running Claude Code session for its own child processes. If Umay was started
# from one (e.g. after a rebuild), the claude it launches would think it's nested in that
# session and misbehave, so these are dropped. Umay's own CLAUDE_CODE_* settings stay.
_SESSION_VARS = {
    "CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_AGENT_SDK_VERSION", "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_SSE_PORT",
    "CLAUDE_CODE_QUESTION_PREVIEW_FORMAT", "CLAUDE_CODE_EMIT_STARTUP_TIMING",
    "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING", "CLAUDE_CODE_ENABLE_TASKS",
}


def clean_env():
    """The environment for launching claude: without another session's markers, and without the
    settings Umay read from her own .env (they're hers, not Claude Code's)."""
    return {k: v for k, v in os.environ.items()
            if k.upper() not in _SESSION_VARS and k not in paths.ENV_FILE_KEYS}


def _work_dir():
    configured = os.environ.get("CLAUDE_CODE_CWD", "").strip()
    return Path(configured) if configured else Path.home() / "Documents" / "Projects"


def _extra_dirs():
    """Folders Claude Code may use besides the work folder (default: Documents\\Work)."""
    configured = os.environ.get("CLAUDE_CODE_EXTRA_DIRS", "").strip()
    if configured:
        dirs = [Path(os.path.expandvars(os.path.expanduser(d.strip()))) for d in configured.split(";") if d.strip()]
    else:
        dirs = [Path.home() / "Documents" / "Work"]
    return [str(d) for d in dirs if d.is_dir()]


def reset():
    _state["session_id"] = None


def _run(exe, task, session_id):
    cmd = [exe, "-p", task, "--output-format", "json", "--permission-mode", "acceptEdits"]
    extra = _extra_dirs()
    if extra:
        cmd += ["--add-dir", *extra]
    if session_id:
        cmd += ["--resume", session_id]
    proc = subprocess.run(
        cmd, cwd=_work_dir(), env=clean_env(), capture_output=True, stdin=subprocess.DEVNULL,
        timeout=TIMEOUT_SECONDS, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    stdout = proc.stdout.decode("utf-8", errors="replace").strip()
    try:
        return json.loads(stdout.splitlines()[-1])
    except (ValueError, IndexError):
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(stderr or stdout or f"exit code {proc.returncode}")


def run(task):
    """Runs `task` in Claude Code and returns (ok, text)."""
    exe = _claude_exe()
    if not exe:
        return False, "Claude Code (claude.exe) not found. Set CLAUDE_CODE_PATH in .env."
    try:
        try:
            data = _run(exe, task, _state["session_id"])
        except RuntimeError:
            if not _state["session_id"]:
                raise
            reset()  # the old session may be gone; start fresh
            data = _run(exe, task, None)
    except subprocess.TimeoutExpired:
        return False, f"Claude Code didn't finish within {TIMEOUT_SECONDS // 60} minutes."
    except (OSError, RuntimeError) as e:
        return False, f"Claude Code failed: {e}"

    _state["session_id"] = data.get("session_id") or _state["session_id"]
    text = (data.get("result") or "").strip() or "(no text answer)"
    return not data.get("is_error"), text
