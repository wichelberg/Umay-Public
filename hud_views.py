"""The painted detail views (hud_detail.py) of three HUD panels:

- Weather: now, with every value Open-Meteo gives (humidity, dew point, wind and gusts, pressure, cloud cover,
  UV, visibility, rain, daylight); the next 48 hours (temperature, feels like, rain chance, sky); 7 days.
- Vitals: the gauges, each CPU core, the GPU's power and clocks, the last 5 minutes of CPU, GPU, memory and
  network, the programs using most CPU and memory, swap, battery, uptime. Read every 2 s while open.
- Calendar: a month (‹ › to page, TODAY back) with every alarm ring in its days and the holidays (mint); a
  click on a day lists them; the coming rings and holidays, and the alarms that are off.
"""
import threading
import time
from datetime import date, datetime

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPen

import hud_data
from hud_detail import Painted, chart
from hud_paint import C, color, font, gauge, rule, text, weather_icon

READ_SECONDS = 2


def n(value, unit="", digits=0):
    return "—" if value is None else f"{value:.{digits}f}{unit}"


def head(p, x, y, label, w=None):
    text(p, x, y, label, font(13, 700, 3.0), C["cyan2"])
    if w:
        rule(p, x, y + 8, w)


def label_value(p, x, y, label, value, size=20):
    text(p, x, y, label.upper(), font(12, 600, 2.4), C["muted"])
    text(p, x, y + 26, value, font(size, 400, mono=True), C["text"])


def icon(p, x, y, kind, scale):
    p.save()
    p.translate(x, y)
    p.scale(scale, scale)
    weather_icon(p, 0, 0, kind)
    p.restore()


def rate(bps):
    if bps is None:
        return "—"
    for unit, size in (("GB/s", 2 ** 30), ("MB/s", 2 ** 20), ("KB/s", 2 ** 10)):
        if bps >= size:
            return f"{bps / size:.1f} {unit}"
    return f"{bps:.0f} B/s"


def load_color(frac):
    return C["cyan"] if frac < 0.6 else C["amber"] if frac < 0.85 else C["hot"]


# --- weather -------------------------------------------------------------------------------------------

def daylight(sunrise, sunset):
    try:
        a = datetime.strptime(sunrise, "%H:%M")
        b = datetime.strptime(sunset, "%H:%M")
    except (TypeError, ValueError):
        return "—"
    minutes = int((b - a).total_seconds() // 60)
    return f"{minutes // 60} h {minutes % 60:02} min"


class WeatherView(Painted):
    NAME, INDEX, TITLE = "weather", "01", "ATMOSPHERE"

    def sub(self):
        d = self.shared.data.get("weather")
        return d["place"] if d else ""

    def changed(self, what):
        if what == "weather":
            self.update()

    def draw(self, p, w, h):
        d = self.shared.data.get("weather")
        if not d:
            text(p, 0, 30, "ACQUIRING…", font(15, 600, 2.6), C["muted"])
            return
        left = min(440, w * 0.32)
        # now
        icon(p, 0, 4, d["icon"], 1.45)
        tw = text(p, 122, 100, n(d["temp"]), font(104, 500), C["text"])
        text(p, 128 + tw, 58, "°C", font(34, 500), C["cyan2"])
        text(p, 0, 150, d["text"], font(22, 700, 4), C["cyan2"])
        text(p, 0, 176, f"FEELS {n(d.get('feels'), '°')} · H {n(d.get('high'), '°')} · L {n(d.get('low'), '°')}",
             font(13, 400, mono=True), C["soft"])
        updated = time.strftime("%H:%M", time.localtime(d.get("at", time.time())))
        text(p, 0, 198, f"UPDATED {updated} · OPEN-METEO", font(11, 400, mono=True), C["muted"])
        rule(p, 0, 216, left - 20)
        cells = [("Humidity", n(d.get("humidity"), "%")), ("Dew point", n(d.get("dew"), "°", 1)),
                 ("Wind", f"{n(d.get('wind'))} km/h {d.get('wind_dir', '')}"), ("Gusts", n(d.get("gusts"), " km/h")),
                 ("Pressure", n(d.get("pressure"), " hPa")), ("Cloud cover", n(d.get("cloud"), "%")),
                 ("UV index", n(d.get("uv"), "", 1)), ("Visibility", n(d.get("visibility_km"), " km", 1)),
                 ("Rain today", n(d.get("rain_chance"), "%")), ("Daylight", daylight(d.get("sunrise"), d.get("sunset"))),
                 ("Sunrise", d.get("sunrise") or "—"), ("Sunset", d.get("sunset") or "—")]
        row_h = max(52, min(62, (h - 250) / 6))
        for i, (lab, val) in enumerate(cells):
            label_value(p, (i % 2) * (left / 2), 250 + (i // 2) * row_h, lab, val, 19)

        x0 = left + 30
        cw = w - x0
        self._hours(p, d.get("hours48") or [], QRectF(x0, 0, cw, h * 0.44))
        self._days(p, d.get("days") or [], QRectF(x0, h * 0.44 + 30, cw, h - h * 0.44 - 30))

    def _hours(self, p, hours, r):
        head(p, r.left(), r.top() + 14, "NEXT 48 H")
        text(p, r.right(), r.top() + 14, "TEMP ─   FEELS ┄   ▮ RAIN %", font(11, 400, mono=True), C["muted"], "right")
        if len(hours) < 2:
            return
        c = QRectF(r.left(), r.top() + 32, r.width(), r.height() - 32 - 64)
        temps = [x["temp"] for x in hours]
        feels = [x["feels"] for x in hours]
        values = [v for v in temps + feels if v is not None]
        lo, hi = min(values) - 1, max(values) + 1
        step = c.width() / (len(hours) - 1)
        for i, x in enumerate(hours):  # rain chance bars, behind the lines
            bh = (x["rain"] or 0) / 100 * c.height()
            p.fillRect(QRectF(c.left() + i * step - step * 0.35, c.bottom() - bh, step * 0.7, bh), color(C["amber"], 0.28))
        chart(p, c, [(temps, C["cyan"], 0.10, False), (feels, C["cyan2"], 0, True)], lo, hi, unit="°")
        for i, x in enumerate(hours):
            cx = c.left() + i * step
            if x["hour"] == "00" and i:
                p.setPen(QPen(color(C["cyan2"], 0.35), 1, Qt.PenStyle.DashLine))
                p.drawLine(QPointF(cx, c.top()), QPointF(cx, c.bottom()))
                day = datetime.strptime(x["time"][:10], "%Y-%m-%d").strftime("%a").upper()
                text(p, cx + 6, c.top() + 14, day, font(12, 700, 2.4), C["cyan2"])
            if i % 3 == 0:
                text(p, cx, c.bottom() + 16, x["hour"], font(10, 400, mono=True), C["muted"], "center")
                icon(p, cx - 13, c.bottom() + 22, x["icon"], 0.36)
                text(p, cx, c.bottom() + 62, n(x["temp"], "°"), font(10, 400, mono=True), C["soft"], "center")

    def _days(self, p, days, r):
        head(p, r.left(), r.top() + 14, f"{len(days)} DAYS")
        if not days:
            return
        lo = min(d["low"] for d in days)
        hi = max(d["high"] for d in days)
        gap = 10
        cw = (r.width() - gap * (len(days) - 1)) / len(days)
        top, ch = r.top() + 30, r.height() - 30
        today = date.today().isoformat()
        for i, d in enumerate(days):
            x = r.left() + i * (cw + gap)
            card = QRectF(x, top, cw, ch)
            p.fillRect(card, color(C["cyan"], 0.10 if d["date"] == today else 0.04))
            if d["date"] == today:
                p.setPen(QPen(color(C["cyan"], 0.5), 1))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(card.adjusted(0.5, 0.5, -0.5, -0.5))
            cx = x + cw / 2
            text(p, cx, top + 26, "TODAY" if d["date"] == today else d["weekday"], font(17, 700, 2.4), C["cyan2"], "center")
            text(p, cx, top + 44, d["date"][5:].replace("-", "."), font(11, 400, mono=True), C["muted"], "center")
            s = min(0.75, cw / 110)
            icon(p, cx - 37 * s, top + 52, d["icon"], s)
            label = d["text"] if len(d["text"]) <= 14 else d["text"][:13] + "…"
            text(p, cx, top + 62 + 74 * s, label, font(11, 600, 1.2), C["soft"], "center")
            y = top + 96 + 74 * s
            text(p, cx, y, n(d["high"], "°"), font(26, 500), C["text"], "center")
            text(p, cx, y + 22, n(d["low"], "°"), font(17, 500), C["muted"], "center")
            bx, bw = x + 14, cw - 28
            p.fillRect(QRectF(bx, y + 34, bw, 4), color(C["cyan"], 0.12))
            a = (d["low"] - lo) / (hi - lo or 1)
            b = (d["high"] - lo) / (hi - lo or 1)
            p.fillRect(QRectF(bx + bw * a, y + 34, max(3, bw * (b - a)), 4), QColor(C["cyan"]))
            rows = [("RAIN", n(d.get("rain"), "%")), ("MM", n(d.get("precip"), "", 1)), ("WIND", n(d.get("wind"))),
                    ("UV", n(d.get("uv"), "", 1)), ("SUN", f"{d.get('sunrise', '')}–{d.get('sunset', '')}")]
            ry = y + 62
            for lab, val in rows:
                if ry > top + ch - 8:
                    break
                text(p, x + 12, ry, lab, font(10, 600, 1.6), C["muted"])
                text(p, x + cw - 12, ry, val, font(10, 400, mono=True), C["amber"] if lab == "RAIN" else C["text"], "right")
                ry += 20


# --- vitals --------------------------------------------------------------------------------------------

class VitalsView(Painted):
    NAME, INDEX, TITLE = "vitals", "02", "SYSTEM VITALS"
    got = Signal(object)

    def __init__(self, shared):
        super().__init__(shared)
        self.detail, self.active = None, False
        self.processes = hud_data.Processes()
        self.got.connect(self._got)

    def sub(self):
        return self.shared.data.get("cpu_name", "")

    def opened(self):
        self.active = True

        def run():
            while self.active:
                try:
                    self.got.emit(hud_data.read_vitals_detail(self.processes))
                except Exception as e:
                    self.shared.log(f"vitals detail read failed: {type(e).__name__}: {e}")
                time.sleep(READ_SECONDS)
        threading.Thread(target=run, daemon=True).start()

    def closing(self):
        self.active = False

    def _got(self, detail):
        self.detail = detail
        self.update()

    def changed(self, what):
        if what == "vitals":
            self.update()

    def draw(self, p, w, h):
        d = self.shared.data.get("vitals")
        if not d:
            text(p, 0, 30, "ACQUIRING…", font(15, 600, 2.6), C["muted"])
            return
        gpu = d.get("gpu")
        x = self.detail or {}
        cols = [(d["cpu"] / 100, C["cyan"], f"{d['cpu']:.0f}%", "CPU", self.shared.data.get("cpu_name", "CPU")[:18])]
        if gpu:
            tcol = C["mint"] if gpu["temp"] < 70 else C["amber"] if gpu["temp"] < 85 else C["hot"]
            cols += [(gpu["util"] / 100, C["cyan"], f"{gpu['util']:.0f}%", "GPU", gpu["name"][:18]),
                     (gpu["temp"] / 100, tcol, f"{gpu['temp']:.0f}°", "GPU TEMP", "LIMIT 87°C")]
        ram = d["ram_used"] / d["ram_total"]
        cols.append((ram, load_color(ram), f"{ram * 100:.0f}%", "MEMORY", f"{d['ram_used']:.1f} / {d['ram_total']:.0f} GB"))
        if gpu:
            vram = gpu["mem_used"] / gpu["mem_total"]
            cols.append((vram, load_color(vram), f"{vram * 100:.0f}%", "VRAM",
                         f"{gpu['mem_used']:.1f} / {gpu['mem_total']:.1f} GB"))
        cw = w / len(cols)
        for i, (frac, col, value, label, sub) in enumerate(cols):
            gauge(p, cw * i + cw / 2, 56, frac, col, value, label, sub)

        y = 186
        half = w / 2 - 20
        self._cores(p, x, QRectF(0, y, half, 200))
        self._gpu(p, d, x, QRectF(w / 2 + 20, y, half, 200))
        self._history(p, QRectF(0, y + 236, w, 150))
        self._lists(p, d, x, QRectF(0, y + 420, w, h - y - 420))

    def _cores(self, p, x, r):
        cores = x.get("cores") or []
        freq = x.get("freq")
        info = f"{x.get('physical', '?')} CORES · {x.get('logical', '?')} THREADS"
        if freq:
            info += f" · {freq / 1000:.2f} GHz"
        head(p, r.left(), r.top() + 12, "CORES", r.width())
        text(p, r.right(), r.top() + 12, info, font(11, 400, mono=True), C["muted"], "right")
        if not cores:
            text(p, r.left(), r.top() + 50, "READING…", font(12, 600, 2), C["muted"])
            return
        gap = 6
        bw = (r.width() - gap * (len(cores) - 1)) / len(cores)
        top, bh = r.top() + 40, r.height() - 76
        for i, c in enumerate(cores):
            bx = r.left() + i * (bw + gap)
            p.fillRect(QRectF(bx, top, bw, bh), color(C["cyan"], 0.08))
            fh = bh * c / 100
            p.fillRect(QRectF(bx, top + bh - fh, bw, fh), QColor(load_color(c / 100)))
            text(p, bx + bw / 2, top + bh + 16, f"{c:.0f}", font(10, 400, mono=True), C["soft"], "center")
            text(p, bx + bw / 2, top + bh + 32, str(i), font(9, 400, mono=True), C["dim"], "center")

    def _gpu(self, p, d, x, r):
        gpu = d.get("gpu")
        head(p, r.left(), r.top() + 12, "GPU", r.width())
        if not gpu:
            text(p, r.left(), r.top() + 50, "NO NVIDIA GPU", font(12, 600, 2), C["muted"])
            return
        g = x.get("gpu") or {}
        llm = min(d.get("llm_vram", 0), gpu["mem_used"])
        rows = [("NAME", gpu["name"]), ("LOAD", f"{gpu['util']:.0f}%   {gpu['temp']:.0f}°C"),
                ("POWER", f"{n(g.get('power'), ' W', 1)}" + (f" / {g['power_limit']:.0f} W" if g.get("power_limit") else "")),
                ("CLOCK", f"{n(g.get('clock'), ' MHz')} / {n(g.get('clock_max'), ' MHz')}"),
                ("MEMORY CLOCK", f"{n(g.get('mem_clock'), ' MHz')} / {n(g.get('mem_clock_max'), ' MHz')}"),
                ("STATE", f"{g.get('pstate', '—')} · DRIVER {g.get('driver', '—')}"),
                ("VRAM", f"UMAY LLM {llm:.1f} · OTHER {max(0.0, gpu['mem_used'] - llm):.1f} · FREE "
                         f"{gpu['mem_total'] - gpu['mem_used']:.1f} GB")]
        for i, (lab, val) in enumerate(rows):
            yy = r.top() + 42 + i * 22
            text(p, r.left(), yy, lab, font(11, 600, 2), C["muted"])
            text(p, r.left() + 130, yy, val, font(12, 400, mono=True), C["text"])
        yy = r.top() + 42 + len(rows) * 22 - 8
        p.fillRect(QRectF(r.left() + 130, yy, r.width() - 130, 5), color(C["cyan"], 0.12))
        seg = (r.width() - 130) / gpu["mem_total"]
        p.fillRect(QRectF(r.left() + 130, yy, seg * llm, 5), QColor(C["cyan"]))
        p.fillRect(QRectF(r.left() + 130 + seg * llm, yy, seg * max(0.0, gpu["mem_used"] - llm), 5), color(C["cyan2"], 0.45))

    def _history(self, p, r):
        hist = list(self.shared.history)
        head(p, r.left(), r.top() + 12, "LAST 5 MIN")
        gap = 22
        cw = (r.width() - 3 * gap) / 4
        boxes = [
            ("CPU · GPU %", [([s["cpu"] for s in hist], C["cyan"], 0.12, False),
                             ([s["gpu"] for s in hist], C["mint"], 0, False)], 0, 100, "%"),
            ("GPU TEMP °C", [([s["temp"] for s in hist], C["amber"], 0.10, False)], None, None, "°"),
            ("MEMORY GB", [([s["ram"] for s in hist], C["cyan2"], 0.10, False)], None, None, ""),
            ("NETWORK MB/s ↓ ↑", [([None if s["down"] is None else s["down"] / 2 ** 20 for s in hist], C["cyan"], 0.10, False),
                                  ([None if s["up"] is None else s["up"] / 2 ** 20 for s in hist], C["hot"], 0, False)],
             0, None, ""),
        ]
        for i, (label, series, lo, hi, unit) in enumerate(boxes):
            box = QRectF(r.left() + i * (cw + gap), r.top() + 30, cw, r.height() - 30)
            chart(p, box.adjusted(0, 18, 0, 0), series, lo, hi, unit=unit)
            text(p, box.left() + 2, box.top() + 12, label, font(11, 600, 2), C["muted"])

    def _lists(self, p, d, x, r):
        procs = x.get("processes") or {}
        gap = 30
        cw = (r.width() - 2 * gap) / 3
        lists = [("TOP CPU", [(f"{q['name']}" + (f" ×{q['count']}" if q["count"] > 1 else ""), f"{q['cpu']:.1f}%")
                              for q in procs.get("cpu", [])]),
                 ("TOP MEMORY", [(f"{q['name']}" + (f" ×{q['count']}" if q["count"] > 1 else ""),
                                  f"{q['ram'] / 2 ** 30:.2f} GB") for q in procs.get("ram", [])])]
        rows = max(1, int((r.height() - 30) // 22))
        for i, (title, items) in enumerate(lists):
            bx = r.left() + i * (cw + gap)
            head(p, bx, r.top() + 12, title, cw)
            if not items:
                text(p, bx, r.top() + 44, "READING…", font(12, 600, 2), C["muted"])
            for j, (name, value) in enumerate(items[:rows]):
                yy = r.top() + 40 + j * 22
                name = name if len(name) <= 34 else name[:33] + "…"
                text(p, bx, yy, name, font(12, 400, mono=True), C["text"])
                text(p, bx + cw, yy, value, font(12, 400, mono=True), C["cyan2"], "right")
        bx = r.left() + 2 * (cw + gap)
        head(p, bx, r.top() + 12, "SYSTEM", cw)
        up = int(d["uptime"])
        battery = x.get("battery")
        bat = "—" if not battery else f"{battery['percent']:.0f}% · " + ("PLUGGED IN" if battery["plugged"] else
                                                                          f"{(battery['seconds'] or 0) // 60} MIN LEFT")
        rows_ = [("UPTIME", f"{up // 86400}d {up % 86400 // 3600:02}:{up % 3600 // 60:02}:{up % 60:02}"),
                 ("RAM FREE", n(x.get("ram_available"), " GB", 1)),
                 ("SWAP", f"{n(x.get('swap_used'), '', 1)} / {n(x.get('swap_total'), ' GB', 1)}"),
                 ("BATTERY", bat), ("PROCESSES", str(procs.get("count", "—"))),
                 ("NET ↓", rate(d.get("down"))), ("NET ↑", rate(d.get("up")))]
        for j, (lab, val) in enumerate(rows_[:rows]):
            yy = r.top() + 40 + j * 22
            text(p, bx, yy, lab, font(11, 600, 2), C["muted"])
            text(p, bx + cw, yy, val, font(12, 400, mono=True), C["text"], "right")


# --- calendar ------------------------------------------------------------------------------------------

MONTHS = ("JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY", "AUGUST", "SEPTEMBER", "OCTOBER",
          "NOVEMBER", "DECEMBER")


class CalendarView(Painted):
    NAME, INDEX, TITLE = "calendar", "04", "CHRONOLOG"
    got = Signal(object)

    def __init__(self, shared):
        super().__init__(shared)
        today = date.today()
        self.year, self.month, self.selected = today.year, today.month, today
        self.data, self.buttons, self.cells = None, {}, {}
        self.got.connect(self._got)
        self.refresh = QTimer(self)
        self.refresh.timeout.connect(self.load)

    def sub(self):
        return f"{MONTHS[self.month - 1]} {self.year}"

    def opened(self):
        self.load()
        self.refresh.start(30000)  # an alarm Umay just set shows up

    def closing(self):
        self.refresh.stop()

    def load(self):
        year, month = self.year, self.month

        def run():
            try:
                self.got.emit(hud_data.read_alarms(year, month))
            except Exception as e:
                self.shared.log(f"calendar detail read failed: {type(e).__name__}: {e}")
        threading.Thread(target=run, daemon=True).start()

    def _got(self, data):
        if (data["year"], data["month"]) == (self.year, self.month):
            self.data = data
            self.update()

    def go(self, months):
        total = self.year * 12 + self.month - 1 + months
        self.year, self.month = total // 12, total % 12 + 1
        self.data = None
        self.load()
        self.update()
        self.parent() and self.parent().update()  # the header shows the month

    def mousePressEvent(self, ev):
        pos = ev.position()
        for name, r in self.buttons.items():
            if r.contains(pos):
                if name == "today":
                    today = date.today()
                    self.selected = today
                    self.go((today.year - self.year) * 12 + today.month - self.month)
                else:
                    self.go(-1 if name == "prev" else 1)
                return
        for day, r in self.cells.items():
            if r.contains(pos):
                self.selected = date(self.year, self.month, day)
                self.update()
                return

    def draw(self, p, w, h):
        gw = w * 0.64
        text(p, 0, 34, f"{MONTHS[self.month - 1]} {self.year}", font(30, 600, 6), C["text"])
        self.buttons = {}
        bx = gw
        for name, label, bw in (("next", "›", 44), ("prev", "‹", 44), ("today", "TODAY", 92)):
            bx -= bw
            r = QRectF(bx, 6, bw - 8, 34)
            self.buttons[name] = r
            p.setPen(QPen(color(C["cyan"], 0.4), 1))
            p.setBrush(color(C["cyan"], 0.06))
            p.drawRect(r)
            text(p, r.center().x(), r.top() + 24, label, font(18 if len(label) == 1 else 13, 700, 2), C["cyan2"], "center")
        first = date(self.year, self.month, 1)
        lead = first.weekday()
        days = ((first.replace(year=self.year + self.month // 12, month=self.month % 12 + 1)) - first).days
        cw, top = gw / 7, 76
        weeks = (lead + days + 6) // 7
        chh = (h - top - 8) / max(weeks, 5)
        for i, wd in enumerate(("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")):
            text(p, cw * i + 10, top - 12, wd, font(12, 700, 2.4), C["muted"])
        by_day = (self.data or {}).get("by_day", {})
        holidays = (self.data or {}).get("holidays", {})
        today = date.today()
        self.cells = {}
        for d in range(1, days + 1):
            cell = lead + d - 1
            r = QRectF(cw * (cell % 7) + 2, top + chh * (cell // 7) + 2, cw - 4, chh - 4)
            self.cells[d] = r
            this = date(self.year, self.month, d)
            p.fillRect(r, color(C["cyan"], 0.07 if this >= today else 0.045))
            p.setPen(QPen(color(C["cyan"], 0.10), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(r.adjusted(0.5, 0.5, -0.5, -0.5))
            if this == self.selected:
                p.setPen(QPen(QColor(C["cyan"]), 1.5))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(r.adjusted(0.5, 0.5, -0.5, -0.5))
            if this == today:
                p.fillRect(QRectF(r.left() + 6, r.top() + 6, 34, 24), QColor(C["cyan"]))
                text(p, r.left() + 23, r.top() + 24, f"{d:02}", font(15, 400, mono=True), "#000000", "center")
            else:
                text(p, r.left() + 10, r.top() + 24, f"{d:02}", font(15, 400, mono=True),
                     C["soft"] if this >= today else C["dim"])
            rings = by_day.get(d, [])
            days_off = holidays.get(d, [])
            if rings or days_off:
                p.fillRect(QRectF(r.left(), r.top(), 3, r.height()), QColor(C["amber"] if rings else C["mint"]))
            fit = max(0, int((r.height() - 40) // 17))
            lines = [(h_["name"], C["mint"]) for h_ in days_off] + \
                [(f"{ring_['at']:%H:%M} {ring_['name']}", C["amber"]) for ring_ in rings]
            for j, (label, col) in enumerate(lines[:fit]):
                text(p, r.left() + 10, r.top() + 48 + j * 17, label[:int(cw / 7.2)], font(11, 400, mono=True), col)
            if len(lines) > fit:
                text(p, r.right() - 8, r.top() + 24, f"+{len(lines) - fit}", font(11, 600, 1), C["amber"], "right")

        x0, cw2 = gw + 36, w - gw - 36
        y = 34
        sel = self.selected
        head(p, x0, y, f"{sel:%a %d %b %Y}".upper(), cw2)
        y += 30
        this_month = (sel.year, sel.month) == (self.year, self.month)
        rings = by_day.get(sel.day, []) if this_month else []
        days_off = holidays.get(sel.day, []) if this_month else []
        if self.data is None:
            text(p, x0, y + 8, "READING…", font(12, 600, 2), C["muted"])
            y += 30
        elif not rings and not days_off:
            text(p, x0, y + 8, "NOTHING RINGS THIS DAY", font(12, 600, 2), C["muted"])
            y += 30
        for h_ in days_off[:3]:
            text(p, x0, y + 8, h_["country"], font(15, 400, mono=True), C["mint"])
            text(p, x0 + 70, y + 8, h_["name"][:34], font(16, 500), C["text"])
            text(p, x0 + cw2, y + 8, "DAY OFF" if h_["off"] else "", font(10, 600, 1.4), C["muted"], "right")
            y += 26
        for r_ in rings[:8]:
            text(p, x0, y + 8, f"{r_['at']:%H:%M}", font(15, 400, mono=True), C["cyan2"])
            text(p, x0 + 70, y + 8, r_["name"][:34], font(16, 500), C["text"])
            text(p, x0 + cw2, y + 8, r_["repeat"], font(10, 600, 1.4), C["muted"], "right")
            y += 26
        y += 20
        head(p, x0, y, "COMING · 60 DAYS", cw2)
        y += 30
        coming = (self.data or {}).get("coming", [])
        off = (self.data or {}).get("off", [])
        room = int((h - y - (40 + 22 * min(len(off), 4) if off else 0)) // 24)
        if self.data is not None and not coming:
            text(p, x0, y + 8, "NO ALARMS SET · ASK UMAY: \"REMIND ME…\"", font(12, 600, 1.6), C["muted"])
            y += 30
        for r_ in coming[:max(0, room)]:
            when = f"{r_['at']:%a %d %b}".upper() + (f"  {r_['repeat']}" if r_.get("holiday") else f"{r_['at']: %H:%M}")
            text(p, x0, y + 8, when, font(12, 400, mono=True), C["mint"] if r_.get("holiday") else C["cyan2"])
            text(p, x0 + 150, y + 8, r_["name"][:30], font(15, 500), C["text"])
            y += 24
        if off:
            y += 16
            head(p, x0, y, "OFF", cw2)
            y += 26
            for e in off[:4]:
                text(p, x0, y + 6, e["name"][:36], font(14, 500), C["dim"])
                text(p, x0 + cw2, y + 6, e["repeat"], font(10, 600, 1.4), C["dim"], "right")
                y += 22
