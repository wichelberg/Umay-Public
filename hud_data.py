"""What the HUD's panels and their detail views show: weather, the PC's vitals, its drives, the alarms and
the API credit log. No Qt: each read_* returns a plain dict, and the HUD calls them off its UI thread.

- Weather: Open-Meteo (free, no key). The place is UMAY_WEATHER_LOCATION ("Istanbul", or "41.01,28.98"),
  else where this PC's IP address is; found once and cached by the HUD. Now, 48 hours and 7 days.
- Vitals: psutil (CPU, RAM, network, uptime) and nvidia-smi (GPU); the local model's VRAM from Ollama.
  The detail view adds each core, the programs using most CPU and memory, the GPU's power and clocks.
- Drives: the fixed drives, and what Umay's models take on disk.
- Alarms: config.json's events, the next time each rings (the rule check_event.py rings them by), and
  every ring in a month for the calendar's detail view; Türkiye's and the UK's holidays beside them
  (holiday_days.py).
- Credits: what the paid requests cost today / this week / this month (credit_log's ledger).

.env: UMAY_WEATHER_LOCATION.
"""
import os
import shutil
import subprocess
import time
import winreg
from datetime import datetime, timedelta
from pathlib import Path

import psutil
import requests

import config_loader
import credit_log
import holiday_days
import local_llm

TIMEOUT = 10
# WMO weather codes (Open-Meteo): text and the icon the panel draws.
WEATHER_CODES = {
    0: ("CLEAR", "sun"), 1: ("MAINLY CLEAR", "sun"), 2: ("PARTLY CLOUDY", "cloud_sun"), 3: ("OVERCAST", "cloud"),
    45: ("FOG", "fog"), 48: ("RIME FOG", "fog"), 51: ("LIGHT DRIZZLE", "rain"), 53: ("DRIZZLE", "rain"),
    55: ("HEAVY DRIZZLE", "rain"), 56: ("FREEZING DRIZZLE", "rain"), 57: ("FREEZING DRIZZLE", "rain"),
    61: ("LIGHT RAIN", "rain"), 63: ("RAIN", "rain"), 65: ("HEAVY RAIN", "rain"), 66: ("FREEZING RAIN", "rain"),
    67: ("FREEZING RAIN", "rain"), 71: ("LIGHT SNOW", "snow"), 73: ("SNOW", "snow"), 75: ("HEAVY SNOW", "snow"),
    77: ("SNOW GRAINS", "snow"), 80: ("SHOWERS", "rain"), 81: ("SHOWERS", "rain"), 82: ("HEAVY SHOWERS", "rain"),
    85: ("SNOW SHOWERS", "snow"), 86: ("SNOW SHOWERS", "snow"), 95: ("THUNDERSTORM", "storm"),
    96: ("THUNDERSTORM, HAIL", "storm"), 99: ("THUNDERSTORM, HAIL", "storm"),
}
COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


# --- weather ------------------------------------------------------------------------------------

def find_place(setting=None):
    """{"name", "country", "lat", "lon"} for UMAY_WEATHER_LOCATION, else for this PC's IP address."""
    setting = (setting if setting is not None else os.environ.get("UMAY_WEATHER_LOCATION", "")).strip()
    if setting:
        parts = [p.strip() for p in setting.split(",")]
        try:
            lat, lon = float(parts[0]), float(parts[1])
            return {"name": setting, "country": "", "lat": lat, "lon": lon}
        except (ValueError, IndexError):
            pass
        found = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                             params={"name": setting, "count": 1}, timeout=TIMEOUT).json().get("results") or []
        if not found:
            raise ValueError(f"UMAY_WEATHER_LOCATION: no place called {setting!r}")
        r = found[0]
        return {"name": r["name"], "country": r.get("country_code", ""), "lat": r["latitude"], "lon": r["longitude"]}
    # By IP: the ISP's city, which may not be the owner's (UMAY_WEATHER_LOCATION then). ipapi.co was
    # rate limited at the first try (2026-11-02); these two answered.
    try:
        ip = requests.get("https://ipwho.is/", timeout=TIMEOUT).json()
        if ip.get("success"):
            return {"name": ip["city"], "country": ip.get("country_code", ""), "lat": ip["latitude"],
                    "lon": ip["longitude"]}
    except (requests.RequestException, ValueError, KeyError):
        pass
    ip = requests.get("http://ip-api.com/json/?fields=status,city,countryCode,lat,lon", timeout=TIMEOUT).json()
    if ip.get("status") != "success":
        raise ValueError("couldn't find this PC's place by its IP address")
    return {"name": ip["city"], "country": ip.get("countryCode", ""), "lat": ip["lat"], "lon": ip["lon"]}


def read_weather(place):
    """Now, the next 24 hours and today's range at `place` (find_place)."""
    current = ("temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,pressure_msl,"
               "wind_speed_10m,wind_direction_10m,uv_index,visibility,precipitation,cloud_cover,wind_gusts_10m,"
               "dew_point_2m")
    data = requests.get("https://api.open-meteo.com/v1/forecast", params={
        "latitude": place["lat"], "longitude": place["lon"], "current": current,
        "hourly": ("temperature_2m,apparent_temperature,precipitation_probability,precipitation,weather_code,"
                   "wind_speed_10m,relative_humidity_2m"),
        "daily": ("weather_code,sunrise,sunset,temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
                  "precipitation_sum,uv_index_max,wind_speed_10m_max"),
        "timezone": "auto", "forecast_days": 7,
    }, timeout=TIMEOUT)
    data.raise_for_status()
    return parse_weather(data.json(), place)


def parse_weather(data, place):
    """Open-Meteo's answer -> what the weather panel draws (pure, for the tests)."""
    now = data["current"]
    text, icon = WEATHER_CODES.get(now.get("weather_code"), ("—", "cloud"))
    hours, start = data["hourly"]["time"], now["time"][:13]  # "2026-11-02T11"
    first = next((i for i, t in enumerate(hours) if t[:13] >= start), 0)
    daily = data["daily"]
    wind_dir = now.get("wind_direction_10m")
    hourly = data["hourly"]

    def col(block, name, n):
        return block.get(name) or [None] * n
    n = len(hours)
    feels, precip, codes, winds, hums = (col(hourly, k, n) for k in (
        "apparent_temperature", "precipitation", "weather_code", "wind_speed_10m", "relative_humidity_2m"))
    days = daily.get("time") or []
    d_codes, d_rain, d_precip, d_uv, d_wind = (col(daily, k, len(days)) for k in (
        "weather_code", "precipitation_probability_max", "precipitation_sum", "uv_index_max", "wind_speed_10m_max"))
    return {
        "place": place["name"].upper() + (f" · {place['country']}" if place.get("country") else ""),
        "temp": now["temperature_2m"], "feels": now.get("apparent_temperature"),
        "text": text, "icon": icon,
        "humidity": now.get("relative_humidity_2m"), "pressure": now.get("pressure_msl"),
        "wind": now.get("wind_speed_10m"), "wind_dir": COMPASS[round(wind_dir / 22.5) % 16] if wind_dir is not None else "",
        "uv": now.get("uv_index"), "visibility_km": (now.get("visibility") or 0) / 1000,
        "rain_chance": (daily.get("precipitation_probability_max") or [None])[0],
        "high": daily["temperature_2m_max"][0], "low": daily["temperature_2m_min"][0],
        "sunrise": daily["sunrise"][0][11:16], "sunset": daily["sunset"][0][11:16],
        "hourly": [{"hour": hours[i][11:13], "temp": data["hourly"]["temperature_2m"][i],
                    "rain": data["hourly"]["precipitation_probability"][i] or 0}
                   for i in range(first, min(first + 24, len(hours)))],
        "cloud": now.get("cloud_cover"), "gusts": now.get("wind_gusts_10m"), "dew": now.get("dew_point_2m"),
        "hours48": [{"time": hours[i], "hour": hours[i][11:13], "temp": hourly["temperature_2m"][i],
                     "feels": feels[i], "rain": hourly["precipitation_probability"][i] or 0, "precip": precip[i],
                     "icon": WEATHER_CODES.get(codes[i], ("—", "cloud"))[1], "wind": winds[i], "humidity": hums[i]}
                    for i in range(first, min(first + 48, len(hours)))],
        "days": [{"date": days[i], "weekday": datetime.strptime(days[i], "%Y-%m-%d").strftime("%a").upper(),
                  "text": WEATHER_CODES.get(d_codes[i], ("—", "cloud"))[0],
                  "icon": WEATHER_CODES.get(d_codes[i], ("—", "cloud"))[1],
                  "high": daily["temperature_2m_max"][i], "low": daily["temperature_2m_min"][i],
                  "rain": d_rain[i], "precip": d_precip[i], "uv": d_uv[i], "wind": d_wind[i],
                  "sunrise": daily["sunrise"][i][11:16], "sunset": daily["sunset"][i][11:16]}
                 for i in range(len(days))],
        "at": time.time(),
    }


# --- vitals ---------------------------------------------------------------------------------------

_net = {"at": None, "sent": 0, "recv": 0}


def cpu_name():
    """"Ryzen AI 7 350" from Windows' processor name ("AMD Ryzen AI 7 350 w/ Radeon 860M")."""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
            name = winreg.QueryValueEx(key, "ProcessorNameString")[0]
    except OSError:
        return "CPU"
    name = name.split(" w/ ")[0].replace("(R)", "").replace("(TM)", "").split("@")[0]
    for vendor in ("AMD ", "Intel ", "Core "):
        name = name.replace(vendor, "")
    return " ".join(name.split()).upper()


def read_gpu():
    """{"name", "util", "mem_used", "mem_total", "temp"} of the first NVIDIA GPU, or None."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5,
                             creationflags=subprocess.CREATE_NO_WINDOW).stdout
        name, util, used, total, temp = [v.strip() for v in out.splitlines()[0].split(",")]
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None
    name = name.replace("NVIDIA ", "").replace("GeForce ", "").upper()
    return {"name": name, "util": float(util), "mem_used": float(used) / 1024, "mem_total": float(total) / 1024,
            "temp": float(temp)}


def read_vitals():
    mem = psutil.virtual_memory()
    net = psutil.net_io_counters()
    now = time.monotonic()
    down = up = None
    if _net["at"] is not None and now > _net["at"]:
        down = (net.bytes_recv - _net["recv"]) / (now - _net["at"])
        up = (net.bytes_sent - _net["sent"]) / (now - _net["at"])
    _net.update(at=now, recv=net.bytes_recv, sent=net.bytes_sent)
    return {
        "cpu": psutil.cpu_percent(interval=None), "ram_used": mem.used / 2 ** 30, "ram_total": mem.total / 2 ** 30,
        "gpu": read_gpu(), "llm_vram": local_llm.vram_mb() / 1024 if local_llm.on() else 0.0,
        "down": down, "up": up, "uptime": time.time() - psutil.boot_time(),
    }


def _number(text):
    try:
        return float(text)
    except ValueError:  # "[N/A]" on a laptop GPU
        return None


def read_gpu_detail():
    """The GPU's power, clocks and performance state for the vitals detail view (None without one)."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    fields = "power.draw,power.limit,clocks.gr,clocks.max.gr,clocks.mem,clocks.max.mem,pstate,driver_version"
    try:
        out = subprocess.run([exe, f"--query-gpu={fields}", "--format=csv,noheader,nounits"], capture_output=True,
                             text=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW).stdout
        values = [v.strip() for v in out.splitlines()[0].split(",")]
    except (OSError, subprocess.SubprocessError, IndexError):
        return None
    if len(values) != 8:
        return None
    power, limit, gr, gr_max, mem, mem_max = (_number(v) for v in values[:6])
    return {"power": power, "power_limit": limit, "clock": gr, "clock_max": gr_max, "mem_clock": mem,
            "mem_clock_max": mem_max, "pstate": values[6], "driver": values[7]}


class Processes:
    """The programs using most CPU and memory, by program name (a browser's many processes as one). It
    keeps each process between readings: psutil measures CPU from one reading to the next."""

    def __init__(self):
        self.procs = {}

    def read(self, limit=8):
        cores = psutil.cpu_count() or 1
        seen, by_name = {}, {}
        for p in psutil.process_iter(["name", "memory_info"]):
            if p.pid == 0:  # the System Idle Process: the idle time, not a program
                continue
            proc = self.procs.get(p.pid, p)
            try:
                cpu = proc.cpu_percent(None) / cores
                rss = p.info["memory_info"].rss if p.info["memory_info"] else 0
            except (psutil.Error, OSError):
                continue
            seen[p.pid] = proc
            name = p.info["name"] or "?"
            row = by_name.setdefault(name, {"name": name, "cpu": 0.0, "ram": 0, "count": 0})
            row["cpu"] += cpu
            row["ram"] += rss
            row["count"] += 1
        self.procs = seen
        rows = list(by_name.values())
        return {"cpu": sorted(rows, key=lambda r: -r["cpu"])[:limit],
                "ram": sorted(rows, key=lambda r: -r["ram"])[:limit], "count": len(seen)}


def read_vitals_detail(processes):
    """What the vitals detail view adds to read_vitals(): each core, the clock, swap, battery, the GPU's
    power and clocks and the busiest programs (processes: a Processes kept between readings)."""
    freq = psutil.cpu_freq()
    swap = psutil.swap_memory()
    battery = psutil.sensors_battery()
    mem = psutil.virtual_memory()
    return {
        "cores": psutil.cpu_percent(interval=None, percpu=True),
        "physical": psutil.cpu_count(logical=False), "logical": psutil.cpu_count(),
        "freq": freq.current if freq else None, "freq_max": freq.max if freq else None,
        "ram_available": mem.available / 2 ** 30,
        "swap_used": swap.used / 2 ** 30, "swap_total": swap.total / 2 ** 30,
        "battery": None if battery is None else {"percent": battery.percent, "plugged": battery.power_plugged,
                                                 "seconds": battery.secsleft if battery.secsleft > 0 else None},
        "gpu": read_gpu_detail(), "processes": processes.read(),
    }


# --- drives ---------------------------------------------------------------------------------------

def folder_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def read_drives():
    """The fixed drives (system drive first) and what Umay's models take on disk."""
    system = os.environ.get("SystemDrive", "C:").upper()
    drives = []
    for part in psutil.disk_partitions(all=False):
        if "fixed" not in part.opts:  # no CD drives or USB sticks
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        drives.append({"name": part.device.rstrip("\\").upper(), "used": usage.used / 2 ** 30,
                       "total": usage.total / 2 ** 30, "free": usage.free / 2 ** 30})
    drives.sort(key=lambda d: (d["name"] != system, d["name"]))
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    umay = [("OLLAMA · " + local_llm.base_model().upper(), Path.home() / ".ollama" / "models"),
            ("UMAY VOICE · WHISPER · PIPER · RVC", local / "Umay" / "voice")]
    return {"drives": drives, "umay": [{"name": n, "size": folder_size(p) / 2 ** 30} for n, p in umay if p.is_dir()]}


# --- alarms -----------------------------------------------------------------------------------------

def next_ring(raw_event, now):
    """When an event in config.json rings next (None: off, or it won't ring again)."""
    if not raw_event.get("active"):
        return None
    config = config_loader.parse_event(raw_event, now)
    at = config.start_date
    if config.loop:
        for _ in range(100000):
            if at >= now:
                break
            later = config_loader.add_interval(at, config.frequency)
            if later <= at:
                return None
            at = later
    if at < now or at > config.end_date:
        return None
    return at


def upcoming(raw_events, now, days=31, limit=4):
    """[(datetime, name)] of the next rings within `days`, soonest first."""
    rings = [(at, raw["name"]) for raw in raw_events if (at := next_ring(raw, now)) is not None]
    return sorted((r for r in rings if r[0] <= now + timedelta(days=days)), key=lambda r: r[0])[:limit]


def rings_between(raw_event, start, end, limit=500):
    """Every time an event in config.json rings from `start` to `end` (an off event: none)."""
    if not raw_event.get("active"):
        return []
    config = config_loader.parse_event(raw_event, start)
    at, out = config.start_date, []
    while at <= end and at <= config.end_date and len(out) < limit:
        if at >= start:
            out.append(at)
        if not config.loop:
            break
        later = config_loader.add_interval(at, config.frequency)
        if later <= at:
            break
        at = later
    return out


def repeat_text(raw_event):
    """"ONCE", "EVERY DAY", "EVERY 2 H", "EVERY 1 MONTH"... (the HUD's words, in English)."""
    if not raw_event.get("loop"):
        return "ONCE"
    f = raw_event.get("frequency") or {}
    parts = [(f.get(k) or 0, unit) for k, unit in (("year", "YEAR"), ("month", "MONTH"), ("day", "DAY"),
                                                    ("hour", "H"), ("minute", "MIN"))]
    parts = [(n, unit) for n, unit in parts if n]
    if parts == [(1, "DAY")]:
        return "EVERY DAY"
    return "EVERY " + " ".join(f"{n} {unit}" for n, unit in parts) if parts else "REPEATS"


def read_alarms(year, month, now=None, days=62):
    """The calendar's detail view: every ring in a month by day, the coming rings and the alarms that are off."""
    now = now or datetime.now()
    try:
        raw = config_loader.load_raw_events()
    except Exception:
        raw = []
    first = datetime(year, month, 1)
    last = (first.replace(year=year + month // 12, month=month % 12 + 1) - timedelta(seconds=1))
    by_day = {}
    for event in raw:
        for at in rings_between(event, first, last):
            by_day.setdefault(at.day, []).append({"at": at, "name": event["name"], "repeat": repeat_text(event)})
    for rings in by_day.values():
        rings.sort(key=lambda r: r["at"])
    coming = []
    for event in raw:
        for at in rings_between(event, now, now + timedelta(days=days), limit=60):
            coming.append({"at": at, "name": event["name"], "repeat": repeat_text(event)})
    for day, h in holiday_days.coming(now.date(), days):
        coming.append({"at": datetime.combine(day, datetime.min.time()), "name": h["name"], "repeat": h["country"],
                       "holiday": True})
    coming.sort(key=lambda r: r["at"])
    holidays = {day.day: hs for day, hs in holiday_days.between(first.date(), last.date()).items()}
    return {"year": year, "month": month, "today": now.date(), "by_day": by_day, "coming": coming[:60],
            "holidays": holidays,
            "off": [{"name": e["name"], "repeat": repeat_text(e)} for e in raw if not e.get("active")]}


def read_calendar(now=None):
    now = now or datetime.now()
    try:
        raw = config_loader.load_raw_events()
    except Exception:
        raw = []
    soon = upcoming(raw, now)
    events = [{"at": at, "name": name} for at, name in soon]
    holidays = holiday_days.coming(now.date(), 31)
    events += [{"at": datetime.combine(day, datetime.min.time()), "name": h["name"], "holiday": h["country"]}
               for day, h in holidays]
    events.sort(key=lambda e: e["at"])
    month_end = (now.replace(day=28) + timedelta(days=4)).replace(day=1).date() - timedelta(days=1)
    return {"today": now.date(), "events": events[:4],
            "marked": sorted({at.day for at, _ in soon if (at.year, at.month) == (now.year, now.month)}),
            "holidays": sorted(day.day for day in holiday_days.between(now.date().replace(day=1), month_end))}


# --- credits ----------------------------------------------------------------------------------------

def read_credits(now=None):
    """What the paid requests cost today, in 7 and in 30 days ((cost, entries) each), and the latest one."""
    now = now or time.time()
    ledger = credit_log.Ledger(credit_log.path())
    try:
        t = time.localtime(now)
        midnight = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
        out = {}
        for key, start in (("today", midnight), ("week", now - 7 * 86400), ("month", now - 30 * 86400)):
            by = ledger.totals(start)
            out[key] = (sum(c for _, c in by.values()), sum(n for n, _ in by.values()))
        last = ledger.entries(limit=1)
        out["last"] = None if not last else {"what": last[0].what, "source": last[0].source, "cost": last[0].cost,
                                             "at": last[0].started}
        return out
    finally:
        ledger.close()
