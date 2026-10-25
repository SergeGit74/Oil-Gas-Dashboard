import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import config
import storage
from jobs import scheduler

DAY = "2026-10-26"
T0 = datetime(2026, 10, 26, 3, 0, tzinfo=timezone.utc)


# ---- claim_daily_run ------------------------------------------------------

def test_two_concurrent_claimers_only_one_wins(db):
    results, barrier = [], threading.Barrier(8)

    def claimer():
        barrier.wait()
        results.append(storage.claim_daily_run("brief", DAY, max_attempts=1, cooldown_min=30, now=T0))

    threads = [threading.Thread(target=claimer) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(True) == 1 and results.count(False) == 7


def test_attempt_cap_and_cooldown_respected(db):
    assert storage.claim_daily_run("brief", DAY, 2, 30, now=T0)
    assert not storage.claim_daily_run("brief", DAY, 2, 30, now=T0 + timedelta(minutes=10))  # cooldown
    assert storage.claim_daily_run("brief", DAY, 2, 30, now=T0 + timedelta(minutes=31))      # second attempt
    assert not storage.claim_daily_run("brief", DAY, 2, 30, now=T0 + timedelta(hours=5))     # cap reached


def test_claims_are_per_job_and_per_day(db):
    assert storage.claim_daily_run("brief", DAY, 1, 30, now=T0)
    assert storage.claim_daily_run("brief_email", DAY, 1, 30, now=T0)
    assert storage.claim_daily_run("brief", "2026-10-27", 1, 30, now=T0)


# ---- catch_up_brief -------------------------------------------------------

def _at(hour, minute=0):
    return datetime(2026, 10, 26, hour, minute, tzinfo=ZoneInfo("Asia/Dubai"))


def _setup(monkeypatch, calls, verified=True, status="published"):
    monkeypatch.setattr(scheduler, "_brief_day", lambda: DAY)
    monkeypatch.setattr(scheduler.storage, "verified_articles", lambda *a, **k: [{"id": 1}] if verified else [])
    monkeypatch.setattr(scheduler, "run_evaluation", lambda: calls.append("eval") or status)
    monkeypatch.setattr(scheduler, "check_eval_trend", lambda: calls.append("trend"))


def test_no_brief_runs_after_catchup_time(db, monkeypatch):
    calls = []
    _setup(monkeypatch, calls)
    assert scheduler.catch_up_brief(now=_at(6, 5)) == "published"
    assert calls == ["eval", "trend"]


def test_before_catchup_time_does_nothing(db, monkeypatch):
    calls = []
    _setup(monkeypatch, calls)
    assert scheduler.catch_up_brief(now=_at(5, 59)) is None
    assert calls == []


def test_published_brief_counts_as_happened(db, monkeypatch):
    calls = []
    _setup(monkeypatch, calls)
    storage.save_brief(DAY, "published", 0.95, "text", {})
    assert scheduler.catch_up_brief(now=_at(7)) is None
    assert calls == []


def test_flagged_brief_counts_as_happened(db, monkeypatch):
    calls = []
    _setup(monkeypatch, calls)
    storage.save_brief(DAY, "flagged_for_review", 0.5, "text", {"reasons": ["x"]})
    assert scheduler.catch_up_brief(now=_at(7)) is None
    assert calls == []


def test_failing_run_is_retried_at_most_twice_a_day(db, monkeypatch):
    calls = []
    _setup(monkeypatch, calls, status=None)  # run_evaluation produces no brief
    clock = {"now": T0}
    real = storage.claim_daily_run
    monkeypatch.setattr(scheduler.storage, "claim_daily_run",
                        lambda *a, **k: real(*a, **{**k, "now": clock["now"]}))
    for minutes in (0, 5, 40, 80, 400):
        clock["now"] = T0 + timedelta(minutes=minutes)
        scheduler.catch_up_brief(now=_at(7))
    assert calls.count("eval") == 2  # 0 and 40; the 5 minute call hit the cooldown, the rest the cap


def test_no_verified_articles_spends_no_attempt(db, monkeypatch):
    calls = []
    _setup(monkeypatch, calls, verified=False)
    assert scheduler.catch_up_brief(now=_at(7)) is None
    assert calls == []
    assert storage.claim_daily_run("daily_brief", DAY, config.BRIEF_MAX_ATTEMPTS, 30)  # attempt still available
