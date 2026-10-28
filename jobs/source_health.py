"""Generic consecutive-degradation monitor. To monitor a new source, log its status to job_runs at the
call site and add one entry to SOURCE_HEALTH_CONFIG."""
import alerts
import demo_mode
import storage

SOURCE_HEALTH_CONFIG = {
    "fetch_apify_commodity_news": {"label": "Apify trade-press feed", "threshold": 3},
    "serper_corroboration": {"label": "Serper corroboration", "threshold": 3},
    "fetch_eia_data": {"label": "EIA price and inventory data", "threshold": 2},
    "fetch_prices": {"label": "Market prices", "threshold": 3},
    "fetch_eia_weekly": {"label": "EIA weekly stocks and gas storage", "threshold": 2},
    "slack_alerts": {"label": "Slack high-impact alerts", "threshold": 3},
    "send_brief_email": {"label": "Brief email", "threshold": 2},
    "load_events": {"label": "Event calendar (EIA schedules)", "threshold": 3},
}
BAD_STATUSES = {"degraded", "failed"}


def current_streak(job_name):
    """Consecutive degraded/failed runs, newest first. 'skipped' neither breaks nor extends a streak."""
    streak = 0
    for run in storage.recent_job_runs(job_name, limit=20):
        if run["status"] == "skipped":
            continue
        if run["status"] in BAD_STATUSES:
            streak += 1
        else:
            break
    return streak


def check_source_health(notify=True):
    """notify=True (scheduler): alert once when a streak reaches its threshold (not on every cycle of a long
    outage) and return the sources alerted.

    notify=False (dashboard): read-only. Sends nothing and writes nothing to job_runs, so a page load can never
    cause an alert; returns every source whose streak is at or past its threshold.

    notify=True refuses to run in demo mode (it would send alerts)."""
    if notify:
        demo_mode.refuse("jobs.source_health.check_source_health(notify=True)")
    flagged = []
    for job_name, cfg in SOURCE_HEALTH_CONFIG.items():
        streak = current_streak(job_name)
        if notify:
            if streak == cfg["threshold"]:
                alerts.send_alert(f"{cfg['label']} degraded for {cfg['threshold']} consecutive cycles.",
                                  source=job_name)
                flagged.append(job_name)
        elif streak >= cfg["threshold"]:
            flagged.append(job_name)
    return flagged
