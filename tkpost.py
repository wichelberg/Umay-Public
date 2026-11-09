"""Handing work to a Tk thread without waiting for it.

Umay has several Tk roots, each on its own thread (the pet, the text box / panel, the alerts).
tkinter's root.after() called from another thread doesn't return until the owner thread has taken
the call, so one Tk thread calling into another waits for it, and if that one is meanwhile waiting
for the first (a SendMessage to its windows, its own call back), both hang. That froze the app
twice on 2026-10-21 (the text box during a game, the Disk tab). A Mailbox is a plain queue the owner
thread empties every POLL_MS: post() never waits.
"""
import queue
import tkinter as tk

POLL_MS = 25


class Mailbox:
    def __init__(self, root, every=POLL_MS):
        """Create it on the root's own thread."""
        self.root, self.every = root, every
        self.queue = queue.SimpleQueue()
        root.after(every, self._drain)

    def post(self, fn):
        """Thread-safe and never blocks: fn() runs on the root's thread soon after."""
        self.queue.put(fn)

    def _drain(self):
        while True:
            try:
                fn = self.queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception as e:  # one failing call mustn't stop the ones after it
                print(f"Tk mailbox: {e!r}")
        try:
            self.root.after(self.every, self._drain)
        except tk.TclError:  # the root is gone
            pass
