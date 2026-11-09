import calendar
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

import paths

CONFIG_PATH = paths.app_dir() / "config.json"

_reload = threading.Event()


def request_reload():
    """Makes the supervisor re-read config.json now instead of at its next interval."""
    _reload.set()


def wait_for_reload(timeout):
    """Sleeps up to `timeout` seconds, returning early if request_reload() is called."""
    _reload.wait(timeout)
    _reload.clear()


@dataclass
class DateParts:
    year: Optional[int] = None
    month: Optional[int] = None
    day: Optional[int] = None
    hour: Optional[int] = None
    minute: Optional[int] = None

    def is_empty(self) -> bool:
        return all(v is None for v in (self.year, self.month, self.day, self.hour, self.minute))

    def to_datetime(self, reference: datetime) -> datetime:
        return datetime(
            self.year if self.year is not None else reference.year,
            self.month if self.month is not None else reference.month,
            self.day if self.day is not None else reference.day,
            self.hour if self.hour is not None else reference.hour,
            self.minute if self.minute is not None else reference.minute,
        )


@dataclass
class Config:
    name: str
    active: bool
    loop: bool
    start_date: datetime
    frequency: DateParts
    end_date: datetime
    sound: str


def add_interval(base: datetime, freq: DateParts) -> datetime:
    year = freq.year or 0
    month = freq.month or 0
    day = freq.day or 0
    hour = freq.hour or 0
    minute = freq.minute or 0

    total_months = base.month - 1 + month + year * 12
    result_year = base.year + total_months // 12
    result_month = total_months % 12 + 1
    result_day = min(base.day, calendar.monthrange(result_year, result_month)[1])

    result = base.replace(year=result_year, month=result_month, day=result_day)
    result += timedelta(days=day, hours=hour, minutes=minute)
    return result


def parse_event(raw_event: dict, now: datetime) -> Config:
    name = raw_event["name"]
    active = raw_event["active"]
    loop = raw_event["loop"]
    sound = str(paths.resource_dir() / raw_event["sound"])

    start_date = DateParts(**raw_event["start_date"]).to_datetime(now)


    end_parts = DateParts(**raw_event["end_date"])
    end_date = datetime.max if end_parts.is_empty() else end_parts.to_datetime(now)

    frequency = DateParts(**raw_event["frequency"])

    return Config(
        name=name,
        active=active,
        loop=loop,
        start_date=start_date,
        frequency=frequency,
        end_date=end_date,
        sound=sound,
    )


def load_raw_events(path: Path = CONFIG_PATH) -> List[dict]:
    """The events in config.json; none if the file doesn't exist yet (reminders create it)."""
    if not Path(path).exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return raw["events"]


def load_config(path: Path = CONFIG_PATH) -> List[Config]:
    now = datetime.now()
    return [parse_event(raw_event, now) for raw_event in load_raw_events(path)]


if __name__ == "__main__":
    for config in load_config():
        print(config)
        if config.loop:
            print("Next run:", add_interval(config.start_date, config.frequency))
