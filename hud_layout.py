"""Where each HUD window goes across the monitors, and what is remembered about it. Pure: no Qt, no Win32.

A window's place is saved as the monitor it is on plus its offset from that monitor's work area
(the screen minus the taskbar), so it comes back to the same spot on the same monitor even when the
monitors are arranged differently. A monitor is known by its EDID (manufacturer, model, serial), not
by Windows' name for it (\\\\.\\DISPLAY2), which changes when monitors are plugged in again; two
identical monitors without a serial are told apart by that name.

When the saved monitor isn't there (unplugged, or the window was never moved), the window goes to
its default spot on the primary monitor; the saved place is kept, so it returns once that monitor is
back. A smaller screen than before can't push a window off it: it is clamped inside the work area.
"""
from dataclasses import dataclass

MARGIN = 64  # px from the work area's edges for the default spots


@dataclass(frozen=True)
class Screen:
    key: str      # screen_key(): stable across replugging
    name: str     # Windows' device name
    area: tuple   # work area (x, y, w, h), logical px
    primary: bool = False


def screen_key(manufacturer, model, serial, name):
    """A monitor's identity: its EDID when Windows reports one, else its device name."""
    parts = [p.strip() for p in (manufacturer, model, serial)]
    return "|".join(parts) if any(parts) else name


def primary(screens):
    return next((s for s in screens if s.primary), screens[0])


def find_screen(screens, saved):
    """The monitor `saved` (a remember() dict's "screen") was on, or None if it isn't connected."""
    if not saved:
        return None
    same = [s for s in screens if s.key == saved.get("key")]
    if len(same) > 1:  # identical monitors: the device name tells them apart
        return next((s for s in same if s.name == saved.get("name")), same[0])
    return same[0] if same else None


def clamp(x, y, size, area):
    """(x, y) moved so a window of `size` lies inside `area` (its top-left wins if it can't fit)."""
    ax, ay, aw, ah = area
    w, h = size
    return max(ax, min(x, ax + aw - w)), max(ay, min(y, ay + ah - h))


def default_spot(slot, size, area):
    """A window's place when nothing is saved: a corner of the work area, or its center."""
    ax, ay, aw, ah = area
    w, h = size
    x = {"left": ax + MARGIN, "right": ax + aw - w - MARGIN}.get(slot.split("-")[-1], ax + (aw - w) // 2)
    if slot.startswith("top"):
        y = ay + MARGIN
    elif slot.startswith("bottom"):
        y = ay + ah - h - MARGIN
    else:
        y = ay + (ah - h) // 2
    return clamp(x, y, size, area)


def place(saved, slot, size, screens):
    """Where a window goes: (x, y, screen). `saved` is what remember() returned for it, or None."""
    screen = find_screen(screens, (saved or {}).get("screen"))
    if screen is None:
        screen = primary(screens)
        return (*default_spot(slot, size, screen.area), screen)
    ax, ay, _, _ = screen.area
    dx, dy = saved["offset"]
    return (*clamp(ax + dx, ay + dy, size, screen.area), screen)


def screen_at(screens, x, y):
    """The monitor whose work area holds (x, y), else the one nearest to it."""
    def distance(s):
        ax, ay, aw, ah = s.area
        return max(ax - x, 0, x - (ax + aw)) ** 2 + max(ay - y, 0, y - (ay + ah)) ** 2
    return min(screens, key=distance)


def remember(x, y, size, screens):
    """What to save for a window the owner dropped at (x, y): the monitor under its center and its
    offset there."""
    w, h = size
    screen = screen_at(screens, x + w / 2, y + h / 2)
    ax, ay, _, _ = screen.area
    x, y = clamp(x, y, size, screen.area)
    return {"screen": {"key": screen.key, "name": screen.name}, "offset": [x - ax, y - ay]}
