"""Long-running APScheduler process. Each cycle runs inside one cost_run.

SCHEDULER_MODE=local (default) runs every job. SCHEDULER_MODE=deployment runs only deployment_cycle."""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler

import config
import cost_tracker
import demo_mode
import sentiment_analysis
import storage
from jobs.check_cost_budget import check_cost_budget
from jobs.check_eval_trend import check_eval_trend
from jobs.run_collection import run_collection
from jobs.load_events import run_load_events
from jobs.run_evaluation import run_evaluation
from jobs.run_ingestion import run_ingestion
from jobs.run_prices import purge_old_ticks, run_prices
from jobs.send_brief_email import send_brief_email
from jobs.source_health import check_source_health

log = logging.getLogger("scheduler")
TIMEZONE = config.DESK_TIMEZONE  # set DESK_TIMEZONE in .env


def _step(name, fn):
    """Run one step; a failure is logged and does not stop the rest of the cycle."""
    try:
        return fn()
    except Exception as exc:
        log.exception("%s failed", name)
        storage.log_job(name, "failed", str(exc))
        cost_tracker.warn(f"{name} failed: {exc}")


def _brief_day():
    # Same key run_evaluation saves the brief under.
    """The date key (UTC) briefs and the daily-brief claim are stored under."""
    return storage.utcnow()[:10]


def _claim_brief_run():
    """Try to claim today's brief run (limited attempts and cooldown)."""
    return storage.claim_daily_run("daily_brief", _brief_day(), config.BRIEF_MAX_ATTEMPTS,
                                   config.BRIEF_RETRY_COOLDOWN_MIN)


def catch_up_brief(now=None):
    """Create today's brief if the scheduled run did not produce one.

    Runs at scheduler startup and after every collection cycle, but only once the local time in the desk
    timezone is past config.CATCHUP_AFTER_HOUR:CATCHUP_AFTER_MINUTE (06:00 by default). Any brief row for today,
    including one flagged for review, counts as "the morning run happened". Otherwise it needs a claim, so a
    failing run is retried at most BRIEF_MAX_ATTEMPTS times a day, BRIEF_RETRY_COOLDOWN_MIN apart, and never
    twice at once. With no verified articles there is nothing to brief and no attempt is spent."""
    now = now or datetime.now(ZoneInfo(TIMEZONE))
    if (now.hour, now.minute) < (config.CATCHUP_AFTER_HOUR, config.CATCHUP_AFTER_MINUTE):
        return None
    return brief_if_missing()


def brief_if_missing():
    """Generate and evaluate today's brief only when no brief row exists for today (published or flagged) and
    there are verified articles to brief; a claim_daily_run claim then limits retries. Returns the run status or None."""
    if storage.brief_for_date(_brief_day()):
        return None
    if not storage.verified_articles(storage.hours_ago(24), limit=1):
        return None
    if not _claim_brief_run():
        log.info("Brief not claimed (attempt cap or cooldown).")
        return None
    log.info("No brief for today; generating it now.")
    status = _step("run_evaluation", run_evaluation)
    if status:
        _step("check_eval_trend", check_eval_trend)
    return status


def collection_cycle():
    """One costed collection cycle: collect, ingest, catch up a missed brief, email it, check source health."""
    with cost_tracker.cost_run("collection_cycle"):
        _step("run_collection", run_collection)
        _step("run_ingestion", run_ingestion)
        _step("catch_up_brief", catch_up_brief)
        _step("send_brief_email", send_brief_email)  # no-op unless enabled and today's brief is unsent
        _step("source_health", check_source_health)


def daily_brief_cycle():
    """The 05:30 cycle: generate and evaluate the brief if claimed, email it, then run health, divergence and budget checks."""
    with cost_tracker.cost_run("daily_brief_cycle"):
        if _claim_brief_run():
            _step("run_evaluation", run_evaluation)
            _step("check_eval_trend", check_eval_trend)
            _step("send_brief_email", send_brief_email)
        else:
            log.info("Scheduled brief not claimed (attempt cap or cooldown).")
        _step("source_health", check_source_health)
        _step("check_sentiment_divergence", sentiment_analysis.check_sentiment_divergence)
    _step("check_cost_budget", check_cost_budget)  # after the run so it sees yesterday's finished totals


def price_cycle():
    """One zero-cost price poll."""
    with cost_tracker.cost_run("price_poll"):
        _step("fetch_prices", run_prices)  # logs its own status; _step logs 'failed' under the same name


def price_retention():
    """Daily job: roll up and purge old price ticks."""
    _step("price_retention", purge_old_ticks)


def events_cycle():
    """Daily job: reload the event calendar."""
    with cost_tracker.cost_run("load_events"):
        _step("load_events", run_load_events)  # logs its own status; _step logs 'failed' under the same name


def deployment_cycle():
    """The only job in SCHEDULER_MODE=deployment: collect, ingest, brief (once a day), email it, check source health."""
    with cost_tracker.cost_run("deployment_cycle"):
        _step("run_collection", run_collection)
        _step("run_ingestion", run_ingestion)
        _step("brief_if_missing", brief_if_missing)  # no brief yet today and a claim, else a no-op
        _step("send_brief_email", send_brief_email)  # no-op unless enabled and today's brief is unsent
        _step("source_health", check_source_health)


def validate_mode():
    """Check SCHEDULER_MODE, SCHEDULER_TZ and DEPLOY_RUN_HOURS; returns (mode, timezone name, hours).
    Raises ValueError with a readable message on a bad value."""
    mode = config.SCHEDULER_MODE
    if mode not in ("local", "deployment"):
        raise ValueError(f"SCHEDULER_MODE must be 'local' or 'deployment', not {mode!r}")
    hours = config.parse_run_hours(config.DEPLOY_RUN_HOURS_RAW)
    try:
        ZoneInfo(config.SCHEDULER_TZ)
    except Exception as exc:  # ZoneInfoNotFoundError, or ValueError for a malformed key
        raise ValueError(f"SCHEDULER_TZ {config.SCHEDULER_TZ!r} is not a known timezone (is tzdata installed?)") from exc
    return mode, config.SCHEDULER_TZ, hours


def register_jobs(scheduler, mode="local", tz=None, hours=None):
    """Add the jobs for the mode. Local: the five regular jobs. Deployment: deployment_cycle only, at minute 0 of
    each hour in `hours` in timezone `tz`. Every other job is left unregistered."""
    if mode == "deployment":
        hours = hours if hours is not None else config.DEPLOY_RUN_HOURS
        scheduler.add_job(deployment_cycle, "cron", hour=",".join(str(h) for h in hours), minute=0,
                          timezone=tz or config.SCHEDULER_TZ, id="deployment_cycle", max_instances=1,
                          coalesce=True, misfire_grace_time=900)
        return
    scheduler.add_job(collection_cycle, "interval", minutes=config.COLLECTION_INTERVAL_MINUTES,
                      id="collection_cycle", max_instances=1, coalesce=True)
    scheduler.add_job(price_cycle, "interval", minutes=config.PRICE_POLL_INTERVAL_MIN,
                      id="price_poll", max_instances=1, coalesce=True)
    scheduler.add_job(daily_brief_cycle, "cron", hour=config.BRIEF_HOUR, minute=config.BRIEF_MINUTE,
                      id="daily_brief_cycle", max_instances=1, coalesce=True)
    scheduler.add_job(price_retention, "cron", hour=3, minute=10, id="price_retention",
                      max_instances=1, coalesce=True)
    scheduler.add_job(events_cycle, "cron", hour=4, minute=0, id="load_events", max_instances=1, coalesce=True)


@demo_mode.block_in_demo("jobs.scheduler.main")
def main():
    """Start the scheduler: register the jobs for the mode, run the startup cycles once (local mode), then block."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        mode, tz, hours = validate_mode()
    except ValueError as exc:
        raise SystemExit(f"Scheduler not started: {exc}")
    missing = [k for k in config.REQUIRED_KEYS if not config.get(k)]
    if missing:  # steps that need these degrade or fail visibly; the scheduler itself keeps running
        log.warning("Missing settings in .env, the related steps will fail or degrade: %s", ", ".join(missing))
    scheduler = BlockingScheduler(timezone=tz if mode == "deployment" else TIMEZONE)
    register_jobs(scheduler, mode, tz, hours)
    if mode == "deployment":
        if tz != config.DESK_TIMEZONE:
            log.warning("SCHEDULER_TZ (%s) differs from DESK_TIMEZONE (%s): the dashboard header and the brief "
                        "catch-up times use DESK_TIMEZONE. Keep them equal.", tz, config.DESK_TIMEZONE)
        log.info("Scheduler started in deployment mode (%s). deployment_cycle runs at %s:00; nothing runs now. "
                 "Prices, event loads, retention and budget checks are disabled.",
                 tz, ", ".join(f"{h:02d}" for h in hours))
        scheduler.start()
        return
    log.info("Scheduler started (%s). Running the event load, one price poll and one collection cycle now.", TIMEZONE)
    _step("events_cycle", events_cycle)  # before the first brief catch-up so "Watch today" has its events
    _step("price_cycle", price_cycle)
    _step("collection_cycle", collection_cycle)
    scheduler.start()


if __name__ == "__main__":
    main()
