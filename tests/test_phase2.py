from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import config
import data_sources
import event_calendar
import freshness
import llm_pipeline
import market_calendar
import market_data
import storage
from jobs import run_evaluation

APP = str(Path(__file__).resolve().parent.parent / "app.py")


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


# ---- market_status --------------------------------------------------------

def test_market_status_normal_weekday_is_open():
    now = utc(2026, 10, 27, 14, 0)  # Tuesday 10:00 New York
    assert {k: market_calendar.market_status(k, now).status for k in ("BRENT", "WTI", "TTF", "SPX", "GOLD")} == \
           {k: "open" for k in ("BRENT", "WTI", "TTF", "SPX", "GOLD")}


def test_market_status_weekend_is_closed_not_holiday():
    s = market_calendar.market_status("WTI", utc(2026, 10, 24, 14, 0))  # Saturday
    assert s.status == "closed"
    assert s.last_close.startswith("2026-10-23") and s.next_open.startswith("2026-10-25")  # Sunday evening open


def test_market_status_holiday():
    xmas = utc(2026, 12, 25, 14, 0)  # a Friday
    assert market_calendar.market_status("WTI", xmas).status == "holiday"
    assert market_calendar.market_status("SPX", xmas).status == "holiday"
    assert market_calendar.market_status("SPX", utc(2026, 11, 26, 15, 0)).status == "holiday"  # US Thanksgiving
    assert market_calendar.market_status("TTF", utc(2026, 11, 26, 10, 0)).status == "open"     # Amsterdam trades


def test_market_status_outside_session_hours_on_a_trading_day():
    s = market_calendar.market_status("SPX", utc(2026, 10, 27, 22, 0))  # after the US close
    assert s.status == "closed" and s.last_close.startswith("2026-10-27")


def test_market_status_falls_back_to_window_without_calendar():
    cfg = {**market_data.load_indicators()["BRENT"], "calendar": None}
    s = market_calendar.market_status(cfg, utc(2026, 10, 27, 14, 0))
    assert s.status == "open" and "approximate" in s.note
    assert market_calendar.market_status(cfg, utc(2026, 10, 24, 14, 0)).status == "closed"


def test_status_line_lists_closed_markets_with_last_close():
    line = market_calendar.status_line(utc(2026, 10, 25, 12, 0))  # Sunday
    assert line.startswith("Market status at 25 Oct 2026 12:00 UTC")
    assert "open - none" in line and "BRENT (last close 23 Oct)" in line


def test_holiday_polls_at_the_slow_rate():
    cfg = market_data.load_indicators()["WTI"]
    assert market_data.interval_minutes(cfg, utc(2026, 10, 27, 14, 0)) == config.PRICE_POLL_INTERVAL_MIN
    assert market_data.interval_minutes(cfg, utc(2026, 12, 25, 14, 0)) == config.PRICE_POLL_OFFHOURS_INTERVAL_MIN


# ---- derived values -------------------------------------------------------

def tick(key, price, ts, provider="yahoo"):
    return {"indicator_key": key, "price": price, "ts": ts, "provider": provider}


def test_derived_metrics():
    t = "2026-10-27T14:00:00+00:00"
    out = market_data.derived_metrics({"BRENT": tick("BRENT", 80.0, t), "WTI": tick("WTI", 76.5, t),
                                       "GOLD": tick("GOLD", 4000.0, t, "twelvedata")})
    assert out["brent_wti_spread"]["value"] == 3.5
    assert out["gold_brent_ratio"]["value"] == 50.0


def test_derived_metrics_refuse_stale_or_mixed_inputs():
    t = "2026-10-27T14:00:00+00:00"
    old = "2026-10-20T14:00:00+00:00"
    assert market_data.derived_metrics({"BRENT": tick("BRENT", 80, t), "WTI": tick("WTI", 76, old)}) \
        ["brent_wti_spread"] is None                                           # more than 24 h apart
    assert market_data.derived_metrics({"BRENT": tick("BRENT", 80, t), "WTI": tick("WTI", 76, t, "eia")}) \
        ["brent_wti_spread"] is None                                           # different providers
    assert market_data.derived_metrics({"BRENT": tick("BRENT", 80, t)})["gold_brent_ratio"] is None


# ---- EIA weekly -----------------------------------------------------------

def test_refresh_eia_weekly_stores_and_is_gated(db, monkeypatch):
    monkeypatch.setenv("EIA_API_KEY", "test")
    calls = []
    monkeypatch.setattr(data_sources, "_eia_series",
                        lambda route, series, freq, length=10: calls.append(series) or [("2026-10-16", 100.0), ("2026-10-09", 90.0)])
    assert data_sources.refresh_eia_weekly() is True
    assert len(calls) == 3
    assert storage.eia_weekly("gas_storage")[0] == {"period": "2026-10-16", "value": 100.0, "unit": "Bcf",
                                                    "fetched_at": storage.eia_weekly("gas_storage")[0]["fetched_at"]}
    assert data_sources.refresh_eia_weekly() is False and len(calls) == 3      # fetched recently: no new call
    assert storage.recent_job_runs("fetch_eia_weekly", 5)[0]["status"] == "success"


def test_refresh_eia_weekly_without_key_is_degraded(db, monkeypatch):
    monkeypatch.setenv("EIA_API_KEY", "")
    assert data_sources.refresh_eia_weekly() is False
    assert storage.recent_job_runs("fetch_eia_weekly", 1)[0]["status"] == "degraded"


def test_eia_context_text_includes_weekly_series_with_dates(db):
    storage.save_eia_weekly("distillate_stocks", [("2026-10-16", 104286.0), ("2026-10-09", 102611.0)], "thousand bbl")
    storage.save_eia_weekly("gas_storage", [("2026-10-16", 3644.0)], "Bcf")
    text = run_evaluation.eia_context_text({"brent": [("2026-10-20", 115.22)]})
    assert "Brent spot USD/bbl: 2026-10-20: 115.22" in text
    assert "distillate fuel oil stocks (thousand bbl): 2026-10-16: 104286, 2026-10-09: 102611" in text
    assert "natural gas storage (Bcf): 2026-10-16: 3644" in text


# ---- event schedules ------------------------------------------------------

WPSR_TEXT = ("Weekly Petroleum Status Report Schedule Release Schedule The standard release time and day of the week "
             "will be at 10:30 a.m. eastern time on Wednesday with the following exceptions. All times are eastern. "
             "Data for the week ending Alternate release date Release day Release time Holiday "
             "October 10, 2025 October 16, 2025 Thursday 12:00 p.m. Columbus Day "
             "November 6, 2026 November 12, 2026 Thursday 12:00 p.m. Veterans Day")
NGS_TEXT = ("Weekly Natural Gas Storage Report Schedule Holiday Release Schedule The standard release time and day of "
            "the week will be at 10:30 a.m. eastern time on Thursdays with the following exceptions. All times are "
            "eastern. Alternate release date Release day Release time Holiday "
            "January 3, 2025 Friday 10:30 a.m. New Year's Day "
            "November 13, 2026 Friday 10:30 a.m. Veterans Day November 25, 2026 Wednesday 12:00 p.m. Thanksgiving Day EIA - 1000")


def test_parse_wpsr_standard_wednesdays_and_moved_week():
    rows = event_calendar.parse_wpsr(WPSR_TEXT, date(2026, 11, 1), horizon=21)
    got = {(r["date"], r["time"]) for r in rows}
    assert ("2026-11-04", "10:30") in got and ("2026-11-18", "10:30") in got
    assert ("2026-11-11", "10:30") not in got          # week ending Nov 6 was moved...
    assert ("2026-11-12", "12:00") in got              # ...to Thursday noon
    assert all(r["tz"] == "America/New_York" for r in rows)


def test_parse_ngs_standard_thursdays_and_displaced_weeks():
    got = {(r["date"], r["time"]) for r in event_calendar.parse_ngs(NGS_TEXT, date(2026, 11, 1), horizon=30)}
    assert ("2026-11-05", "10:30") in got and ("2026-11-19", "10:30") in got
    assert ("2026-11-12", "10:30") not in got and ("2026-11-13", "10:30") in got      # Friday replaces Thursday
    assert ("2026-11-26", "10:30") not in got and ("2026-11-25", "12:00") in got      # Thanksgiving week


def test_parser_refuses_a_page_without_the_standard_sentence():
    with pytest.raises(event_calendar.ScheduleParseError):
        event_calendar.parse_wpsr("Release Schedule October 10, 2025 October 16, 2025 Thursday 12:00 p.m.", date(2026, 1, 1))


def test_load_events_is_idempotent_and_a_failed_page_keeps_old_rows(db):
    pages = {event_calendar.WPSR_URL: WPSR_TEXT, event_calendar.NGS_URL: NGS_TEXT}
    counts, errors = event_calendar.load_events(today=date(2026, 11, 1), fetch=pages.__getitem__)
    assert errors == [] and counts["manual"] > 0 and counts["eia_wpsr"] > 0
    def snapshot():  # ids change when a source's rows are regenerated, so compare the content only
        return [{k: v for k, v in e.items() if k != "id"} for e in storage.events_between("2026-11-01", "2027-12-31")]

    before = snapshot()
    event_calendar.load_events(today=date(2026, 11, 1), fetch=pages.__getitem__)
    assert snapshot() == before                                                   # no duplicates

    def broken(url):
        return "layout changed" if url == event_calendar.WPSR_URL else NGS_TEXT

    _, errors = event_calendar.load_events(today=date(2026, 11, 1), fetch=broken)
    assert len(errors) == 1 and "eia_wpsr" in errors[0]
    assert snapshot() == before                                                   # old WPSR rows kept


# ---- brief prompt ---------------------------------------------------------

def _capture_prompt(monkeypatch):
    seen = {}

    class Resp:
        pass

    def fake_create(step, system, messages, max_tokens):
        seen["system"], seen["user"] = system, messages[0]["content"]
        return Resp()

    monkeypatch.setattr(llm_pipeline.tracked_llm, "create_message", fake_create)
    monkeypatch.setattr(llm_pipeline.tracked_llm, "text_of", lambda r: "## Watch today\nNo scheduled events.")
    return seen


SENT = {"label": "Bull", "score": 0.2, "n": 3}
ART = [{"title": "t", "source": "s", "direction": "Bullish", "magnitude": 4, "snippet": ""}]


def test_brief_prompt_contains_only_events_from_the_table(db, monkeypatch):
    seen = _capture_prompt(monkeypatch)
    storage.upsert_events([
        {"date": "2026-10-28", "time": "10:30", "tz": "America/New_York", "title": "Table event A", "category": "x"},
        {"date": "2026-10-30", "time": None, "tz": None, "title": "Table event B", "category": "y"},
        {"date": "2026-11-20", "time": None, "tz": None, "title": "Too far ahead", "category": "y"},
        {"date": "2026-10-20", "time": None, "tz": None, "title": "Already past", "category": "y"},
    ])
    events = event_calendar.brief_events(date(2026, 10, 28))
    llm_pipeline.generate_brief(ART, SENT, "eia", brief_date=date(2026, 10, 28),
                                market_status="Market status: ...", events_text=event_calendar.events_text(events))
    prompt = seen["user"]
    assert "Table event A" in prompt and "Table event B" in prompt
    assert "Too far ahead" not in prompt and "Already past" not in prompt
    assert "ONLY the events given" in seen["system"] and "Never add an event" in seen["system"]


def test_brief_prompt_says_none_when_the_table_is_empty(db, monkeypatch):
    seen = _capture_prompt(monkeypatch)
    llm_pipeline.generate_brief(ART, SENT, "eia", brief_date=date(2026, 10, 28),
                                events_text=event_calendar.events_text(event_calendar.brief_events(date(2026, 10, 28))))
    assert "the only events Watch today may mention):\nnone" in seen["user"]


def test_run_evaluation_passes_market_status_and_table_events(db, monkeypatch):
    storage.upsert_events([{"date": storage.utcnow()[:10], "time": "10:30", "tz": "America/New_York",
                            "title": "Only this event", "category": "x"}])
    storage.save_articles([{"url": "u1", "title": "Oil rises", "source": "reuters.com", "snippet": "x",
                            "collected_at": storage.utcnow(), "direction": "Bullish", "magnitude": 4,
                            "confidence": 0.9, "content_type": "news", "flagged_injection": 0}])
    seen = {}
    monkeypatch.setattr(run_evaluation.data_sources, "refresh_eia_weekly", lambda: None)
    monkeypatch.setattr(run_evaluation.data_sources, "fetch_eia_data", lambda: {})
    monkeypatch.setattr(run_evaluation.sentiment_analysis, "current_sentiment", lambda: SENT)
    monkeypatch.setattr(run_evaluation.rag_pipeline, "history_context", lambda q: "")

    def fake_generate(articles, sentiment, eia, history, brief_date, market_status, events_text):
        seen.update(market_status=market_status, events_text=events_text)
        return "# t\n\nbody"

    monkeypatch.setattr(run_evaluation.llm_pipeline, "generate_brief", fake_generate)
    monkeypatch.setattr(run_evaluation.brief_evaluation, "evaluate_brief",
                        lambda brief, articles, context: seen.update(context=context) or
                        {"passed": True, "groundedness": 1.0, "reasons": []})
    assert run_evaluation.run_evaluation() == "published"
    assert seen["market_status"].startswith("Market status at") and "Only this event" in seen["events_text"]
    assert "Only this event" in seen["context"] and seen["market_status"] in seen["context"]


# ---- freshness ------------------------------------------------------------

def test_level_thresholds():
    assert freshness.level_for(None, 10, 20) == "unknown"
    assert [freshness.level_for(a, 10, 20) for a in (5, 10, 19, 20, 90)] == ["ok", "amber", "amber", "red", "red"]


def test_collection_freshness_reads_job_runs(db):
    now = datetime.now(timezone.utc)
    assert freshness.collection_freshness(now).level == "unknown"
    storage.log_job("fetch_gdelt", "success", "x")
    assert freshness.collection_freshness(now + timedelta(minutes=5)).level == "ok"
    assert freshness.collection_freshness(now + timedelta(minutes=config.STALE_COLLECTION_AMBER_MIN + 1)).level == "amber"
    assert freshness.collection_freshness(now + timedelta(minutes=config.STALE_COLLECTION_RED_MIN + 1)).level == "red"


def test_price_freshness_only_judged_while_a_market_is_open(db):
    storage.save_ticks([{"indicator_key": "BRENT", "ts": "2026-10-27T10:00:00+00:00", "price": 80.0,
                         "provider": "yahoo", "fetched_at": "2026-10-27T10:00:00+00:00"}])
    open_now = utc(2026, 10, 27, 14, 0)             # four hours later, markets open
    assert freshness.price_freshness(open_now).level == "red"
    closed = utc(2026, 10, 24, 14, 0)               # same age check on a Saturday must not alarm
    storage.save_ticks([{"indicator_key": "BRENT", "ts": "2026-10-24T08:00:00+00:00", "price": 80.0,
                         "provider": "yahoo", "fetched_at": "2026-10-24T08:00:00+00:00"}])
    assert freshness.price_freshness(closed).level == "ok" and "markets closed" in freshness.price_freshness(closed).text


def test_brief_freshness_states(db, monkeypatch):
    # 02:30 UTC = 06:30 Dubai: past the 06:00 catch-up time with no brief
    late = utc(2026, 10, 27, 2, 30)
    assert freshness.brief_freshness(late).level == "amber"
    assert freshness.brief_freshness(utc(2026, 10, 27, 5, 30)).level == "red"
    assert freshness.brief_freshness(utc(2026, 10, 27, 0, 30)).level == "ok"       # 04:30 Dubai: still pending
    storage.save_brief("2026-10-27", "published", 0.95, "text", {})
    assert freshness.brief_freshness(late).level == "ok"


# ---- dashboard ------------------------------------------------------------

def test_dashboard_renders_phase2_panels(db):
    now = storage.utcnow()
    storage.save_ticks([
        {"indicator_key": k, "ts": now, "price": p, "prev_close": p - 1, "change_pct": 1.0, "provider": "yahoo",
         "fetched_at": now} for k, p in (("BRENT", 80.0), ("WTI", 76.0), ("SPX", 5000.0))])
    storage.save_eia_weekly("gas_storage", [("2026-10-16", 3644.0), ("2026-10-09", 3567.0)], "Bcf")
    storage.upsert_events([{"date": now[:10], "time": "10:30", "tz": "America/New_York", "title": "Dash event",
                            "category": "x", "source_url": "https://example.com"}])
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception
    values = [m.value for m in at.metric]
    assert "+4.00" in values                                  # Brent-WTI spread tile
    assert any("3,644" in v for v in values)                  # gas storage
    assert any("title" in d.value and "Dash event" in d.value["title"].tolist() for d in at.dataframe)
