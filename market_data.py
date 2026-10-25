"""Market prices: providers, quota manager, trading windows and polling-rate planning.

Providers are tried in the order of config.PRICE_PROVIDERS. Every provider implements
fetch_quotes(indicators) -> list[Quote] (indicators maps key -> its config entry) and records per-indicator
failures in `last_errors`. Nothing here writes to the database except the quota manager, and nothing here is
called from the dashboard.
"""
import json
import logging
import math
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

import config
import data_sources
import storage

log = logging.getLogger("market_data")

TWELVE_DATA_URL = "https://api.twelvedata.com/quote"
# Unofficial, undocumented Yahoo Finance endpoint. It has no published terms or limits and can change or stop
# without notice. It is off unless ENABLE_YAHOO_FALLBACK=true; review Yahoo's terms before relying on it.
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; oil-gas-dashboard price poller)"}
HTTP_TIMEOUT = 15
_DAYS = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}


@dataclass
class Quote:
    indicator_key: str
    price: float
    prev_close: float | None
    change_pct: float | None
    as_of: str               # UTC ISO time of the quote itself
    provider: str
    delayed_minutes: int | None
    currency: str | None


# ---- configuration --------------------------------------------------------

def load_indicators(path=None):
    with open(path or config.MARKET_SYMBOLS_PATH, encoding="utf-8") as fh:
        return json.load(fh)["indicators"]


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _change_pct(price, prev):
    return round((price - prev) / prev * 100, 4) if prev else None


def _valid(price):
    return isinstance(price, (int, float)) and math.isfinite(price) and price > 0


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _make_quote(key, cfg, provider, price, prev, as_of_dt, delayed=None, currency=None):
    price, prev = _num(price), _num(prev)
    if not _valid(price) or as_of_dt is None:
        return None
    if prev is not None and not _valid(prev):
        prev = None
    return Quote(key, price, prev, _change_pct(price, prev), _iso(as_of_dt), provider, delayed,
                 currency or cfg.get("currency"))


# ---- trading windows ------------------------------------------------------

def _week_minute(spec):
    day, hhmm = spec.split()
    h, m = hhmm.split(":")
    return _DAYS[day] * 1440 + int(h) * 60 + int(m)


def in_window(window, now):
    """True when `now` falls inside the instrument's approximate trading session (see market_symbols.json)."""
    local = now.astimezone(ZoneInfo(window["tz"]))
    if window["kind"] == "weekly":
        t = local.weekday() * 1440 + local.hour * 60 + local.minute
        opens, closes = _week_minute(window["open"]), _week_minute(window["close"])
        return opens <= t < closes if opens < closes else (t >= opens or t < closes)
    if window["kind"] == "daily":
        if local.weekday() not in {_DAYS[d] for d in window["days"]}:
            return False
        hh, mm = window["start"].split(":")
        start = int(hh) * 60 + int(mm)
        hh, mm = window["end"].split(":")
        return start <= local.hour * 60 + local.minute < int(hh) * 60 + int(mm)
    raise ValueError(f"unknown window kind: {window['kind']}")


# ---- providers ------------------------------------------------------------

class TwelveDataProvider:
    name = "twelvedata"

    def __init__(self, api_key):
        self.api_key = api_key
        self.last_errors = {}

    def supports(self, key, cfg):
        return bool(cfg["symbols"].get(self.name))

    def fetch_quotes(self, indicators):
        """One request for all symbols; the provider charges one credit per symbol (documented)."""
        self.last_errors = {}
        by_symbol = {cfg["symbols"][self.name]: (key, cfg) for key, cfg in indicators.items()}
        try:
            resp = requests.get(TWELVE_DATA_URL, params={"symbol": ",".join(by_symbol), "apikey": self.api_key},
                                timeout=HTTP_TIMEOUT)
            body = resp.json()
        except (requests.RequestException, ValueError) as exc:
            self.last_errors = {key: f"request failed: {exc}" for key, _ in by_symbol.values()}
            return []
        if len(by_symbol) == 1:  # a single symbol comes back unwrapped
            body = {next(iter(by_symbol)): body}
        quotes = []
        for symbol, (key, cfg) in by_symbol.items():
            item = body.get(symbol) if isinstance(body, dict) else None
            if not isinstance(item, dict) or item.get("status") == "error" or "close" not in item:
                msg = item.get("message") if isinstance(item, dict) else None
                self.last_errors[key] = f"{symbol}: {msg or 'no data'}"
                continue
            ts = item.get("timestamp")
            as_of = datetime.fromtimestamp(int(ts), timezone.utc) if ts not in (None, "") else None
            quote = _make_quote(key, cfg, self.name, item.get("close"), item.get("previous_close"), as_of,
                                currency=item.get("currency"))
            if quote:
                quotes.append(quote)
            else:
                self.last_errors[key] = f"{symbol}: unusable quote"
        return quotes


class YahooProvider:
    name = "yahoo"

    def __init__(self):
        self.last_errors = {}

    def supports(self, key, cfg):
        return bool(cfg["symbols"].get(self.name))

    def fetch_quotes(self, indicators):
        self.last_errors = {}
        quotes = []
        for key, cfg in indicators.items():
            symbol = cfg["symbols"][self.name]
            try:
                resp = requests.get(YAHOO_CHART_URL.format(symbol=symbol), params={"interval": "1m", "range": "1d"},
                                    headers=YAHOO_HEADERS, timeout=HTTP_TIMEOUT)
                if resp.status_code != 200:
                    self.last_errors[key] = f"{symbol}: HTTP {resp.status_code}"
                    continue
                body = resp.json().get("chart", {})
                result = (body.get("result") or [None])[0]
                if not result:
                    err = body.get("error") or {}
                    self.last_errors[key] = f"{symbol}: {err.get('description') or 'no result'}"
                    continue
                meta = result.get("meta", {})
                ts = meta.get("regularMarketTime")
                as_of = datetime.fromtimestamp(int(ts), timezone.utc) if ts else None
                prev = meta.get("previousClose") or meta.get("chartPreviousClose")
                delayed = meta.get("exchangeDataDelayedBy")
                quote = _make_quote(key, cfg, self.name, meta.get("regularMarketPrice"), prev, as_of,
                                    delayed=delayed if isinstance(delayed, int) else None,
                                    currency=meta.get("currency"))
            except (requests.RequestException, ValueError, TypeError) as exc:
                self.last_errors[key] = f"{symbol}: {exc}"
                continue
            if quote:
                quotes.append(quote)
            else:
                self.last_errors[key] = f"{symbol}: unusable quote"
        return quotes


class EiaProvider:
    """EIA daily spot (Brent, WTI). Updates once a day and lags the calendar, so it is a last-resort fallback."""
    name = "eia"

    def __init__(self):
        self.last_errors = {}

    def supports(self, key, cfg):
        return cfg["symbols"].get(self.name) in data_sources.EIA_SERIES

    def fetch_quotes(self, indicators):
        self.last_errors = {}
        quotes = []
        for key, cfg in indicators.items():
            route, series, freq = data_sources.EIA_SERIES[cfg["symbols"][self.name]]
            try:
                rows = data_sources._eia_series(route, series, freq, length=2)
            except (requests.RequestException, KeyError, ValueError) as exc:
                self.last_errors[key] = f"{series}: {exc}"
                continue
            if not rows:
                self.last_errors[key] = f"{series}: no data"
                continue
            as_of = datetime.fromisoformat(rows[0][0]).replace(tzinfo=timezone.utc)
            quote = _make_quote(key, cfg, self.name, rows[0][1], rows[1][1] if len(rows) > 1 else None, as_of)
            if quote:
                quotes.append(quote)
            else:
                self.last_errors[key] = f"{series}: unusable value"
        return quotes


def build_providers(names=None):
    """Providers enabled by configuration, in the configured order."""
    out = []
    for name in names or config.PRICE_PROVIDERS:
        if name == "twelvedata" and config.get("TWELVE_DATA_API_KEY"):
            out.append(TwelveDataProvider(config.get("TWELVE_DATA_API_KEY")))
        elif name == "yahoo" and config.ENABLE_YAHOO_FALLBACK:
            out.append(YahooProvider())
        elif name == "eia" and config.get("EIA_API_KEY"):
            out.append(EiaProvider())
    return out


# ---- quota ----------------------------------------------------------------

class QuotaManager:
    """Daily cap (a fraction of the provider's limit, kept in the database) and per-minute pacing (in memory)."""

    def __init__(self, limits=None, clock=None, sleep=time.sleep, monotonic=time.monotonic):
        self.limits = limits or config.PROVIDER_LIMITS
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep
        self.monotonic = monotonic
        self._events = {}

    def daily_cap(self, provider):
        return int(self.limits[provider][1] * config.QUOTA_SAFETY_FRACTION)

    def per_minute(self, provider):
        return self.limits[provider][0]

    def reserve(self, provider, credits):
        """Count `credits` against today's total unless that passes the cap. False means refuse the call."""
        return storage.reserve_quota(provider, self.clock().date().isoformat(), credits, self.daily_cap(provider))

    def pace(self, provider, credits):
        """Block until `credits` fit inside the per-minute limit."""
        events = self._events.setdefault(provider, deque())
        limit = self.per_minute(provider)
        while True:
            now = self.monotonic()
            while events and now - events[0][0] >= 60:
                events.popleft()
            if sum(c for _, c in events) + credits <= limit or not events:
                events.append((now, credits))
                return
            self.sleep(max(0.0, 60 - (now - events[0][0])) + 0.05)


# ---- planning -------------------------------------------------------------

def assign_providers(indicators, providers):
    """{key: provider name} using the first provider that supports each indicator."""
    plan = {}
    for key, cfg in indicators.items():
        for provider in providers:
            if provider.supports(key, cfg):
                plan[key] = provider.name
                break
    return plan


def budget_floor_minutes(assignment, limits=None):
    """Shortest polling interval that keeps every provider under its daily cap if it polls all day. The
    interval is lengthened to this floor automatically so the quota cannot be exhausted early."""
    limits = limits or config.PROVIDER_LIMITS
    floor = 0.0
    for provider in set(assignment.values()):
        n = sum(1 for p in assignment.values() if p == provider)
        cap = limits[provider][1] * config.QUOTA_SAFETY_FRACTION
        floor = max(floor, 1440 * n / cap)
    return floor


def interval_minutes(cfg, now, floor=0.0):
    import market_calendar  # imported here: market_calendar itself imports this module

    open_now = market_calendar.market_status(cfg, now).status == "open"  # closed and holiday poll at the slow rate
    base = config.PRICE_POLL_INTERVAL_MIN if open_now else config.PRICE_POLL_OFFHOURS_INTERVAL_MIN
    return max(base, floor)


# ---- derived values (computed at read time, no extra API calls) ------------
DERIVED_MAX_GAP = timedelta(hours=24)


def derived_metrics(ticks):
    """Brent-WTI spread and gold/Brent ratio from the latest ticks ({key: row}). Each entry is None when an input
    is missing or the two quotes are more than 24 hours apart (so it never mixes a stale and a fresh quote). The
    spread also needs both quotes from the same provider, so it compares like with like. No TTF/Henry Hub spread:
    that needs currency and unit conversion, which is not implemented."""
    def pair(a, b, same_provider):
        ta, tb = ticks.get(a), ticks.get(b)
        if not ta or not tb:
            return None
        gap = abs(datetime.fromisoformat(ta["ts"]) - datetime.fromisoformat(tb["ts"]))
        if gap > DERIVED_MAX_GAP or (same_provider and ta["provider"] != tb["provider"]):
            return None
        return ta, tb

    out = {"brent_wti_spread": None, "gold_brent_ratio": None}
    p = pair("BRENT", "WTI", same_provider=True)
    if p:
        out["brent_wti_spread"] = {"value": round(p[0]["price"] - p[1]["price"], 2), "unit": "USD/bbl",
                                   "as_of": min(p[0]["ts"], p[1]["ts"]), "provider": p[0]["provider"]}
    p = pair("GOLD", "BRENT", same_provider=False)
    if p:
        out["gold_brent_ratio"] = {"value": round(p[0]["price"] / p[1]["price"], 2), "unit": "barrels per ounce",
                                   "as_of": min(p[0]["ts"], p[1]["ts"]), "provider": f"{p[0]['provider']}/{p[1]['provider']}"}
    return out


def due_indicators(indicators, last_fetch, now, floor=0.0):
    """Keys whose last successful fetch is older than their (window- and quota-aware) interval."""
    due = []
    for key, cfg in indicators.items():
        last = last_fetch.get(key)
        if last is None:
            due.append(key)
            continue
        age_min = (now - datetime.fromisoformat(last)).total_seconds() / 60
        if age_min >= 0.9 * interval_minutes(cfg, now, floor):  # 0.9: scheduler ticks jitter by a few seconds
            due.append(key)
    return due
