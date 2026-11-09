"""The memory file's place and the wallpaper's view of it (the JSON contract at the top of cortex.html), with
nothing but sqlite3: the HUD's process reads it through this. Importing numpy there (memory_cortex) hung the
HUD's main thread inside the exe once QtWebEngine was up (2026-11-07), so the HUD never imports memory_cortex.
"""
import json
import os
import sqlite3
from pathlib import Path

import paths


def db_path():
    given = os.environ.get("UMAY_CORTEX_DB", "").strip()
    return Path(given) if given else paths.voice_home().parent / "umay_memory.db"


def export(c):
    """The graph from an open connection (rows as sqlite3.Row)."""
    nodes = [dict(r) for r in c.execute(
        "SELECT id,label,branch,parent,type,importance,created,accessed,status FROM nodes WHERE valid_to IS NULL")]
    edges = [dict(r) for r in c.execute(
        "SELECT e.source, e.target, e.kind FROM edges e JOIN nodes a ON a.id=e.source JOIN nodes b "
        "ON b.id=e.target WHERE a.valid_to IS NULL AND b.valid_to IS NULL")]
    events = [{"id": r["id"], "type": r["type"], "nodes": json.loads(r["nodes"]), "ts": r["ts"]}
              for r in c.execute("SELECT * FROM events ORDER BY ts ASC")]
    meta = {r["k"]: r["v"] for r in c.execute("SELECT k, v FROM meta")}
    return {"nodes": nodes, "edges": edges, "events": events,
            "meta": {"consolidated_at": int(meta["consolidated_at"]) if "consolidated_at" in meta else None,
                     "state": meta.get("state", "idle")}}


def summary(path=None):
    """For the HUD's MEMORY panel: counts, the last fact filed (with its path) and the state; None: no file yet."""
    path = Path(path) if path else db_path()
    if not path.exists():
        return None
    c = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
    try:
        c.row_factory = sqlite3.Row
        count = lambda where: c.execute(f"SELECT COUNT(*) FROM nodes WHERE valid_to IS NULL AND {where}").fetchone()[0]
        by_id = {r["id"]: r for r in c.execute("SELECT id, label, parent FROM nodes")}
        last = c.execute("SELECT id, label, created FROM nodes WHERE valid_to IS NULL AND type='fact' "
                         "AND status='placed' ORDER BY created DESC LIMIT 1").fetchone()
        trail, seen = [], set()
        if last:
            nid = by_id[last["id"]]["parent"]
            while nid and nid in by_id and nid not in seen:
                seen.add(nid)
                trail.append(by_id[nid]["label"])
                nid = by_id[nid]["parent"]
        state = c.execute("SELECT v FROM meta WHERE k='state'").fetchone()
        return {"neurons": count("1=1"),  # every node the wallpaper draws
                "user": count("branch='user' AND type IN ('fact', 'entity') AND status='placed'"),
                "self": count("branch='self' AND type='fact'"),
                "inbox": count("status='inbox'"),
                "episodes": count("type='episode'"),
                "last": {"label": last["label"], "path": " › ".join(reversed(trail)), "at": last["created"]}
                if last else None,
                "state": state["v"] if state else "idle"}
    finally:
        c.close()


def read(path=None):
    """The graph, read only (the file not made yet: an empty graph)."""
    path = Path(path) if path else db_path()
    if not path.exists():
        return {"nodes": [], "edges": [], "events": [], "meta": {"consolidated_at": None, "state": "idle"}}
    c = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
    try:
        c.row_factory = sqlite3.Row
        return export(c)
    finally:
        c.close()
