"""Coding mode, Umay's side: "Activate coding mode" (spoken or written) or Ctrl+F7 opens a window
(coding_window.py, its own Qt process) where the owner gives the local coding model (coding_server.py: TwIL-8B Heretic) a
coding task in a folder; it works on it (coding_agent.py, in coding_workspace.py's sandbox) and the owner keeps the
result (tick) or undoes it (cross). Opening the window starts the model's server; closing it (or 10 idle minutes) stops it.

- State lives here, the window is only a view: every change is sent to it as one snapshot ({"state": {...}}), and
  what it sends back are events (task, folder, keep, revert, stop, voice, closed). Closing the window does not stop a
  task; opening it again shows where it is.
- The folder: the window's pick, else the VS Code window's folder (VS Code's storage.json), else the last one used.
  Umay's own folders are closed (coding_workspace.why_closed); the window then says so and waits for another pick.
- One task at a time, and none until the last one is ticked or crossed. The journal is on the disk
  (coding_workspace.Journal): a task pending at a restart is pending when the window opens again.
- Voice (the window's button, off whenever the window opens): while it is on, what the owner says in a spoken
  conversation (voice_turn, the voice session's answer callable) is a task for the coding model and never reaches the normal
  Umay; off, voice is the normal Umay and the window is for typing. Telegram, the text box and calls never go to it.
  The voice session stays open meanwhile (voice_session.stay_open) so a long task does not end the conversation.
  A Whisper made-up "Thank you." or a one- or two-word scrap is not a task (looks_like_noise).
- The coding model's own words are never the report: the window shows what the journal says changed, with a diff.
"""
import json
import os
import re
import subprocess
import sys
import threading
import urllib.parse
from pathlib import Path

import coding_agent
import coding_server
import coding_workspace as cw
import local_llm
import paths

ASK_EN = re.compile(r"\b(?:activate|start|open|enable|turn on|launch)\s+(?:the\s+)?coding\s+mode\b", re.I)
ASK_TR = re.compile(r"\bkodlama\s+mod\w*\s*(?:u\s*)?(?:aç|ac|başlat|baslat|aktif)|\bcoding\s+mod\w*\s*(?:aç|ac|başlat)", re.I)
NOISE_WORDS = {"thank you", "thanks", "thanks for watching", "you", "bye", "okay", "ok", "yes", "no", "hmm", "uh", "um"}
MIN_WORDS = 3
LOG_LINES = 300

_s = {"proc": None, "folder": None, "voice_on": False, "busy": False, "log": [], "notice": "", "task": "",
      "stop": False, "closing": False, "via_voice": False}
_lock = threading.Lock()


def asked(text):
    """"activate coding mode" (the spoken hint at the end of a voice message is not part of it)."""
    import voice
    text = (text or "").split(voice.SPOKEN_HINT.strip()[:20])[0]
    return bool(ASK_EN.search(text) or ASK_TR.search(text))


def journal():
    return cw.Journal()


# --- the folder ----------------------------------------------------------------------------------------------

def _last_file():
    return cw.home() / "last_folder.json"


def vscode_folder():
    """The folder VS Code's last active window has open (its storage.json), or None."""
    path = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "Code" / "User" / "globalStorage" / "storage.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        uri = state["windowsState"]["lastActiveWindow"]["folder"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not uri.startswith("file:///"):
        return None
    folder = Path(urllib.parse.unquote(uri[len("file:///"):]))
    return folder if folder.is_dir() else None


def remembered_folder():
    try:
        folder = Path(json.loads(_last_file().read_text(encoding="utf-8"))["folder"])
        return folder if folder.is_dir() else None
    except (OSError, ValueError, KeyError):
        return None


def choose_folder(folder):
    """The owner's pick (or the default). Returns "" when accepted, else the sentence saying why not."""
    problem = cw.why_closed(folder)
    if problem:
        return problem
    with _lock:
        if _s["busy"] or journal().exists():
            return "Not while a task is running or waiting for your tick or cross."
        _s["folder"] = str(folder)
    try:
        cw.home().mkdir(parents=True, exist_ok=True)
        _last_file().write_text(json.dumps({"folder": str(folder)}), encoding="utf-8")
    except OSError:
        pass
    return ""


def default_folder():
    """VS Code's folder when it can be the workspace; else the last one; else None. Says why in the notice."""
    pending = journal().load()
    if pending:
        return Path(pending["folder"]), ""
    code = vscode_folder()
    if code is not None:
        problem = cw.why_closed(code)
        if not problem:
            return code, ""
        notice = f"VS Code has {code.name} open. {problem} Pick another folder."
    else:
        notice = "Pick a folder to work in."
    last = remembered_folder()
    if last is not None and not cw.why_closed(last):
        return last, notice
    return None, notice


# --- the window ----------------------------------------------------------------------------------------------

def _command():
    if getattr(sys, "frozen", False):
        return [sys.executable, "--coding", str(os.getpid())]
    return [sys.executable, "-X", "utf8", str(paths.resource_dir() / "coding_window.py"), str(os.getpid())]


def window_open():
    proc = _s["proc"]
    return proc is not None and proc.poll() is None


def _send(obj):
    proc = _s["proc"]
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()
    except (OSError, ValueError):
        pass


def snapshot():
    j = journal()
    data = j.load()
    if data and data["status"] == "working" and not _s["busy"]:   # Umay ended mid-task: what was written is pending
        j.set_status("pending", "The task was interrupted (Umay ended before it finished).")
        data = j.load()
    pending = bool(data) and data["status"] == "pending"
    return {"folder": _s["folder"] or "", "busy": _s["busy"], "pending": pending, "voice_on": _s["voice_on"],
            "log": _s["log"][-LOG_LINES:], "notice": _s["notice"], "task": (data or {}).get("task", _s["task"]),
            "summary": (data or {}).get("summary", "") if pending else "",
            "changes": [list(c) for c in j.changes()] if pending else [],
            "diff": j.diff_text() if pending else "", "llama": _llama_ok(), "loading": coding_server.warming()}


def _llama_ok():
    """The coding model (UMAY_CODING_MODEL) and its llama-server.exe are there."""
    return coding_server.available()


def publish():
    _send({"state": snapshot()})


def _log(line):
    _s["log"].append(line)
    del _s["log"][:-LOG_LINES * 2]
    _send({"log": line})


def open_window(via="other"):
    """Opens the window (or brings the open one forward). Returns the sentence Umay says. Thread-safe."""
    with _lock:
        if not window_open():
            _s["voice_on"] = False
            try:
                log = paths.voice_home().parent / "coding_window.log"
                log.parent.mkdir(parents=True, exist_ok=True)
                with open(log, "a", encoding="utf-8") as err:
                    _s["proc"] = subprocess.Popen(_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err,
                                                  text=True, encoding="utf-8", cwd=str(paths.resource_dir()),
                                                  env=paths.own_child_env(), creationflags=subprocess.CREATE_NO_WINDOW)
            except OSError as e:
                return f"The coding window could not start: {e}"
            threading.Thread(target=_read, args=(_s["proc"],), daemon=True).start()
        else:
            _send({"front": True})
    if not _s["busy"]:
        folder, notice = default_folder()
        _s["notice"] = notice
        if folder is not None and not _s["folder"]:
            _s["folder"] = str(folder)
    if not _llama_ok():
        _s["notice"] = "The coding model is not set up (UMAY_CODING_MODEL / UMAY_CODING_EXE): nothing can be written."
    else:   # start its server and read the prompt start now, so the first task does not wait for it
        coding_server.warm(prime=coding_agent.prime, done=publish)
    publish()
    waiting = " The last task is waiting for your tick or cross." if journal().exists() and not _s["busy"] else ""
    return "Coding mode is open, dear. Press the voice button when you want me to listen there." + waiting


def toggle_window():
    """Ctrl+F7: opens the window, or closes it when it is open."""
    if window_open():
        close_window()
    else:
        open_window()


def close_window():
    _s["voice_on"] = False
    proc = _s["proc"]
    if proc is not None and proc.poll() is None:
        _send({"close": True})
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
    _s["proc"] = None
    _release()


def _release():
    """The window is gone: the coding model's server goes with it unless a task is still running (it then ends by its
    idle time after the task)."""
    if not _s["busy"]:
        coding_server.stop()


def _read(proc):
    for line in proc.stdout:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        try:
            handle(event)
        except Exception as e:  # a bad event never takes the reader down
            print(f"Coding window event {event.get('event')} failed: {type(e).__name__}: {e}")
    proc.stdout.close()
    proc.wait()
    if _s["proc"] is proc:
        _s["proc"] = None
        _s["voice_on"] = False
        _release()


def handle(event):
    name = event.get("event")
    if name == "ready":
        publish()
    elif name == "task":
        problem = submit(str(event.get("text") or ""))
        if problem:
            _s["notice"] = problem
            publish()
    elif name == "folder":
        _s["notice"] = choose_folder(event.get("folder") or "")
        publish()
    elif name == "keep":
        keep()
    elif name == "revert":
        revert()
    elif name == "stop":
        _s["stop"] = True
    elif name == "voice":
        _s["voice_on"] = bool(event.get("on"))
        publish()


# --- tasks ---------------------------------------------------------------------------------------------------

def submit(text, voice=False):
    """Starts a task. Returns "" when it started, else the sentence saying why not."""
    text = (text or "").strip()
    if not text:
        return "Type the task first."
    with _lock:
        if _s["busy"]:
            return "I am still working on the last task."
        if journal().exists():
            return "The last task is waiting for your tick or cross."
        if not _s["folder"]:
            return "Pick a folder first."
        if not _llama_ok():
            return "The coding model is not set up (UMAY_CODING_MODEL / UMAY_CODING_EXE)."
        problem = cw.why_closed(_s["folder"])
        if problem:
            return problem
        _s.update(busy=True, stop=False, task=text, notice="", log=[], via_voice=voice)
    publish()
    threading.Thread(target=_run, args=(text,), daemon=True).start()
    return ""


def _run(task):
    j = journal()
    summary = ""
    try:
        ws = cw.Workspace(_s["folder"], j)
        j.begin(ws.root, task)
        _log(f"Task: {task}")
        agent = coding_agent.Agent(ws, task, on_step=_log, stopped=lambda: _s["stop"])
        with local_llm.working(background=True):   # the whole task, not each request: no "rest" between two rounds; not a reply, so
            # it doesn't stop its own server (local_llm.working(background=False) makes an idle coding server give way)
            summary = agent.run()
        data = j.load() or {"files": {}}
        if not data["files"]:
            j.keep()                       # nothing was written: nothing to decide
            _s["notice"] = summary or "Nothing was changed."
        else:
            j.set_status("pending", summary)
    except cw.WorkspaceError as e:
        _s["notice"] = str(e)
        if j.exists() and not (j.load() or {}).get("files"):
            j.keep()
    except Exception as e:  # noqa: BLE001 - whatever was written stays in the journal
        summary = f"The task broke off: {type(e).__name__}: {e}"
        _s["notice"] = summary
        if j.exists():
            j.set_status("pending", summary)
    finally:
        _s["busy"] = False
        if not window_open():       # the window was closed while it worked: the model's server goes now (off outside coding mode)
            coding_server.stop()
        publish()
        _tell_by_voice(summary)


def _tell_by_voice(summary):
    if not _s["via_voice"] or not _s["voice_on"]:
        return
    changed = journal().changes() if journal().exists() else []
    text = (f"Done, dear: {len(changed)} file{'s' if len(changed) != 1 else ''} changed. Tick or cross in the window."
            if changed else "Done, dear, but I changed nothing. " + (summary or ""))
    import voice_session
    session = voice_session._session["it"]
    try:
        if session is not None and session.active():
            session.say(text)
        else:
            voice_session.open_with(text)
    except Exception as e:  # noqa: BLE001 - the window shows it anyway
        print(f"Coding mode: couldn't say it aloud: {e!r}")


def keep():
    """The tick."""
    if _s["busy"]:
        return
    journal().keep()
    _s["notice"] = "Kept."
    _s["log"] = []
    publish()


def revert():
    """The cross."""
    if _s["busy"]:
        return
    result = journal().revert()
    note = f"Undone: {result['restored']} file(s) restored, {result['removed']} removed."
    if result["rescued"]:
        note += f" You had changed {', '.join(result['rescued'])} meanwhile; your version is in {cw.home() / 'rescued'}."
    _s["notice"] = note
    _s["log"] = []
    publish()


# --- voice ---------------------------------------------------------------------------------------------------

def voice_wanted():
    """The voice session should stay open and its words go here: the window is open and its voice button is on."""
    return bool(_s["voice_on"]) and window_open()


def looks_like_noise(text):
    plain = re.sub(r"[^\w\s']", "", text.lower()).strip()
    import voice
    return voice.looped(text) or plain in NOISE_WORDS or len(plain.split()) < MIN_WORDS


def voice_turn(chat, text):
    """The voice session's answer, first: None = not for coding mode (the normal Umay answers); "" = heard, nothing to
    say (noise); else the short sentence she speaks."""
    if not voice_wanted():
        return None
    import voice
    task = text.replace(voice.SPOKEN_HINT, "").strip()
    if looks_like_noise(task):
        return ""
    problem = submit(task, voice=True)
    if problem:
        _s["notice"] = problem
        publish()
        return problem
    return "On it, dear."
