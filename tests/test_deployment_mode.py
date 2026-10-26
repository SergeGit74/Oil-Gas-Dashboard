"""SCHEDULER_MODE=deployment: the single deployment_cycle job, its schedule, and the brief-once-per-day logic."""
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from apscheduler.schedulers.blocking import BlockingScheduler
from streamlit.testing.v1 import AppTest

import brief_email
import config
import freshness
import storage
from jobs import scheduler
from jobs.send_brief_email import send_brief_email as real_send_brief_email

ROOT = Path(__file__).resolve().parent.parent
DUBAI = ZoneInfo("Asia/Dubai")
HOURS = [6, 10, 14, 18, 22]
LOCAL_JOBS = {"collection_cycle", "price_poll", "daily_brief_cycle", "price_retention", "load_events"}


# ---- settings -------------------------------------------------------------

def test_parse_run_hours_default_sorts_and_dedupes():
    """The default list parses, and a messy list is sorted, de-duplicated and trimmed."""
    assert config.parse_run_hours("6,10,14,18,22") == HOURS
    assert config.parse_run_hours(" 22, 6 ,6,14 ") == [6, 14, 22]


@pytest.mark.parametrize("raw", ["", " , ", "24", "-1", "six", "6;10", "6.5"])
def test_parse_run_hours_rejects_bad_values(raw):
    """Empty, out-of-range and non-numeric hours are errors."""
    with pytest.raises(ValueError):
        config.parse_run_hours(raw)


def test_longest_gap_wraps_past_midnight():
    """The longest gap includes the wrap from the last run of one day to the first of the next."""
    assert config.longest_gap_minutes(HOURS) == 8 * 60      # 22:00 -> 06:00
    assert config.longest_gap_minutes([6]) == 24 * 60
    assert config.longest_gap_minutes([0, 12]) == 12 * 60


def test_validate_mode_accepts_defaults_and_rejects_bad_settings(monkeypatch):
    """validate_mode returns the settings, and raises a readable error for a bad mode, hour list or timezone."""
    monkeypatch.setattr(config, "SCHEDULER_MODE", "deployment")
    monkeypatch.setattr(config, "SCHEDULER_TZ", "Asia/Dubai")
    monkeypatch.setattr(config, "DEPLOY_RUN_HOURS_RAW", "6,10,14,18,22")
    assert scheduler.validate_mode() == ("deployment", "Asia/Dubai", HOURS)
    monkeypatch.setattr(config, "SCHEDULER_MODE", "prod")
    with pytest.raises(ValueError, match="SCHEDULER_MODE"):
        scheduler.validate_mode()
    monkeypatch.setattr(config, "SCHEDULER_MODE", "local")
    monkeypatch.setattr(config, "DEPLOY_RUN_HOURS_RAW", "6,25")
    with pytest.raises(ValueError, match="DEPLOY_RUN_HOURS"):
        scheduler.validate_mode()
    monkeypatch.setattr(config, "DEPLOY_RUN_HOURS_RAW", "6")
    monkeypatch.setattr(config, "SCHEDULER_TZ", "Mars/Olympus")
    with pytest.raises(ValueError, match="SCHEDULER_TZ"):
        scheduler.validate_mode()


def test_deployment_defaults_in_a_fresh_process():
    """With only SCHEDULER_MODE=deployment set, config derives the brief time and staleness limits from the run hours."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("DEPLOY_", "SCHEDULER_", "STALE_", "BRIEF_", "CATCHUP_", "DESK_"))}
    env.update(SCHEDULER_MODE="deployment", PYTHONPATH=str(ROOT), DATA_DIR=str(ROOT / "tests"))
    code = ("import config; print(config.DEPLOYMENT, config.SCHEDULER_TZ, config.BRIEF_HOUR, config.BRIEF_MINUTE, "
            "config.STALE_AFTER_MINUTES, config.STALE_COLLECTION_AMBER_MIN, config.STALE_COLLECTION_RED_MIN)")
    out = subprocess.run([sys.executable, "-c", code], env=env, cwd=ROOT, capture_output=True, text=True, check=True)
    assert out.stdout.split() == ["True", "Asia/Dubai", "6", "0", "540", "540", "660"]


# ---- schedule -------------------------------------------------------------

def test_deployment_registers_only_deployment_cycle():
    """Deployment mode registers exactly one job, so every other job stays disabled."""
    sched = BlockingScheduler(timezone="Asia/Dubai")
    scheduler.register_jobs(sched, "deployment", "Asia/Dubai", HOURS)
    assert [job.id for job in sched.get_jobs()] == ["deployment_cycle"]


def test_local_mode_still_registers_the_five_regular_jobs():
    """Local mode is unchanged: the same five jobs, no deployment_cycle."""
    sched = BlockingScheduler(timezone="Asia/Dubai")
    scheduler.register_jobs(sched, "local")
    assert {job.id for job in sched.get_jobs()} == LOCAL_JOBS


def test_deployment_cycle_fires_at_the_configured_dubai_hours():
    """From 05:00 Dubai the next fires are 06, 10, 14, 18, 22, then 06 the next day, all on the hour in Dubai time."""
    sched = BlockingScheduler(timezone="Asia/Dubai")
    scheduler.register_jobs(sched, "deployment", "Asia/Dubai", HOURS)
    trigger = sched.get_jobs()[0].trigger
    fires, when = [], datetime(2026, 10, 26, 5, 0, tzinfo=DUBAI)
    for _ in range(6):
        when = trigger.get_next_fire_time(None, when)
        fires.append(when)
        when += timedelta(seconds=1)
    assert [f.hour for f in fires] == HOURS + [6]
    assert all(f.minute == 0 and f.second == 0 for f in fires)
    assert fires[-1].date() == fires[0].date() + timedelta(days=1)
    assert all(f.utcoffset() == timedelta(hours=4) for f in fires)


def test_custom_hours_and_timezone_are_used():
    """DEPLOY_RUN_HOURS and the timezone argument drive the trigger."""
    sched = BlockingScheduler(timezone="UTC")
    scheduler.register_jobs(sched, "deployment", "Europe/London", [7, 19])
    trigger = sched.get_jobs()[0].trigger
    first = trigger.get_next_fire_time(None, datetime(2026, 1, 5, 0, 0, tzinfo=ZoneInfo("Europe/London")))
    assert (first.hour, first.tzinfo.key) == (7, "Europe/London")


# ---- the cycle ------------------------------------------------------------

@pytest.fixture
def cycle(db, monkeypatch):
    """Stub the pipeline steps, record the order they run in, and give a verified article to brief."""
    calls = {"order": [], "brief_status": "published"}
    day = storage.utcnow()[:10]

    def step(name, result=None):
        """A stub for one scheduler step that records its name."""
        return lambda *a, **k: calls["order"].append(name) or result

    def fake_evaluation():
        """Stand-in for run_evaluation: records the call and stores today's brief unless told to fail."""
        calls["order"].append("eval")
        if calls["brief_status"]:
            storage.save_brief(day, calls["brief_status"], 0.95, "# Brief\n\nbody [1]", {"reasons": []},
                               key_points={}, sources=[{"n": 1, "title": "T", "source": "reuters.com", "url": "https://x.test/a"}])
        return calls["brief_status"]

    monkeypatch.setattr(scheduler, "run_collection", step("collection"))
    monkeypatch.setattr(scheduler, "run_ingestion", step("ingestion"))
    monkeypatch.setattr(scheduler, "run_evaluation", fake_evaluation)
    monkeypatch.setattr(scheduler, "check_eval_trend", step("trend"))
    monkeypatch.setattr(scheduler, "send_brief_email", step("email"))
    monkeypatch.setattr(scheduler, "check_source_health", step("health"))
    monkeypatch.setattr(scheduler.storage, "verified_articles", lambda *a, **k: [{"id": 1}])
    calls["day"] = day
    return calls


def test_first_run_makes_the_brief_then_emails_and_checks_health(cycle):
    """With no brief today: collection, ingestion, brief, trend check, email, then source health, in that order."""
    scheduler.deployment_cycle()
    assert cycle["order"] == ["collection", "ingestion", "eval", "trend", "email", "health"]


def test_later_runs_skip_the_brief_once_one_exists(cycle):
    """After a published brief, the next runs still collect, email and check health but never regenerate it."""
    scheduler.deployment_cycle()
    cycle["order"].clear()
    for _ in range(3):
        scheduler.deployment_cycle()
    assert "eval" not in cycle["order"] and cycle["order"].count("collection") == 3
    assert cycle["order"].count("email") == 3 and cycle["order"].count("health") == 3


def test_flagged_brief_is_not_regenerated(cycle):
    """A brief flagged for review counts as today's brief, so later runs do not regenerate it."""
    cycle["brief_status"] = "flagged_for_review"
    scheduler.deployment_cycle()
    cycle["order"].clear()
    scheduler.deployment_cycle()
    assert "eval" not in cycle["order"]


def test_denied_claim_means_no_brief(cycle):
    """If claim_daily_run refuses (another attempt is inside its cooldown), the brief is not generated."""
    assert storage.claim_daily_run("daily_brief", cycle["day"], config.BRIEF_MAX_ATTEMPTS, 30)
    scheduler.deployment_cycle()
    assert "eval" not in cycle["order"] and "health" in cycle["order"]


def test_no_verified_articles_means_no_brief_and_no_attempt_spent(cycle, monkeypatch):
    """With nothing to brief the evaluation does not run, and the day's claim is still available afterwards."""
    monkeypatch.setattr(scheduler.storage, "verified_articles", lambda *a, **k: [])
    scheduler.deployment_cycle()
    assert "eval" not in cycle["order"]
    assert storage.claim_daily_run("daily_brief", cycle["day"], config.BRIEF_MAX_ATTEMPTS, 30)


def test_failing_brief_is_retried_at_most_twice_a_day(cycle, monkeypatch):
    """Five runs in a day where the evaluation never produces a brief: it is attempted twice, then the cap holds."""
    cycle["brief_status"] = None
    clock = {"now": datetime.now(DUBAI)}
    real = storage.claim_daily_run
    monkeypatch.setattr(scheduler.storage, "claim_daily_run",
                        lambda *a, **k: real(*a, **{**k, "now": clock["now"].astimezone(ZoneInfo("UTC"))}))
    for hours in (0, 4, 8, 12, 16):  # runs four hours apart, well past the 30 minute cooldown
        clock["now"] = datetime.now(DUBAI) + timedelta(hours=hours)
        scheduler.deployment_cycle()
    assert cycle["order"].count("eval") == config.BRIEF_MAX_ATTEMPTS == 2
    assert cycle["order"].count("health") == 5


def test_a_failing_step_does_not_stop_the_rest(cycle, monkeypatch):
    """If collection raises, ingestion, the brief, the email and the health check still run, and the failure is logged."""
    def boom():
        """A collection step that fails."""
        raise RuntimeError("apify down")

    monkeypatch.setattr(scheduler, "run_collection", boom)
    scheduler.deployment_cycle()
    assert cycle["order"] == ["ingestion", "eval", "trend", "email", "health"]
    assert any(r["job_name"] == "run_collection" and r["status"] == "failed" for r in storage.latest_job_runs(20))


def test_cycle_is_recorded_as_one_cost_run(cycle):
    """The cycle runs inside cost_run, so exactly one run_costs row named deployment_cycle is written."""
    scheduler.deployment_cycle()
    rows = [r for r in storage.recent_run_costs(20) if r["job_name"] == "deployment_cycle"]
    assert len(rows) == 1 and rows[0]["status"] == "success"
    assert storage.last_cycle_finished() == rows[0]["finished_at"]


# ---- email through the cycle ----------------------------------------------

@pytest.fixture
def smtp(cycle, monkeypatch):
    """Enable email with test settings and record what smtp_send is asked to send; uses the real send_brief_email."""
    for name, value in {"EMAIL_ENABLED": "true", "SMTP_HOST": "smtp.example.test", "SMTP_PORT": "587",
                        "SMTP_USER": "user", "SMTP_PASSWORD": "pw", "EMAIL_FROM": "desk@example.test",
                        "EMAIL_TO": "owner@example.test"}.items():
        monkeypatch.setenv(name, value)
    sent = []
    monkeypatch.setattr(brief_email, "smtp_send", lambda msg, to: sent.append((msg, to)))
    monkeypatch.setattr(scheduler, "send_brief_email", real_send_brief_email)
    return sent


def test_brief_is_emailed_once_across_all_runs_of_the_day(cycle, smtp):
    """The first run emails the new brief; the four later runs find it already sent and send nothing."""
    for _ in range(5):
        scheduler.deployment_cycle()
    assert len(smtp) == 1 and smtp[0][1] == ["owner@example.test"]


def test_email_waits_for_the_first_run_that_makes_a_brief(cycle, smtp, monkeypatch):
    """If the first run produces no brief, nothing is emailed; the next run that makes one sends it."""
    cycle["brief_status"] = None
    scheduler.deployment_cycle()
    assert smtp == []
    cycle["brief_status"] = "published"
    real = storage.claim_daily_run  # the brief claim has a 30 minute cooldown, so run the second cycle 4 hours later
    later = datetime.now(ZoneInfo("UTC")) + timedelta(hours=4)
    monkeypatch.setattr(scheduler.storage, "claim_daily_run", lambda *a, **k: real(*a, **{**k, "now": later}))
    scheduler.deployment_cycle()
    assert len(smtp) == 1


def test_email_disabled_sends_nothing(cycle, smtp, monkeypatch):
    """With EMAIL_ENABLED=false the email step is a no-op even after a brief is made."""
    monkeypatch.setenv("EMAIL_ENABLED", "false")
    scheduler.deployment_cycle()
    assert smtp == []


# ---- dashboard in deployment mode ------------------------------------------

def test_header_does_not_judge_prices_in_deployment_mode(db, monkeypatch):
    """The price freshness indicator says prices are not polled, instead of going red."""
    monkeypatch.setattr(config, "DEPLOYMENT", True)
    item = freshness.price_freshness(datetime.now(ZoneInfo("UTC")))
    assert item.level == "unknown" and "not polled" in item.text


def test_dashboard_renders_in_deployment_mode(db, monkeypatch):
    """The dashboard renders in deployment mode and tells the reader the price tiles are not live."""
    monkeypatch.setattr(config, "DEPLOYMENT", True)
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
    assert not at.exception
    assert any("Deployment mode" in c.value for c in at.caption)
