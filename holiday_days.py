"""The holidays of Türkiye and the UK for the HUD's calendar and Umay's morning greeting: the days off from the
`holidays` package (offline; its tables hold the Islamic holidays' dates, the half days before them too), plus
the days everyone marks that aren't days off (NOTABLE: 10 November, Mother's Day, Bonfire Night...).

.env: UMAY_HOLIDAYS (TR,GB: the countries, as the `holidays` package names them), UMAY_HOLIDAYS_UK_PART (ENG:
England's bank holidays; SCT, WLS or NIR).
"""
import os
from datetime import date, timedelta
from functools import lru_cache

COUNTRIES = "TR,GB"
UK_PART = "ENG"
TAGS = {"GB": "UK"}  # what the HUD writes


def _nth_sunday(year, month, n):
    first = date(year, month, 1)
    return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))


def _mothering_sunday(year):
    from dateutil.easter import easter
    return easter(year) - timedelta(days=21)


# Marked days that aren't days off: (name, the day in a given year).
NOTABLE = {
    "TR": [
        ("Çanakkale Victory Day", lambda y: date(y, 3, 18)),
        ("Mother's Day", lambda y: _nth_sunday(y, 5, 2)),
        ("Father's Day", lambda y: _nth_sunday(y, 6, 3)),
        ("Atatürk Memorial Day", lambda y: date(y, 11, 10)),
        ("Teachers' Day", lambda y: date(y, 11, 24)),
    ],
    "GB": [
        ("Valentine's Day", lambda y: date(y, 2, 14)),
        ("Mothering Sunday", _mothering_sunday),
        ("St George's Day", lambda y: date(y, 4, 23)),
        ("Father's Day", lambda y: _nth_sunday(y, 6, 3)),
        ("Halloween", lambda y: date(y, 10, 31)),
        ("Bonfire Night", lambda y: date(y, 11, 5)),
        ("Remembrance Sunday", lambda y: _nth_sunday(y, 11, 2)),
        ("Christmas Eve", lambda y: date(y, 12, 24)),
        ("New Year's Eve", lambda y: date(y, 12, 31)),
    ],
}


def countries():
    raw = os.environ.get("UMAY_HOLIDAYS", "").strip() or COUNTRIES
    return [c.strip().upper() for c in raw.split(",") if c.strip()]


def tag(country):
    return TAGS.get(country, country)


@lru_cache(maxsize=32)
def _year(country, year, uk_part):
    """[(day, name, off)] of one country's year."""
    import holidays

    out = []
    try:
        kwargs = {"subdiv": uk_part} if country == "GB" and uk_part else {}
        try:
            days = holidays.country_holidays(country, years=year, language="en_US",
                                             categories=("public", "half_day"), **kwargs)
        except (NotImplementedError, ValueError):  # a country without half days or English names
            days = holidays.country_holidays(country, years=year, **kwargs)
        out = [(day, name, True) for day, name in days.items()]
    except Exception as e:
        print(f"Holidays: {country} {year}: {e!r}")
    taken = {day for day, _, _ in out}
    for name, when in NOTABLE.get(country, []):
        day = when(year)
        if day not in taken:
            out.append((day, name, False))
    return sorted(out)


def between(start, end):
    """{day: [{"name", "country", "off"}]} from `start` to `end` (dates, both counted), every country."""
    uk_part = (os.environ.get("UMAY_HOLIDAYS_UK_PART", "").strip() or UK_PART).upper()
    found = {}
    for country in countries():
        for year in range(start.year, end.year + 1):
            for day, name, off in _year(country, year, uk_part):
                if start <= day <= end:
                    found.setdefault(day, []).append({"name": name, "country": tag(country), "off": off})
    return found


def coming(today, days=31):
    """[(day, {"name", "country", "off"})] from today on, soonest first."""
    return [(day, h) for day, hs in sorted(between(today, today + timedelta(days=days)).items()) for h in hs]


def describe(items):
    """"Republic Day (TR)", for Umay."""
    return ", ".join(f"{h['name']} ({h['country']}{'' if h['off'] else ', not a day off'})" for h in items)
