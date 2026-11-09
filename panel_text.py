"""What the HUD's detail views say about the disk scan and the credit log (they took the Ctrl+F1 panel's
Disk and Credits tabs' place). Pure: no Qt, no Tk; the HUD draws it, the tests read it.

- Disk: the items the filters let through and how they sort, each column's text, the selected item's
  details, why it has no delete button, the "Delete X?" confirmation (how it goes, what goes with it,
  what breaks) and what a delete came to.
- Credits: money, the periods, each entry's columns, its request-by-request details and the totals.
- The OpenRouter and Tavily credit lines.
"""
import time

import disk_actions
import disk_tools
import i18n
from disk_watch import size_text

# --- credits (OpenRouter, Tavily) -------------------------------------------------------------------

def openrouter_text(lang, balance):
    """What the OpenRouter line shows for openrouter_balance()'s result (USD or None)."""
    if balance is None:
        return i18n.get(lang, "panel_failed")
    return f"${balance:.2f}"


def tavily_text(lang, result):
    """What the Tavily line shows for _get_tavily_credits()'s (kind, data)."""
    kind, data = result
    if kind == "ok":
        return i18n.get(lang, "panel_tavily_value", remaining=number(data["remaining"]), limit=number(data["limit"]))
    return i18n.get(lang, "panel_unknown" if kind == "unknown" else "panel_failed")


def number(x):
    if x is None:
        return "—"
    return str(int(x)) if float(x).is_integer() else f"{x:.1f}"


# --- the credit log --------------------------------------------------------------------------------

CREDIT_COLUMNS = ("time", "source", "what", "model", "requests", "cost", "before", "after", "difference")
PERIODS = (("today", None), ("week", 7), ("month", 30), ("all", 0))
MISMATCH = 0.2   # the balance difference and the reported cost disagree by more than this share


def money(x, digits=4):
    if x is None:
        return "—"
    if 0 < abs(x) < 0.001:
        return f"${x:.6f}"
    return f"${x:.{digits}f}"


def since(period, now=None):
    """Start of a period: today (midnight), the last 7 / 30 days, or everything (0)."""
    now = now or time.time()
    days = dict(PERIODS)[period]
    if days is None:
        t = time.localtime(now)
        return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
    return now - days * 86400 if days else 0


def models(entry):
    return ", ".join(dict.fromkeys(r.get("model", "") for r in entry.rounds)) or "—"


def difference_text(lang, entry):
    if entry.before is None:
        return "—"
    if entry.after is None:
        return i18n.get(lang, "credits_pending")
    return money(entry.difference) + (" *" if entry.overlap else "")


def credit_row(lang, e):
    """An entry's columns (CREDIT_COLUMNS)."""
    return (time.strftime("%m-%d %H:%M:%S", time.localtime(e.started)), i18n.get(lang, f"credits_source_{e.source}"),
            e.what or "—", models(e), str(e.requests), money(e.cost), money(e.before), money(e.after),
            difference_text(lang, e))


def credit_state(e):
    """How a row looks: "pending" (its after balance isn't in yet), "overlap" or ""."""
    return "pending" if e.before is not None and e.after is None else "overlap" if e.overlap else ""


def totals(by):
    """(cost, entries) of a Ledger.totals() answer."""
    return sum(c for _, c in by.values()), sum(n for n, _ in by.values())


def credit_summary(lang, ledger, now=None):
    """"Today: $0.0163 (2) · Last 7 days: …" for today, the week and the month."""
    now = now or time.time()
    parts = []
    for p in ("today", "week", "month"):
        cost, count = totals(ledger.totals(since(p, now)))
        parts.append(i18n.get(lang, "credits_summary_part", period=i18n.get(lang, f"credits_period_{p}"),
                              cost=money(cost), count=count))
    return "  ·  ".join(parts)


def by_source_text(lang, by):
    return "  ·  ".join(f"{i18n.get(lang, 'credits_source_' + s)} {money(c)} ({n})"
                        for s, (n, c) in sorted(by.items(), key=lambda x: -x[1][1])) or i18n.get(lang, "credits_empty")


def credit_details(lang, entry):
    """The selected entry, request by request."""
    t = lambda key, **kw: i18n.get(lang, key, **kw)  # noqa: E731
    lines = [f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(entry.started))} · "
             f"{t('credits_source_' + entry.source)} · {entry.what or '—'}"]
    for n, r in enumerate(entry.rounds, 1):
        tools = f" · {t('credits_tools')}: {', '.join(r['tools'])}" if r.get("tools") else ""
        lines.append(t("credits_round", n=n, model=r.get("model", "?"), prompt=r.get("prompt_tokens", 0),
                       completion=r.get("completion_tokens", 0), cost=money(r.get("cost", 0))) + tools)
    lines.append(t("credits_total", cost=money(entry.cost), before=money(entry.before), after=money(entry.after),
                   difference=money(entry.difference)))
    if entry.searches:
        used = (entry.tavily_before - entry.tavily_after
                if entry.tavily_before is not None and entry.tavily_after is not None else None)
        lines.append(t("credits_tavily", searches=entry.searches, before=number(entry.tavily_before),
                       after=number(entry.tavily_after), used=number(used)))
    if entry.overlap:
        lines.append(t("credits_overlap_note"))
    elif entry.difference is not None and abs(entry.difference - entry.cost) > max(0.0005, entry.cost * MISMATCH):
        lines.append(t("credits_mismatch_note"))
    if entry.before is None:
        lines.append(t("credits_no_before_note"))
    return "\n".join(lines)


# --- the disk scan ----------------------------------------------------------------------------------

DISK_COLUMNS = ("kind", "name", "size", "used", "what", "redl", "deps")
MIN_SIZES = ((0, "0"), (100 * 1024 ** 2, "100 MB"), (1024 ** 3, "1 GB"), (10 * 1024 ** 3, "10 GB"))


def used_text(lang, item, now=None):
    """'2026-10-21 · 3 days · Steam' style text for the Last used column."""
    now = now or time.time()
    source = i18n.get(lang, f"disk_src_{item.used_source}") if item.used_source else ""
    if not item.last_used:
        return source or "—"
    days = int((now - item.last_used) // 86400)
    when = time.strftime("%Y-%m-%d", time.localtime(item.last_used))
    ago = i18n.get(lang, "disk_days_ago", days=days) if days > 0 else i18n.get(lang, "disk_today")
    return f"{when} · {ago} · {source}"


def what_text(lang, item):
    if not item.what:
        return "—"
    if item.what_source == "gemini":
        return f"{item.what}  [{i18n.get(lang, 'disk_what_src_gemini')}]"
    return item.what


def graph(scan):
    """(by_id, users, same) of a scan: items by id, who uses each item, the venvs that are copies."""
    users = {}
    for it in scan.items:
        for d in it.dep_ids:
            users.setdefault(d, []).append(it.id)
    same = {}
    for group in disk_tools.duplicate_venvs(scan.items):
        for it in group:
            same[it.id] = [o.name for o in group if o is not it]
    return scan.by_id(), users, same


def deps_text(lang, item, by_id, users, same):
    """The Dependencies column: what's missing first (shown red), then what it needs and who uses it."""
    if item.broken:
        return i18n.get(lang, "disk_deps_missing", path=item.broken[0])
    parts = []
    names = [by_id[d].name for d in item.dep_ids if d in by_id]
    if names:
        parts.append(i18n.get(lang, "disk_deps_needs", names=", ".join(names)))
    if users.get(item.id):
        parts.append(i18n.get(lang, "disk_deps_used_by", count=len(users[item.id])))
    if same.get(item.id):
        parts.append(i18n.get(lang, "disk_deps_same", names=", ".join(same[item.id])))
    return " · ".join(parts) or "—"


def sort_key(column, item):
    return {
        "kind": item.kind, "name": item.name.lower(), "size": item.size, "used": item.last_used or 0,
        "what": item.what.lower(), "redl": item.redownloadable, "deps": (bool(item.broken), len(item.dep_ids)),
    }[column]


def disk_rows(scan, kind="", min_size=0, column="size", reverse=True):
    """The items the filters let through, sorted (a link or a broken item shows whatever its size)."""
    if scan is None:
        return []
    items = [it for it in scan.items if (not kind or it.kind == kind)
             and (it.size >= min_size or (it.kind == "link" and kind == "link") or it.broken)]
    return sorted(items, key=lambda it: sort_key(column, it), reverse=reverse)


def first_sort_reverse(column):
    """Clicking a new column: biggest / newest / most dependencies first, names A to Z."""
    return column in ("size", "used", "deps")


def disk_row(lang, it, by_id, users, same):
    """An item's columns (DISK_COLUMNS)."""
    return (i18n.get(lang, f"disk_kind_{it.kind}"), it.name,
            size_text(it.size) + ("~" if it.size_source == "registry" else ""), used_text(lang, it),
            what_text(lang, it), i18n.get(lang, "disk_yes" if it.redownloadable else "disk_no"),
            deps_text(lang, it, by_id, users, same))


def disk_state(it):
    """How a row looks: "broken" (red), "protected" (dim) or ""."""
    return "broken" if it.broken else "protected" if it.protected else ""


def item_details(lang, it):
    t = lambda key, **kw: i18n.get(lang, key, **kw)  # noqa: E731
    lines = [f"{it.name}  ·  {size_text(it.size)}"]
    if it.shared:
        lines[0] += "  " + t("disk_shared", size=size_text(it.shared))
    if it.path:
        lines.append(f"{t('disk_details_path')}: {it.path}" + (f"  (+{len(it.parts)})" if it.parts else ""))
    lines.append(f"{t('disk_col_used')}: {used_text(lang, it)}")
    if it.what:
        lines.append(f"{t('disk_col_what')}: {it.what} ({t('disk_what_src_' + it.what_source)})")
    if "redownloadable_guess" in it.info:
        lines.append(t("disk_guess_redl", value=t("disk_yes" if it.info["redownloadable_guess"] else "disk_no")))
    if it.broken:
        lines.append(t("disk_deps_missing", path=", ".join(it.broken)))
    return "\n".join(lines)


def no_delete_text(lang, it):
    """Why an item has no delete button ("" when it has one)."""
    if disk_actions.plan(it) is not None:
        return ""
    return i18n.get(lang, "disk_protected", reason=it.protected) if it.protected else i18n.get(lang, "disk_no_action")


def confirm_text(lang, item, p, going, breaking):
    """"Delete X?": how it goes, what goes with it, what breaks."""
    t = lambda key, **kw: i18n.get(lang, key, **kw)  # noqa: E731
    how = {"url": t("disk_how_url", appid=item.info.get("appid", "")),
           "uninstaller": t("disk_how_uninstaller", command=p.describe()),
           "command": t("disk_how_command", command=p.describe()),
           "delete": t("disk_how_delete", size=size_text(p.size)),
           "trash": t("disk_how_trash", size=size_text(p.size)),
           "contents": t("disk_how_contents")}[p.mode]
    lines = [how, ""]
    if going:
        lines.append(t("disk_confirm_inside"))
        lines += [f"  • {g.name} ({size_text(g.size)})" for g in going[:12]]
        if len(going) > 12:
            lines.append(f"  … +{len(going) - 12}")
    if breaking:
        lines.append(t("disk_confirm_breaks"))
        lines += [f"  • {b.name} — {t('disk_kind_' + b.kind)}" for b in breaking[:12]]
        if len(breaking) > 12:
            lines.append(f"  … +{len(breaking) - 12}")
    if not going and not breaking:
        lines.append(t("disk_confirm_nothing"))
    return "\n".join(lines)


def result_text(lang, item, result):
    """(text, ok) for what a delete came to (disk_actions.execute's Result)."""
    t = lambda key, **kw: i18n.get(lang, key, **kw)  # noqa: E731
    if result.error == "too_big_for_bin":
        return t("disk_too_big_for_bin", size=size_text(item.size), free=size_text(result.bin_free)), False
    if result.error == "not_in_bin":
        return t("disk_not_in_bin", name=item.name), False
    if not result.ok:
        return t("disk_failed", error=result.error), False
    if result.started:
        return t("disk_started", name=item.name), True
    return t("disk_done", name=item.name, freed=size_text(result.freed)), True


def scan_status(lang, scan, running, progress):
    """The line under the disk bar: scanning, never scanned, or when the last scan was."""
    if running:
        return progress or i18n.get(lang, "disk_scanning")
    if scan is None:
        return i18n.get(lang, "disk_never_scanned")
    return i18n.get(lang, "disk_last_scan", when=time.strftime("%Y-%m-%d %H:%M", time.localtime(scan.at)),
                    seconds=scan.stats.get("seconds", 0), items=len(scan.items))
