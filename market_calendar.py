"""Exchange-calendar awareness: is each instrument's market open, closed, or on holiday?

Uses the `exchange_calendars` library (chosen over pandas_market_calendars: one library with minute-level
sessions, holidays and early closes for CME Globex (CMES), ICE (IEPA), Euronext Amsterdam (XAMS) and NYSE (XNYS),
and it needs no pandas-version pinning of its own). Each indicator in config/market_symbols.json names a calendar
code. Two of those are PROXIES, not the real venue: Brent uses IEPA (ICE US) and TTF uses XAMS (Euronext
Amsterdam) because the library has no calendar for ICE Futures Europe or ICE Endex. Holiday accuracy for futures
venues is unverified; treat a "holiday" or "closed" label near a holiday as indicative.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd

import market_data

log = logging.getLogger("market_calendar")
_CALENDARS = {}


@dataclass
class MarketStatus:
    status: str                # "open", "closed" (weekend, outside hours) or "holiday" (weekday with no session)
    last_close: str | None     # UTC ISO of the most recent close, when known
    next_open: str | None      # UTC ISO of the next open, when known
    calendar: str | None
    note: str = ""


def get_calendar(code):
    if code not in _CALENDARS:
        import exchange_calendars as xc

        end = pd.Timestamp.now().normalize() + pd.Timedelta(days=730)  # the library default stops one year out
        _CALENDARS[code] = xc.get_calendar(code, end=end)
    return _CALENDARS[code]


def _iso(ts):
    return ts.tz_convert("UTC").isoformat(timespec="seconds")


def _indicator(indicator):
    return indicator if isinstance(indicator, dict) else market_data.load_indicators()[indicator]


def market_status(indicator, now=None):
    """Status of one indicator (key or config entry) at `now` (aware datetime, default now)."""
    cfg = _indicator(indicator)
    now = now or datetime.now(timezone.utc)
    code = cfg.get("calendar")
    if code:
        try:
            cal = get_calendar(code)
            ts = pd.Timestamp(now).tz_convert("UTC").floor("min")
            if cal.is_open_on_minute(ts):
                return MarketStatus("open", None, None, code)
            local = ts.tz_convert(cal.tz)
            holiday = local.weekday() < 5 and not cal.is_session(pd.Timestamp(local.date()))
            return MarketStatus("holiday" if holiday else "closed", _iso(cal.previous_close(ts)),
                                _iso(cal.next_open(ts)), code)
        except Exception as exc:  # outside the calendar's range, unknown code, library change
            log.warning("calendar %s unusable (%s); falling back to the approximate window", code, exc)
            note = f"calendar {code} unavailable, approximate hours used"
    else:
        note = "no calendar configured, approximate hours used"
    is_open = market_data.in_window(cfg["window"], now)
    return MarketStatus("open" if is_open else "closed", None, None, code, note)


def status_line(now=None, keys=None):
    """One deterministic line for the brief prompt, for example
    'Market status at 25 Oct 2026 14:00 UTC: closed - BRENT (last close 23 Oct), WTI (last close 23 Oct); open - none.'"""
    now = now or datetime.now(timezone.utc)
    indicators = market_data.load_indicators()
    groups = {"open": [], "closed": [], "holiday": []}
    for key in keys or indicators:
        s = market_status(indicators[key], now)
        suffix = f" (last close {datetime.fromisoformat(s.last_close):%d %b})" if s.status != "open" and s.last_close else ""
        groups[s.status].append(f"{key}{suffix}")
    parts = [f"{name} - {', '.join(items) if items else 'none'}" for name, items in groups.items()]
    return f"Market status at {now:%d %b %Y %H:%M} UTC: " + "; ".join(parts) + "."
