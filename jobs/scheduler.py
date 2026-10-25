"""Long-running APScheduler process. Each cycle runs inside one cost_run."""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler

import config
import cost_tracker
import sentiment_analysis
import storage
from jobs.check_cost_budget import check_cost_budget
from jobs.check_eval_trend import check_eval_trend
from jobs.run_collection import run_collection
from jobs.run_evaluation import run_evaluation
from jobs.run_ingestion import run_ingestion
from jobs.run_prices import purge_old_ticks, run_prices
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
    return storage.utcnow()[:10]


def _claim_brief_run():
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
    if storage.brief_for_date(_brief_day()):
        return None
    if not storage.verified_articles(storage.hours_ago(24), limit=1):
        return None
    if not _claim_brief_run():
        log.info("Brief catch-up not claimed (attempt cap or cooldown).")
        return None
    log.info("No brief for today; generating it now.")
    status = _step("run_evaluation", run_evaluation)
    if status:
        _step("check_eval_trend", check_eval_trend)
    return status


def collection_cycle():
    with cost_tracker.cost_run("collection_cycle"):
        _step("run_collection", run_collection)
        _step("run_ingestion", run_ingestion)
        _step("catch_up_brief", catch_up_brief)
        _step("source_health", check_source_health)


def daily_brief_cycle():
    with cost_tracker.cost_run("daily_brief_cycle"):
        if _claim_brief_run():
            _step("run_evaluation", run_evaluation)
            _step("check_eval_trend", check_eval_trend)
        else:
            log.info("Scheduled brief not claimed (attempt cap or cooldown).")
        _step("source_health", check_source_health)
        _step("check_sentiment_divergence", sentiment_analysis.check_sentiment_divergence)
    _step("check_cost_budget", check_cost_budget)  # after the run so it sees yesterday's finished totals


def price_cycle():
    with cost_tracker.cost_run("price_poll"):
        _step("fetch_prices", run_prices)  # logs its own status; _step logs 'failed' under the same name


def price_retention():
    _step("price_retention", purge_old_ticks)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    missing = [k for k in config.REQUIRED_KEYS if not config.get(k)]
    if missing:  # steps that need these degrade or fail visibly; the scheduler itself keeps running
        log.warning("Missing settings in .env, the related steps will fail or degrade: %s", ", ".join(missing))
    scheduler = BlockingScheduler(timezone=TIMEZONE)
    scheduler.add_job(collection_cycle, "interval", minutes=config.COLLECTION_INTERVAL_MINUTES,
                      id="collection_cycle", max_instances=1, coalesce=True)
    scheduler.add_job(price_cycle, "interval", minutes=config.PRICE_POLL_INTERVAL_MIN,
                      id="price_poll", max_instances=1, coalesce=True)
    scheduler.add_job(daily_brief_cycle, "cron", hour=config.BRIEF_HOUR, minute=config.BRIEF_MINUTE,
                      id="daily_brief_cycle", max_instances=1, coalesce=True)
    scheduler.add_job(price_retention, "cron", hour=3, minute=10, id="price_retention",
                      max_instances=1, coalesce=True)
    log.info("Scheduler started (%s). Running one price poll and one collection cycle now.", TIMEZONE)
    _step("price_cycle", price_cycle)
    _step("collection_cycle", collection_cycle)
    scheduler.start()


if __name__ == "__main__":
    main()
