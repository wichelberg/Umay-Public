"""A throwaway desktop app for live tests (run_live.py): buttons, boxes and a mail-like form, nothing real behind them.

Everything that happens to it goes to Documents\\UmayPlanTest\\state.json, so a test reads what really happened
instead of what Umay says. Built to an .exe by run_live.py, so "run this exe" is tested on a real program.
"""
import json
import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk

HERE = Path.home() / "Documents" / "UmayPlanTest"
STATE = HERE / "state.json"
TITLE = "Umay Test App"
CODE = sys.argv[1] if len(sys.argv) > 1 else "ZEBRA-42"

events = []
root = tk.Tk()
root.title(TITLE)
root.geometry("900x720+120+60")
fields = {}


def save():
    state = {"code": CODE, "events": events, "fields": {k: f() for k, f in fields.items()}}
    temp = STATE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    temp.replace(STATE)


def happened(name):
    events.append(name)
    save()


def tick():
    save()
    root.after(300, tick)


tk.Label(root, text=f"Test code: {CODE}", font=("Segoe UI", 20, "bold")).pack(pady=6)

row = tk.Frame(root)
row.pack(pady=4)
for label, colour in (("Send Report", "#2d6cdf"), ("Save Draft", "#2e9e4f"), ("Cancel", "#c23b3b")):
    tk.Button(row, text=label, bg=colour, fg="white", font=("Segoe UI", 12), width=14,
              command=lambda n=label: happened(f"button:{n}")).pack(side="left", padx=8)

row = tk.Frame(root)
row.pack(pady=4)
for label in ("Option A", "Option B", "Option C"):
    value = tk.IntVar()
    tk.Checkbutton(row, text=label, variable=value, font=("Segoe UI", 12),
                   command=lambda n=label, v=value: happened(f"check:{n}:{v.get()}")).pack(side="left", padx=10)

row = tk.Frame(root)
row.pack(pady=4)
items = tk.Listbox(row, height=3, font=("Segoe UI", 12), exportselection=False)
for name in ("Apple", "Banana", "Cherry"):
    items.insert("end", name)
items.pack(side="left", padx=10)
items.bind("<Double-Button-1>", lambda e: happened(f"double:{items.get(items.nearest(e.y))}"))
spot = tk.Label(row, text="Right-click here", relief="groove", width=20, height=3, font=("Segoe UI", 12))
spot.pack(side="left", padx=10)
spot.bind("<Button-3>", lambda e: happened("right-click"))

form = tk.LabelFrame(root, text="New mail", font=("Segoe UI", 12))
form.pack(fill="both", expand=True, padx=14, pady=8)
for label in ("To", "Subject"):
    line = tk.Frame(form)
    line.pack(fill="x", padx=8, pady=3)
    tk.Label(line, text=label, width=8, anchor="w", font=("Segoe UI", 12)).pack(side="left")
    box = ttk.Entry(line, font=("Segoe UI", 12))
    box.pack(side="left", fill="x", expand=True)
    fields[label] = box.get
tk.Label(form, text="Body", anchor="w", font=("Segoe UI", 12)).pack(fill="x", padx=8)
body = tk.Text(form, height=9, font=("Segoe UI", 12), wrap="word")
body.pack(fill="both", expand=True, padx=8)
fields["Body"] = lambda: body.get("1.0", "end-1c")
tk.Button(form, text="Send Mail", font=("Segoe UI", 12), width=14,
          command=lambda: happened("button:Send Mail")).pack(pady=6)

root.bind("<Control-s>", lambda e: happened("key:ctrl+s"))
HERE.mkdir(parents=True, exist_ok=True)
tick()
root.mainloop()
