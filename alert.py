import ctypes
import itertools
import queue
import tkinter as tk

_winmm = ctypes.windll.winmm
_queue = queue.Queue()
_alias_counter = itertools.count(1)
_popups = {}  # token -> close function, only used on the Tk thread
_silencers = {}  # token -> stops that popup's sound, only used on the Tk thread


def _mci(command: str) -> None:
    buf = ctypes.create_unicode_buffer(255)
    _winmm.mciSendStringW(command, buf, 254, 0)


def show_choice(token, text, sound_path, choices, on_choice, title="Umay", close_choice=None):
    """Thread-safe popup with a looping sound (none if sound_path is None) and one button per
    choice. on_choice(index) runs on the Tk thread when a button is clicked; closing the window
    only stops the sound, or counts as clicking choices[close_choice] if that's given.
    close(token) removes the popup (e.g. once the question was answered elsewhere)."""
    _queue.put(("popup", token, text, sound_path, tuple(choices), on_choice, title, close_choice))


def close(token) -> None:
    """Thread-safe. Removes the popup shown with this token, if it's still open."""
    _queue.put(("close", token))


def silence(token) -> None:
    """Thread-safe. Stops the sound of the popup shown with this token but leaves the popup open."""
    _queue.put(("silence", token))


def _open_popup(root, token, text, sound_path, choices, on_choice, title, close_choice=None):
    alias = f"event_alert_{next(_alias_counter)}"
    playing = sound_path is not None
    if playing:
        _mci(f'open "{sound_path}" alias {alias}')
        _mci(f"play {alias} repeat")

    win = tk.Toplevel(root)
    win.title(title)
    win.attributes("-topmost", True)
    win.resizable(False, False)

    tk.Label(win, text=text, font=("Segoe UI", 14), padx=30, pady=20, justify="left").pack()

    def stop_sound():
        nonlocal playing
        if playing:
            playing = False
            _mci(f"stop {alias}")
            _mci(f"close {alias}")

    def close_popup():
        stop_sound()
        _popups.pop(token, None)
        _silencers.pop(token, None)
        if win.winfo_exists():
            win.destroy()

    def choose(index):
        close_popup()
        if on_choice is not None:
            on_choice(index)

    buttons = tk.Frame(win)
    buttons.pack(pady=(0, 20))
    for index, label in enumerate(choices):
        tk.Button(buttons, text=label, command=lambda i=index: choose(i), padx=10).pack(side="left", padx=6)
    if on_choice is None:
        win.protocol("WM_DELETE_WINDOW", lambda: choose(0))
    elif close_choice is not None:
        win.protocol("WM_DELETE_WINDOW", lambda: choose(close_choice))
    else:
        win.protocol("WM_DELETE_WINDOW", close_popup)
    if token is not None:
        _popups[token] = close_popup
        _silencers[token] = stop_sound


def _poll_queue(root: tk.Tk) -> None:
    while not _queue.empty():
        kind, token, *rest = _queue.get()
        if kind == "popup":
            _open_popup(root, token, *rest)
        elif kind == "close" and token in _popups:
            _popups[token]()
        elif kind == "silence" and token in _silencers:
            _silencers[token]()
    root.after(200, _poll_queue, root)


def init(root: tk.Tk) -> None:
    """Start polling the alert queue against a shared Tk root."""
    root.after(200, _poll_queue, root)
