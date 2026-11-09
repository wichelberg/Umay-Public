"""Umay on the phone: she calls the owner on WhatsApp and talks, and answers when the owner calls her. Her own
WhatsApp account (a Turkish number of the owner's) lives in WhatsApp Beta, beside the owner's WhatsApp
(whatsapp_tools never touches it); the owner's number is UMAY_CALL_NUMBER.

- call_owner(reason): Beta opens the owner's chat (a whatsapp: link opened in Beta by its package), "Voice call"
  is pressed (UI Automation), and once the call's clock runs (it was answered) she says why she called and the
  conversation goes on like a spoken one (voice_session.Session), with call_audio's sound: she hears the call
  from the speakers' loopback and speaks into VB-CABLE, which Beta has as its microphone. It ends with the call.
  Beta not running: it is opened first (a cold start's UI Automation errors are waited out). After a call its
  window is minimized again.
- Beta runs all the time: watch() starts it minimized when Umay starts (the owner, 2026-11-04).
- The owner calling her: watch() sees Beta's incoming call window; only the owner's number is answered.
- Nothing from the camera: the owner didn't want it (2026-11-02); a voice call, and /kamera for a photo.

The wake listener doesn't listen during a call (main.py), so the owner's voice in the speakers can't start a
second conversation. Logs: chat_logs/<time>_call.txt (what was said), %LOCALAPPDATA%\\Umay\\calls.log (each step).

Pressing a button: UI Automation's Invoke, again if WhatsApp is still changing its view (2026-11-02 from
Umay.exe it failed with 0x80040201 "An event was unable to invoke any of the subscribers" right after the
chat was opened), else a mouse click on the button with the window in front.
"""
import ctypes
import os
import re
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

import call_audio
import call_devices
import voice
import voice_session
import whatsapp_tools

BETA_FAMILY = "5319275A.51895FA4EA97F_cv1g1gvanyjgm"
BETA_ID = "51895fa4ea97f"
RING_SECONDS = 90          # unanswered this long: given up
WATCH_SECONDS = 1.0
CALL_IDLE_SECONDS = 600    # in a call, silence doesn't end it: the call's end does
SOUND_LOG_TICKS = 5       # in a call, how loud both ways were: in calls.log every this many WATCH_SECONDS
CLOCK = re.compile(r"^\d{1,2}[:∶]\d{2}([:∶]\d{2})?$")  # WhatsApp writes "00∶20" (U+2236 RATIO)
BUTTON, TEXT = 50000, 50020
NAME, CONTROL_TYPE = 30005, 30003
INVOKE = 10000
PRESS_TRIES = 3

_state = {"busy": False, "session": None, "deps": None, "lock": threading.Lock(), "watching": False,
          "unanswered": set()}  # incoming call windows left ringing during a game


def _log(text):
    try:
        path = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Umay" / "calls.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}\n")
    except OSError:
        pass


def number():
    return re.sub(r"[^\d]", "", os.environ.get("UMAY_CALL_NUMBER", ""))


ASK = re.compile(r"^\W*(?:umay\W+)?(?:(?:please|pls)\s+)?(?:call me|give me a call|beni ara(?:r m[ıi]s[ıi]n)?|ara beni)\b",
                 re.IGNORECASE)


def asked(text):
    """"call me" / "beni ara" as the whole request (a short one)."""
    return len((text or "").split()) <= 6 and bool(ASK.search(text or ""))


def active():
    """A call is on (the wake listener stays quiet)."""
    return _state["busy"]


def configure(answer, new_chat, notify, activity, log_dir):
    """What a call's conversation needs, the same as a spoken one's (main.py)."""
    _state["deps"] = (answer, new_chat, notify, activity, log_dir)


# --- WhatsApp Beta through UI Automation ------------------------------------------------------------------

class _Uia:
    """UI Automation for this thread (COM objects stay on the thread that made them)."""

    def __init__(self):
        import comtypes
        import comtypes.client
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except OSError:
            pass
        # Its rectangles in physical px, like SetCursorPos below (Umay's Tk is DPI-unaware), on this thread.
        ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        self.module = comtypes.client.GetModule("UIAutomationCore.dll")
        self.uia = comtypes.client.CreateObject(self.module.CUIAutomation, interface=self.module.IUIAutomation)

    def windows(self, minimized=False):
        """Beta's visible top-level windows: [(hwnd, title)]; minimized ones too if asked (watch(): Beta waits
        minimized, and an incoming call may show inside its main window)."""
        import hud_windows
        import system_tools

        return [(w.hwnd, w.title) for w in hud_windows.top_windows()
                if w.visible and (minimized or not w.iconic)
                and BETA_ID in (system_tools._exe_path(w.pid) or "").lower()]

    def find(self, hwnd, name=None, control=BUTTON, match=None):
        """The first element of a control type in a window, by exact name or by match(name)."""
        # All of it may fail while Beta is still starting (2026-11-03 18:50: reading a name right after a cold
        # start gave 0x80040201 and the whole call was dropped): None, so _wait tries again.
        try:
            root = self.uia.ElementFromHandle(hwnd)
            found = root.FindAll(4, self.uia.CreatePropertyCondition(CONTROL_TYPE, control))
            for i in range(found.Length):
                e = found.GetElement(i)
                n = e.CurrentName or ""
                if (name is not None and n == name) or (match is not None and match(n)):
                    return e
        except Exception:
            return None
        return None

    def press(self, element, hwnd=None, name=""):
        """Invoke, PRESS_TRIES times (a fresh element each time when hwnd and name are given), else a click."""
        for attempt in range(PRESS_TRIES):
            try:
                if attempt and hwnd and name:
                    element = self.find(hwnd, name) or element
                pattern = element.GetCurrentPattern(INVOKE)
                pattern.QueryInterface(self.module.IUIAutomationInvokePattern).Invoke()
                return
            except Exception as e:
                _log(f"press {name or '?'}: invoke failed ({e}), try {attempt + 1}")
                time.sleep(0.7)
        self.click(element, hwnd, name)

    def click(self, element, hwnd=None, name=""):
        """A mouse click on the element's middle, its window brought up and to the front first."""
        user32 = ctypes.windll.user32
        if hwnd:
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                time.sleep(0.5)
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.3)
        r = element.CurrentBoundingRectangle
        user32.SetCursorPos((r.left + r.right) // 2, (r.top + r.bottom) // 2)
        time.sleep(0.2)
        user32.mouse_event(0x2, 0, 0, 0, 0)
        user32.mouse_event(0x4, 0, 0, 0, 0)
        _log(f"press {name or '?'}: clicked at its middle")

    def texts(self, hwnd):
        try:
            found = self.uia.ElementFromHandle(hwnd).FindAll(4, self.uia.CreateTrueCondition())
        except Exception:
            return []
        return [found.GetElement(i).CurrentName or "" for i in range(found.Length)]


def _main_window(ui):
    return next((h for h, t in ui.windows() if t == "WhatsApp Beta"), None)


def _call_window(ui):
    """The call's own window (any Beta window that isn't the main one)."""
    return next((h for h, t in ui.windows() if t != "WhatsApp Beta"), None)


def _open(ui, window):
    """The call's window is still there (a popup of the call, e.g. a warning, is a window too: not _call_window's)."""
    return any(h == window for h, _ in ui.windows())


def _wait(fn, seconds, step=0.3):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        got = fn()
        if got:
            return got
        time.sleep(step)
    return None


WAITING = re.compile(r"calling|ringing|connecting", re.IGNORECASE)


def connected(texts, was_waiting=False):
    """The call was answered: its clock runs, or "Calling… / Ringing…" was on it and is gone (2026-11-02 the
    clock wasn't among the window's texts, and an answered call went by unnoticed). Pure, for the tests."""
    if any(CLOCK.match(t.strip()) for t in texts):
        return True
    return was_waiting and not any(WAITING.search(t) for t in texts)


class Answered:
    """Watches a ringing call's window texts: answered() once connected() says so. The texts are logged until
    then, each new set once (what the window shows, for when this needs tuning). Not the chat's call bubble: it
    says "Voice call Ringing 22:03" until the call ends."""

    def __init__(self, ui, window):
        self.ui, self.window, self.was_waiting, self.last = ui, window, False, None

    def answered(self):
        texts = [t for t in self.ui.texts(self.window) if t]
        shown = sorted(set(texts))
        if shown != self.last:
            self.last = shown
            _log("call window: " + " | ".join(shown)[:600])
        if connected(texts, self.was_waiting):
            return True
        self.was_waiting = self.was_waiting or any(WAITING.search(t) for t in texts)
        return False


def is_owner(texts, owner):
    """The incoming call is from the owner: their number is on it (pure, for the tests)."""
    return bool(owner) and any(owner in re.sub(r"[^\d]", "", t) for t in texts if t)


# --- the call ---------------------------------------------------------------------------------------------

def beta_running():
    """WhatsApp Beta's processes (it may run in the tray without a window)."""
    import psutil

    found = []
    for p in psutil.process_iter(["exe"]):
        if BETA_ID in (p.info.get("exe") or "").lower():
            found.append(p)
    return found


def close_beta():
    """Closes WhatsApp Beta (tests; Umay keeps it running so the owner's calls to her are answered)."""
    import psutil

    procs = beta_running()
    for p in procs:
        try:
            p.terminate()
        except psutil.Error:
            pass
    psutil.wait_procs(procs, timeout=5)
    _log(f"closed WhatsApp Beta ({len(procs)} processes)")


SW_MINIMIZE = 6
BETA_APP = f"shell:AppsFolder\\{BETA_FAMILY}!App"


def _beta_main_windows():
    """Beta's main window(s), minimized or not: [hwnd]."""
    import hud_windows
    import system_tools

    return [w.hwnd for w in hud_windows.top_windows()
            if w.visible and w.title == "WhatsApp Beta" and BETA_ID in (system_tools._exe_path(w.pid) or "").lower()]


def minimize_beta():
    """Beta's main window goes down to the taskbar (it stays running, so calls to Umay are answered)."""
    for hwnd in _beta_main_windows():
        ctypes.windll.user32.ShowWindow(hwnd, SW_MINIMIZE)


def start_beta(wait=60):
    """At Umay's start (the owner, 2026-11-04): WhatsApp Beta runs, minimized, so the owner's calls to her are
    answered and her own calls start at once. Started if it isn't running; True once it runs."""
    import subprocess

    if not beta_running():
        _log("WhatsApp Beta isn't running: starting it minimized")
        subprocess.Popen(["explorer.exe", BETA_APP])
        if not _wait(_beta_main_windows, wait, 1.0):
            _log("WhatsApp Beta's window didn't show")
            return bool(beta_running())
        time.sleep(3)  # it draws its chats first; minimized too early it comes back up
    minimize_beta()
    return True


def call_owner(reason="", hint=""):
    """Calls the owner (a voice call from Umay's WhatsApp); returns what to tell the user. The call runs in the
    background. WhatsApp Beta is opened if it isn't running, and closed again after the call. `hint` goes to
    the model with the owner's first answer (voice_session.Session.say)."""
    if not number():
        return "I can't call you yet: UMAY_CALL_NUMBER (your WhatsApp number) isn't set."
    if _state["deps"] is None:
        return "Calling isn't set up in this Umay."
    if voice.gaming["on"]:  # the owner's rule (2026-11-02): the game keeps the VRAM, the call waits
        return "A game is running, so my voice is off to keep the VRAM free: I'm not calling. Write to me here."
    with _state["lock"]:
        if _state["busy"]:
            return "I'm already on a call with you."
        _state["busy"] = True
    threading.Thread(target=_dial, args=(reason, hint), daemon=True).start()
    return "Calling you on WhatsApp now."


def _dial(reason, hint=""):
    opened = False
    try:
        _log(f"calling ({reason})")
        ui = _Uia()
        try:
            opened = not beta_running()
        except Exception as e:
            _log(f"couldn't tell whether WhatsApp Beta runs ({e!r})")
        if opened:
            _log("WhatsApp Beta isn't running: opening it")
        if not whatsapp_tools.launch_in(f"whatsapp://send?phone={number()}", BETA_FAMILY):
            raise RuntimeError("WhatsApp Beta didn't open")
        main = _wait(lambda: _main_window(ui), 60 if opened else 25)
        time.sleep(4.0 if opened else 1.5)  # the chat opens: the header (and its buttons) is drawn again
        button = main and _wait(lambda: ui.find(main, "Voice call"), 40 if opened else 20)
        if not button:
            raise RuntimeError("couldn't find the Voice call button in WhatsApp Beta")
        _use_devices()
        ui.press(button, main, "Voice call")
        window = _wait(lambda: _call_window(ui), 15)
        if not window:
            raise RuntimeError("the call didn't start")
        _log("ringing")
        watch = Answered(ui, window)
        if not _wait(lambda: _call_window(ui) is None or watch.answered(), RING_SECONDS, 0.5) \
                or _call_window(ui) is None:
            _log("not answered")
            return  # not answered (or hung up)
        _log("answered")
        _talk(ui, window, reason or "You asked me to call. I'm here.", hint)
        _log("ended")
    except Exception as e:
        _log("failed:\n" + traceback.format_exc())
        _notify(f"📞 Couldn't call you: {e}")
    finally:
        _restore_devices()
        try:
            time.sleep(2)  # the call's window closes first
            minimize_beta()  # it stays running, out of the way (the owner, 2026-11-04)
        except Exception as e:
            _log(f"couldn't minimize WhatsApp Beta ({e!r})")
        _state["busy"] = False


def _use_devices():
    try:
        call_devices.use_for_call()
        _log("devices: the communication speakers and microphone are the call's")
    except Exception as e:
        _log(f"devices: couldn't switch them ({e!r})")


def _restore_devices():
    try:
        if call_devices.restore():
            _log("devices: the owner's communication speakers and microphone are back")
    except Exception as e:
        _log(f"devices: couldn't restore them ({e!r})")


def _talk(ui, window, first, hint=""):
    """The conversation while the call lasts: she says `first`, then listens and answers."""
    answer, new_chat, notify, activity, log_dir = _state["deps"]
    session = voice_session.Session(answer, new_chat, voice_session._local_transcribe, notify, activity,
                                    voice_session._NoLight(), log_dir, recorder=call_audio.LoopbackRecorder,
                                    idle_seconds=CALL_IDLE_SECONDS, log_suffix="call")
    _state["session"] = session
    voice.set_output(call_audio.CABLE)
    try:
        ear = call_audio.CableEar()
    except Exception as e:
        ear = None
        _log(f"sound: can't measure the cable ({e})")
    try:
        session.start(floor=0.0)
        if first:
            session.say(first, hint)
        ticks = 0
        while _open(ui, window) and session.active():
            time.sleep(WATCH_SECONDS)
            ticks += 1
            if ticks % SOUND_LOG_TICKS == 0:
                cable, heard = call_audio.peaks(ear)
                _log(f"sound: to the call {cable:.2f}, from the call {heard:.2f}, {'speaking' if voice.speaking() else 'listening'}")
    finally:
        session.close("the call ended")
        voice.set_output(None)
        _state["session"] = None
        if ear is not None:
            ear.stop()


def _notify(text):
    deps = _state["deps"]
    try:
        (deps[2] if deps else print)(text)
    except Exception:
        pass


# --- the owner calling her ---------------------------------------------------------------------------------

def watch():
    """Answers the owner's calls to Umay's number (a background loop; main.py)."""
    if _state["watching"]:
        return
    _state["watching"] = True

    def loop():
        _restore_devices()  # a call cut short by a crash
        if number():
            try:
                start_beta()
            except Exception as e:
                _log(f"starting WhatsApp Beta failed ({e!r})")
        ui = None
        while True:
            time.sleep(WATCH_SECONDS)
            if _state["busy"] or not number() or _state["deps"] is None:
                continue
            try:
                ui = ui or _Uia()
                # the call's own window first, the (minimized) main window last
                for hwnd, _ in sorted(ui.windows(minimized=True), key=lambda w: w[1] == "WhatsApp Beta"):
                    accept = ui.find(hwnd, match=_is_accept)
                    if accept is None or not is_owner(ui.texts(hwnd), number()):
                        continue
                    if voice.gaming["on"]:  # not answered: it rings out, and they're told why once
                        if hwnd not in _state["unanswered"]:
                            _state["unanswered"].add(hwnd)
                            _log("the owner's call: not answered, a game is running")
                            _notify("📞 A game is running, so my voice is off: I'm not answering. Write to me here.")
                        continue
                    _answer(ui, hwnd)
                    break
            except Exception as e:
                print(f"Calls: watching failed: {e!r}")
                ui = None
    threading.Thread(target=loop, daemon=True).start()


def _is_accept(name):
    return name.startswith("Accept")


ACCEPT_NAMES = ("Accept call", "Accept")  # the incoming call's window has both (2026-11-04 13:02)


def _accept(ui, hwnd):
    """Presses the incoming call's Accept and checks it took (no Accept button left): each Accept button by
    Invoke, then by a mouse click with the window in front. 2026-11-04 13:02, the first call the owner made to
    her: Accept was invoked, nothing checked it, she talked into a call that was still ringing."""
    for how in ("invoke", "click"):
        for name in ACCEPT_NAMES:
            element = ui.find(hwnd, name)
            if element is None:
                continue
            try:
                if how == "invoke":
                    pattern = element.GetCurrentPattern(INVOKE)
                    pattern.QueryInterface(ui.module.IUIAutomationInvokePattern).Invoke()
                else:
                    ui.click(element, hwnd, name)
            except Exception as e:
                _log(f"accept: {how} {name!r} failed ({e})")
                continue
            if _wait(lambda: ui.find(hwnd, match=_is_accept) is None, 4, 0.3):
                _log(f"accept: {how} {name!r} took")
                return True
            _log(f"accept: {how} {name!r} didn't take (Accept still shown)")
    return False


def _answer(ui, hwnd):
    with _state["lock"]:
        if _state["busy"]:
            return
        _state["busy"] = True
    try:
        title = next((t for h, t in ui.windows(minimized=True) if h == hwnd), "?")
        _log(f"answering the owner's call (window {title!r})")
        _use_devices()
        if not _accept(ui, hwnd):
            _log("couldn't accept the call")
            _notify("📞 Your call rang, but I couldn't pick it up (calls.log says why).")
            return
        window = _wait(lambda: _call_window(ui), 10)
        watch = Answered(ui, window) if window else None
        if window and _wait(lambda: watch.answered() or (watch.last is not None and not watch.was_waiting), 15, 0.5):
            _talk(ui, window, "Hi, it's Umay. I'm listening.")
    except Exception as e:
        _log("answering failed:\n" + traceback.format_exc())
        _notify(f"📞 Couldn't answer your call: {e}")
    finally:
        _restore_devices()
        _state["busy"] = False


# --- the tool ---------------------------------------------------------------------------------------------

def call_owner_tool(reason: str = "") -> str:
    return call_owner(reason)


TOOLS = [{
    "type": "function",
    "function": {
        "name": "call_owner",
        # Only when asked (2026-11-03: in a spoken conversation the local model called on its own, unasked);
        # Umay's own urgent calls go through companion.py, not this tool.
        "description": "Calls the user on WhatsApp (a voice call from Umay's own number) and talks with them. Use "
                       "ONLY when the user explicitly asks you to call them now; never on your own.",
        "parameters": {"type": "object", "properties": {"reason": {
            "type": "string", "description": "What to say first when they answer: why you called, one sentence."}},
            "required": []},
    },
}]
TOOL_FUNCTIONS = {"call_owner": call_owner_tool}
