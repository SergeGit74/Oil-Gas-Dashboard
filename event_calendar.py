"""Scheduled-event calendar: manual entries from config/events.yaml plus EIA release dates read from EIA's
published schedule pages. The brief's "Watch today" section is built only from the `events` table.

Nothing here is guessed: if an EIA page cannot be parsed (layout change, missing standard-release sentence) the
loader raises, the job logs `degraded`, and the rows already in the table are left untouched.
"""
import logging
import re
from datetime import date, datetime, timedelta

import requests
import yaml

import config
import storage

log = logging.getLogger("event_calendar")

EVENTS_YAML = config.BASE_DIR / "config" / "events.yaml"
EIA_TZ = "America/New_York"
WPSR_URL = "https://www.eia.gov/petroleum/supply/weekly/schedule.php"
NGS_URL = "https://ir.eia.gov/ngs/schedule.html"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; oil-gas-dashboard event loader)"}
HORIZON_DAYS = 60

_DATE = r"([A-Z][a-z]+ \d{1,2}, \d{4})"
_DAY = r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
_TIME = r"(\d{1,2}:\d{2} [ap]\.m\.)"
_WEEKDAYS = {n: i for i, n in enumerate(["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"])}


class ScheduleParseError(Exception):
    pass


def _text(html):
    """Strip scripts, styles and tags from HTML and collapse whitespace."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"(?s)<(script|style).*?</\1>", " ", html))).strip()


def _parse_date(s):
    """Parse a 'Month D, YYYY' date."""
    return datetime.strptime(s, "%B %d, %Y").date()


def _parse_time(s):
    """Convert '10:30 a.m.' to 24-hour 'HH:MM'."""
    return datetime.strptime(s.replace(".", "").upper(), "%I:%M %p").strftime("%H:%M")


def _fetch(url):
    """Download a schedule page and return its visible text."""
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return _text(resp.text)


def _standard(text, what):
    """Read the page's own 'standard release time and day' sentence; raise ScheduleParseError if it is absent."""
    m = re.search(r"standard release time and day of the week will be at " + _TIME + r" eastern time on (\w+?)s?\b", text)
    if not m or m.group(2).rstrip("s") not in _WEEKDAYS:
        raise ScheduleParseError(f"{what}: standard release sentence not found")
    return _parse_time(m.group(1)), _WEEKDAYS[m.group(2).rstrip("s")]


# ---- EIA Weekly Petroleum Status Report ------------------------------------

def parse_wpsr(text, today, horizon=HORIZON_DAYS):
    """Standard day and time come from the page's own sentence; its exception table moves a week's release to an
    alternate date. A Wednesday is skipped when the week it reports (ending the Friday before) has an exception."""
    std_time, std_weekday = _standard(text, "WPSR")
    rows = re.findall(_DATE + " " + _DATE + " " + _DAY + " " + _TIME, text)
    if not rows:
        raise ScheduleParseError("WPSR: exception table not found")
    moved_weeks = {_parse_date(week_end) for week_end, *_ in rows}
    end = today + timedelta(days=horizon)
    events = []
    day = today
    while day <= end:
        if day.weekday() == std_weekday and (day - timedelta(days=5)) not in moved_weeks:
            events.append((day, std_time))
        day += timedelta(days=1)
    for _week_end, alternate, _weekday, time in rows:
        d = _parse_date(alternate)
        if today <= d <= end:
            events.append((d, _parse_time(time)))
    return [{"date": d.isoformat(), "time": t, "tz": EIA_TZ, "title": "EIA Weekly Petroleum Status Report",
             "category": "EIA release", "source_url": WPSR_URL} for d, t in sorted(set(events))]


# ---- EIA Weekly Natural Gas Storage Report ---------------------------------

def parse_ngs(text, today, horizon=HORIZON_DAYS):
    """Standard day and time come from the page's sentence. An exception row gives the alternate release date; it
    displaces the standard release in the same Monday-to-Sunday week."""
    std_time, std_weekday = _standard(text, "NGS")
    table = text.split("Holiday", 1)[-1]
    rows = re.findall(_DATE + r"(?: - \(Updated\))? " + _DAY + " " + _TIME, table)
    if not rows:
        raise ScheduleParseError("NGS: exception table not found")
    end = today + timedelta(days=horizon)
    displaced = set()
    exceptions = []
    for d_str, _weekday, time in rows:
        d = _parse_date(d_str)
        displaced.add(d - timedelta(days=d.weekday()) + timedelta(days=std_weekday))
        if today <= d <= end:
            exceptions.append((d, _parse_time(time)))
    events = list(exceptions)
    day = today
    while day <= end:
        if day.weekday() == std_weekday and day not in displaced:
            events.append((day, std_time))
        day += timedelta(days=1)
    return [{"date": d.isoformat(), "time": t, "tz": EIA_TZ, "title": "EIA Weekly Natural Gas Storage Report",
             "category": "EIA release", "source_url": NGS_URL} for d, t in sorted(set(events))]


# ---- manual entries --------------------------------------------------------

def load_yaml_events(path=EVENTS_YAML):
    """Read the manual events from config/events.yaml as event dicts."""
    if not path.exists():
        return []
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out = []
    for item in doc.get("events") or []:
        d = item["date"]
        d = d.isoformat() if isinstance(d, (date, datetime)) else str(d)
        time = item.get("time")
        out.append({"date": d, "time": str(time) if time else None, "tz": item.get("tz"),
                    "title": str(item["title"]), "category": item.get("category"),
                    "source_url": item.get("source_url"), "source": "manual"})
    return out


# ---- loading ---------------------------------------------------------------

def load_events(today=None, fetch=_fetch):
    """Load manual entries and EIA schedules into the events table. Returns {source: count} and a list of errors."""
    today = today or date.today()
    counts, errors = {}, []
    manual = load_yaml_events()
    storage.upsert_events(manual)
    counts["manual"] = len(manual)
    for source, url, parser in (("eia_wpsr", WPSR_URL, parse_wpsr), ("eia_ngs", NGS_URL, parse_ngs)):
        try:
            rows = parser(fetch(url), today)
        except (requests.RequestException, ScheduleParseError) as exc:
            errors.append(f"{source}: {exc}")
            continue
        storage.replace_future_events(source, today.isoformat(), rows)
        counts[source] = len(rows)
    return counts, errors


# ---- reading ---------------------------------------------------------------

def format_event(e):
    """One line for an event: date, time and timezone (or 'time not stated'), title and category."""
    when = f"{e['date']} {e['time']} {e['tz']}" if e["time"] else f"{e['date']} (time not stated)"
    return f"{when} - {e['title']}" + (f" [{e['category']}]" if e["category"] else "")


def events_text(events):
    """The events as newline-separated lines (used in the brief prompt)."""
    return "\n".join(format_event(e) for e in events)


def brief_events(today, days_ahead=3):
    """Events for the brief's Watch section: today through `days_ahead` days out, from the table only."""
    return storage.events_between(today.isoformat(), (today + timedelta(days=days_ahead)).isoformat())
