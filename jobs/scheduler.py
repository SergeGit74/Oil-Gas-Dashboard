"""Long-running APScheduler process. Each cycle runs inside one cost_run."""
import logging

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


def collection_cycle():
    with cost_tracker.cost_run("collection_cycle"):
        _step("run_collection", run_collection)
        _step("run_ingestion", run_ingestion)
        _step("source_health", check_source_health)


def daily_brief_cycle():
    with cost_tracker.cost_run("daily_brief_cycle"):
        _step("run_evaluation", run_evaluation)
        _step("check_eval_trend", check_eval_trend)
        _step("source_health", check_source_health)
        _step("check_sentiment_divergence", sentiment_analysis.check_sentiment_divergence)
    _step("check_cost_budget", check_cost_budget)  # after the run so it sees yesterday's finished totals


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    config.require(*config.REQUIRED_KEYS)
    scheduler = BlockingScheduler(timezone=TIMEZONE)
    scheduler.add_job(collection_cycle, "interval", minutes=config.COLLECTION_INTERVAL_MINUTES,
                      id="collection_cycle", max_instances=1, coalesce=True)
    scheduler.add_job(daily_brief_cycle, "cron", hour=config.BRIEF_HOUR, minute=config.BRIEF_MINUTE,
                      id="daily_brief_cycle", max_instances=1, coalesce=True)
    log.info("Scheduler started (%s). Running one collection cycle now.", TIMEZONE)
    _step("collection_cycle", collection_cycle)
    scheduler.start()


if __name__ == "__main__":
    main()
