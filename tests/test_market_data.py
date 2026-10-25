from datetime import datetime, timedelta, timezone

import pytest
import requests

import config
import data_sources
import market_data
import storage
from jobs import run_prices
from market_data import QuotaManager

NOW = datetime(2026, 10, 27, 14, 0, tzinfo=timezone.utc)  # a Tuesday, markets open
EPOCH = int(NOW.timestamp())


class FakeResponse:
    def __init__(self, body, status=200):
        """Hold a canned JSON body and HTTP status."""
        self._body, self.status_code = body, status
        self.ok = status < 400

    def json(self):
        """The canned JSON body."""
        return self._body

    def raise_for_status(self):
        """Raise HTTPError for an error status, like requests."""
        if not self.ok:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def indicators(*keys):
    """The configured indicators limited to the given keys."""
    everything = market_data.load_indicators()
    return {k: everything[k] for k in keys}


# ---- configuration --------------------------------------------------------

def test_config_module_still_wins_over_config_directory():
    """'import config' loads config.py, not the config/ data folder."""
    assert hasattr(config, "require_keys") and config.MARKET_SYMBOLS_PATH.exists()


def test_symbol_file_has_the_eight_indicators():
    """The symbol file defines exactly the eight indicators."""
    assert set(market_data.load_indicators()) == {"BRENT", "WTI", "HENRY_HUB", "TTF", "GOLD", "DXY", "SPX", "ULSD"}


def test_missing_paid_keys_do_not_stop_import_or_provider_building():
    """With no keys and Yahoo off, no provider is built and nothing raises."""
    assert market_data.build_providers() == []  # no keys, Yahoo off, EIA key empty


# ---- providers (mocked HTTP) ----------------------------------------------

def test_twelvedata_multi_symbol_parse_and_error(monkeypatch):
    """A multi-symbol Twelve Data response yields quotes and records per-symbol errors."""
    body = {"XAU/USD": {"close": "2650.5", "previous_close": "2640.0", "timestamp": EPOCH, "currency": "USD"},
            "DXY": {"code": 404, "message": "symbol not found", "status": "error"}}
    seen = {}

    def fake_get(url, params=None, **kw):
        """Fake requests.get: capture the params and return the canned response."""
        seen.update(params)
        return FakeResponse(body)

    monkeypatch.setattr(requests, "get", fake_get)
    cfg = {"GOLD": {**indicators("GOLD")["GOLD"]}, "DXY": {**indicators("DXY")["DXY"]}}
    cfg["DXY"]["symbols"] = {**cfg["DXY"]["symbols"], "twelvedata": "DXY"}
    provider = market_data.TwelveDataProvider("k")
    quotes = provider.fetch_quotes(cfg)
    assert seen["symbol"] == "XAU/USD,DXY"
    assert [q.indicator_key for q in quotes] == ["GOLD"]
    assert quotes[0].price == 2650.5 and quotes[0].change_pct == pytest.approx(0.3977, abs=1e-3)
    assert "DXY" in provider.last_errors and "not found" in provider.last_errors["DXY"]


def test_twelvedata_single_symbol_unwrapped(monkeypatch):
    """A single-symbol response (not keyed by symbol) is parsed too."""
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(
        {"close": "2650.5", "previous_close": "2640", "timestamp": EPOCH}))
    quotes = market_data.TwelveDataProvider("k").fetch_quotes(indicators("GOLD"))
    assert quotes[0].price == 2650.5 and quotes[0].provider == "twelvedata"


def test_yahoo_parse_and_http_error(monkeypatch):
    """A Yahoo chart response becomes a quote; an HTTP 429 is recorded as an error."""
    meta = {"regularMarketPrice": 80.5, "chartPreviousClose": 79.0, "regularMarketTime": EPOCH, "currency": "USD"}

    def fake_get(url, **kw):
        """Fake requests.get: a good chart for Brent, HTTP 429 for anything else."""
        if "BZ%3DF" in url or "BZ=F" in url:
            return FakeResponse({"chart": {"result": [{"meta": meta}], "error": None}})
        return FakeResponse({}, status=429)

    monkeypatch.setattr(requests, "get", fake_get)
    provider = market_data.YahooProvider()
    quotes = provider.fetch_quotes(indicators("BRENT", "WTI"))
    assert [q.indicator_key for q in quotes] == ["BRENT"]
    assert quotes[0].price == 80.5 and quotes[0].change_pct == pytest.approx(1.9, abs=0.01)
    assert provider.last_errors["WTI"].endswith("HTTP 429")


def test_eia_parse(monkeypatch):
    """EIA observations become a quote with the previous close."""
    monkeypatch.setattr(data_sources, "_eia_series", lambda *a, **k: [("2026-10-23", 90.0), ("2026-10-22", 88.0)])
    quotes = market_data.EiaProvider().fetch_quotes(indicators("BRENT"))
    assert quotes[0].price == 90.0 and quotes[0].prev_close == 88.0 and quotes[0].as_of.startswith("2026-10-23")


def test_bad_prices_are_rejected(monkeypatch):
    """A zero price is rejected and recorded as unusable."""
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(
        {"chart": {"result": [{"meta": {"regularMarketPrice": 0, "regularMarketTime": EPOCH}}]}}))
    provider = market_data.YahooProvider()
    assert provider.fetch_quotes(indicators("BRENT")) == []
    assert "unusable" in provider.last_errors["BRENT"]


# ---- fallback order -------------------------------------------------------

class FakeProvider:
    def __init__(self, name, covers, fail=()):
        """A fake provider covering some indicators and failing on others."""
        self.name, self.covers, self.fail = name, set(covers), set(fail)
        self.last_errors, self.calls = {}, []

    def supports(self, key, cfg):
        """Whether this fake covers the indicator."""
        return key in self.covers

    def fetch_quotes(self, inds):
        """Record the request and return quotes for every indicator that does not fail."""
        self.calls.append(sorted(inds))
        self.last_errors = {k: "boom" for k in inds if k in self.fail}
        return [market_data.Quote(k, 10.0, 9.0, 11.1, NOW.isoformat(), self.name, None, "USD")
                for k in inds if k not in self.fail]


def big_quota(**limits):
    """A quota manager with generous limits and no real sleeping."""
    return QuotaManager(limits={"a": (100, 1000), "b": (100, 1000), "c": (100, 1000), **limits},
                        clock=lambda: NOW, sleep=lambda s: None)


def test_fallback_order_and_only_missing_indicators_fall_through(db):
    """Each indicator is served by the first provider that returns it; only the missing ones go to the next."""
    a = FakeProvider("a", ["BRENT", "WTI", "GOLD"], fail=["WTI"])
    b = FakeProvider("b", ["WTI", "GOLD", "DXY"])
    c = FakeProvider("c", ["DXY", "BRENT"])
    inds = indicators("BRENT", "WTI", "GOLD", "DXY")
    summary = run_prices.run_prices(now=NOW, providers=[a, b, c], quota=big_quota(), indicators=inds)
    assert a.calls == [["BRENT", "GOLD", "WTI"]]
    assert b.calls == [["DXY", "WTI"]]          # GOLD was already served by a
    assert c.calls == []                          # nothing left for c
    assert summary["stored"] == 4 and summary["missing"] == []
    assert {t["provider"] for t in storage.latest_ticks().values()} == {"a", "b"}
    assert storage.recent_job_runs("fetch_prices", 1)[0]["status"] == "success"


def test_unserved_indicator_is_degraded(db):
    """An indicator nobody serves makes the poll 'degraded'."""
    a = FakeProvider("a", ["BRENT"])
    summary = run_prices.run_prices(now=NOW, providers=[a], quota=big_quota(), indicators=indicators("BRENT", "WTI"))
    assert summary["missing"] == ["WTI"]
    assert storage.recent_job_runs("fetch_prices", 1)[0]["status"] == "degraded"


def test_nothing_due_is_skipped(db):
    """A second poll a minute later has nothing due and is logged as skipped."""
    a = FakeProvider("a", ["BRENT"])
    inds = indicators("BRENT")
    run_prices.run_prices(now=NOW, providers=[a], quota=big_quota(), indicators=inds)
    run_prices.run_prices(now=NOW + timedelta(minutes=1), providers=[a], quota=big_quota(), indicators=inds)
    assert len(a.calls) == 1
    assert storage.recent_job_runs("fetch_prices", 1)[0]["status"] == "skipped"


# ---- quota ----------------------------------------------------------------

def test_quota_refuses_beyond_90_percent_of_daily_limit(db):
    """Credits are refused beyond 90% of the daily limit."""
    quota = QuotaManager(limits={"a": (100, 100)}, clock=lambda: NOW, sleep=lambda s: None)
    assert quota.daily_cap("a") == 90
    assert quota.reserve("a", 60) and quota.reserve("a", 30)
    assert not quota.reserve("a", 1)
    assert storage.quota_used("a", "2026-10-27") == 90


def test_poll_stops_calling_provider_at_the_cap_and_falls_back(db):
    """A provider over its cap is not called and the next provider serves the indicators."""
    a = FakeProvider("a", ["BRENT", "WTI"])
    b = FakeProvider("b", ["BRENT", "WTI"])
    quota = big_quota(a=(100, 2))  # cap = int(2 * 0.9) = 1 credit; a request for 2 symbols is refused
    summary = run_prices.run_prices(now=NOW, providers=[a, b], quota=quota, indicators=indicators("BRENT", "WTI"))
    assert a.calls == [] and len(b.calls) == 1 and summary["stored"] == 2


def test_per_minute_pacing_waits(db):
    """A request that would exceed the per-minute limit waits for the window to clear."""
    sleeps, clock = [], {"t": 1000.0}
    quota = QuotaManager(limits={"a": (8, 800)}, clock=lambda: NOW,
                         sleep=lambda s: (sleeps.append(s), clock.update(t=clock["t"] + s)),
                         monotonic=lambda: clock["t"])
    quota.pace("a", 8)
    quota.pace("a", 3)  # 8 + 3 > 8 within the same minute: must wait for the window to clear
    assert len(sleeps) == 1 and 59 < sleeps[0] <= 61


def test_budget_floor_lengthens_the_interval():
    """Eight symbols on one 800/day provider force a 16-minute polling interval."""
    inds = indicators("BRENT", "WTI", "GOLD", "DXY", "SPX", "HENRY_HUB", "TTF", "ULSD")
    plan = {k: "td" for k in inds}
    floor = market_data.budget_floor_minutes(plan, {"td": (8, 800)})
    assert floor == pytest.approx(1440 * 8 / 720)  # 16 minutes
    assert market_data.interval_minutes(inds["BRENT"], NOW, floor) == pytest.approx(16)  # above the 5 minute default


# ---- trading windows and due logic ----------------------------------------

def test_in_window_weekly_and_daily():
    """The approximate weekly and daily trading windows open and close on the right days."""
    brent, spx = indicators("BRENT")["BRENT"]["window"], indicators("SPX")["SPX"]["window"]
    tue = datetime(2026, 10, 27, 14, 0, tzinfo=timezone.utc)
    sat = datetime(2026, 10, 24, 14, 0, tzinfo=timezone.utc)
    assert market_data.in_window(brent, tue) and market_data.in_window(spx, tue)
    assert not market_data.in_window(brent, sat) and not market_data.in_window(spx, sat)
    assert not market_data.in_window(spx, datetime(2026, 10, 27, 3, 0, tzinfo=timezone.utc))  # before the US open


def test_due_uses_window_specific_interval():
    """An indicator is due after the fast interval while open and the hourly one while closed."""
    inds = indicators("BRENT")
    last = {"BRENT": (NOW - timedelta(minutes=4)).isoformat()}
    assert market_data.due_indicators(inds, last, NOW) == []
    last = {"BRENT": (NOW - timedelta(minutes=6)).isoformat()}
    assert market_data.due_indicators(inds, last, NOW) == ["BRENT"]
    sat = datetime(2026, 10, 24, 14, 0, tzinfo=timezone.utc)  # closed: hourly
    assert market_data.due_indicators(inds, {"BRENT": (sat - timedelta(minutes=30)).isoformat()}, sat) == []
    assert market_data.due_indicators(inds, {"BRENT": (sat - timedelta(minutes=58)).isoformat()}, sat) == ["BRENT"]


# ---- tick retention -------------------------------------------------------

def test_retention_rolls_up_then_purges_whole_old_days(db):
    """Old ticks become daily open/high/low/close rows and are purged; recent ticks stay; re-running is safe."""
    def tick(ts, price):
        """A tick row for the retention test."""
        return {"indicator_key": "BRENT", "ts": ts, "price": price, "provider": "p", "fetched_at": ts}

    old = [tick("2026-09-01T10:00:00+00:00", 70), tick("2026-09-01T16:00:00+00:00", 74),
           tick("2026-09-01T12:00:00+00:00", 68)]
    recent = [tick("2026-10-26T10:00:00+00:00", 80), tick("2026-10-26T11:00:00+00:00", 82)]
    storage.save_ticks(old + recent)
    days, deleted = storage.purge_ticks(30, now=NOW)
    assert deleted == 3 and days == 2
    sept = storage.latest_daily("BRENT")[-1]
    assert (sept["day"], sept["open"], sept["high"], sept["low"], sept["close"], sept["n_ticks"]) == \
           ("2026-09-01", 70, 74, 68, 74, 3)
    assert len(storage.price_series("BRENT", "2026-01-01")) == 2  # recent ticks kept
    assert storage.purge_ticks(30, now=NOW)[1] == 0  # idempotent
    assert len(storage.latest_daily("BRENT")) == 2   # the recent day is rolled up too, without duplicates
