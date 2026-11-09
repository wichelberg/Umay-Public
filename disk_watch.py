"""The Disk tab's engine and the daily scan.

Once a day, when nobody has touched the PC for a while, a scan runs in the background with low CPU and
disk priority. Later scans only re-read what changed: the walk itself is cheap (one listing per
folder, about a minute for the whole drive), while package lists, exe info, project code and what an
item is come from the cache and the knowledge unless their files changed. After each scan the
findings are checked against the limits and only a new one makes the pet say "Disk: new findings";
a click on that bubble opens the Disk tab.

Limits (.env): UMAY_DISK_GROW_GB (2) an item grew more than this since the previous scan,
UMAY_DISK_FREE_GB (150) free space fell below this, UMAY_DISK_STALE_DAYS (60) and
UMAY_DISK_STALE_GB (10) an item this big unused this long, UMAY_DISK_MEI_COUNT (3) this many
_MEI leftovers. UMAY_DISK_IDLE_MINUTES (10) how long the PC must be idle; UMAY_DISK_WATCH=off
stops the daily scan (the panel's Scan now still works).
"""
import os
import threading
import time
from dataclasses import dataclass

import disk_memory
import disk_tools
import i18n

CHECK_EVERY_SECONDS = 600
DAY_SECONDS = 23 * 3600   # "once a day" with an hour of slack, so it doesn't drift later every day
GB = 1024 ** 3
NEVER_STALE = {"link", "system", "app"}

_lock = threading.Lock()
_state = {"store": None, "scan": None, "running": False, "progress": "", "listeners": [], "ask": None,
          "notify": None, "lang": lambda: "tr", "loop": False}


def _setting(name, default):
    raw = os.environ.get(f"UMAY_DISK_{name}", "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


@dataclass
class Limits:
    grow_gb: float = 2
    free_gb: float = 150
    stale_days: float = 60
    stale_gb: float = 10
    mei_count: float = 3

    @classmethod
    def from_env(cls):
        return cls(_setting("GROW_GB", 2), _setting("FREE_GB", 150), _setting("STALE_DAYS", 60),
                   _setting("STALE_GB", 10), _setting("MEI_COUNT", 3))


def configure(ask=None, notify=None, lang=None, store=None):
    """ask(prompt) -> text: Gemini Flash for items no rule knows; notify(findings): tells the owner;
    lang() -> "tr"/"en"."""
    if ask is not None:
        _state["ask"] = ask
    if notify is not None:
        _state["notify"] = notify
    if lang is not None:
        _state["lang"] = lang
    if store is not None:
        _state["store"] = store


def store():
    with _lock:
        if _state["store"] is None:
            _state["store"] = disk_memory.Store()
        return _state["store"]


def latest():
    """The newest scan (from disk after a restart), or None before the first one."""
    with _lock:
        scan = _state["scan"]
    if scan is None:
        scan = store().last_scan()
        with _lock:
            _state["scan"] = _state["scan"] or scan
    return scan


def running():
    return _state["running"]


def progress():
    return _state["progress"]


def listen(fn):
    """fn() runs (on the scan's thread) whenever the scan or its progress changes; returns a function
    that stops it."""
    _state["listeners"].append(fn)
    return lambda: fn in _state["listeners"] and _state["listeners"].remove(fn)


def _changed():
    for fn in list(_state["listeners"]):
        try:
            fn()
        except Exception as e:
            print(f"Disk: listener failed: {e}")


def scan_now(places=None, sources=None, wait=False):
    """Starts a scan (unless one is running). Returns False if one was already running."""
    with _lock:
        if _state["running"]:
            return False
        _state["running"] = True
    thread = threading.Thread(target=_scan, args=(places, sources), daemon=True)
    thread.start()
    if wait:
        thread.join()
    return True


def _scan(places, sources):
    disk_tools.set_background_priority()
    lang = _state["lang"]()
    try:
        st = store()

        def report(text):
            _state["progress"] = text
            _changed()

        cache = st.load_cache()
        scan = disk_tools.scan(places, sources, lang, cache, report)
        scan.stats["gemini_calls"] = disk_memory.describe(scan.items, st.knowledge, _state["ask"], lang)
        st.knowledge.save()
        st.save_cache(cache)
        previous = st.history.sizes_before(scan.at + 1)
        st.history.add(scan)
        st.save_scan(scan)
        with _lock:
            _state["scan"] = scan
        found = findings(scan, previous, Limits.from_env(), lang)
        new = [f for f in found if not st.history.told(f.key)]
        if new:
            st.history.tell([f.key for f in new])
            if _state["notify"] is not None:
                _state["notify"](new)
    except Exception as e:
        print(f"Disk: scan failed: {e}")
    finally:
        _state["running"] = False
        _state["progress"] = ""
        _changed()


@dataclass
class Finding:
    key: str      # the same finding is told once
    text: str
    item_id: str = ""


def findings(scan, previous, limits, lang="tr", now=None):
    """What's worth telling after a scan. previous: {item id: size} of the scan before."""
    now = now or scan.at
    t = lambda key, **kw: i18n.get(lang, key, **kw)  # noqa: E731
    day = time.strftime("%Y-%m-%d", time.localtime(scan.at))
    out = []
    if scan.free and scan.free < limits.free_gb * GB:
        out.append(Finding(f"free:{int(scan.free // (10 * GB))}",
                           t("disk_find_free", free=size_text(scan.free), limit=f"{limits.free_gb:.0f} GB")))
    for it in scan.items:
        if it.kind == "link" or it.container:
            continue
        before = previous.get(it.id)
        if before is not None and it.size - before > limits.grow_gb * GB:
            out.append(Finding(f"grew:{it.id}:{day}", t("disk_find_grew", name=it.name, size=size_text(it.size - before)),
                               it.id))
        if it.kind not in NEVER_STALE and not it.protected and it.size >= limits.stale_gb * GB \
                and it.last_used and now - it.last_used > limits.stale_days * 86400:
            out.append(Finding(f"stale:{it.id}", t("disk_find_stale", name=it.name, size=size_text(it.size),
                                                   days=int((now - it.last_used) // 86400)), it.id))
        if it.id == "mei:leftovers" and it.info.get("count", 0) >= limits.mei_count:
            out.append(Finding(f"mei:{it.info['count']}", t("disk_find_mei", count=it.info["count"]), it.id))
    return out


def size_text(n):
    for unit, size in (("TB", 1024 ** 4), ("GB", GB), ("MB", 1024 ** 2), ("KB", 1024)):
        if abs(n) >= size:
            return f"{n / size:.1f} {unit}"
    return f"{n} B"


def after_delete(scan, item):
    """The graph again once an item went: items whose folders are gone leave the list, and whatever
    needed them turns red (missing dependency). Saved as the latest scan."""
    gone = [item] + disk_tools.inside(item, scan.items)
    gone_ids = {g.id for g in gone if not any(os.path.exists(p) for p in [g.path, *g.parts] if p)}
    if item.kind in ("game", "program") and not item.path:
        gone_ids.discard(item.id)
    scan.items = [it for it in scan.items if it.id not in gone_ids]
    disk_tools.build_graph(scan.items)
    store().save_scan(scan)
    with _lock:
        _state["scan"] = scan
    _changed()
    return scan


def refresh_graph():
    """Re-checks what every item needs (after Steam or an uninstaller finished in its own window)."""
    scan = latest()
    if scan is None:
        return None
    scan.items = [it for it in scan.items if not it.path or os.path.exists(it.path) or it.kind == "link"]
    disk_tools.build_graph(scan.items)
    store().save_scan(scan)
    _changed()
    return scan


# --- the daily scan --------------------------------------------------------------------------------


def due(now=None):
    return (now or time.time()) - store().history.last_at() >= DAY_SECONDS


def start(idle_seconds=None):
    """Starts the once-a-day scan in the background (unless UMAY_DISK_WATCH=off)."""
    if os.environ.get("UMAY_DISK_WATCH", "").strip().lower() in ("off", "0", "false", "no"):
        return
    with _lock:
        if _state["loop"]:
            return
        _state["loop"] = True
    if idle_seconds is None:
        import system_tools

        idle_seconds = system_tools.idle_seconds
    threading.Thread(target=_loop, args=(idle_seconds,), daemon=True).start()


def _loop(idle_seconds):
    while True:
        try:
            if not running() and due() and idle_seconds() >= _setting("IDLE_MINUTES", 10) * 60:
                scan_now(wait=True)
        except Exception as e:
            print(f"Disk: daily scan check failed: {e}")
        time.sleep(CHECK_EVERY_SECONDS)
