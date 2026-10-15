"""Generic consecutive-degradation monitor. To monitor a new source, log its status to job_runs at the
call site and add one entry to SOURCE_HEALTH_CONFIG."""
import alerts
import storage

SOURCE_HEALTH_CONFIG = {
    "fetch_apify_commodity_news": {"label": "Apify trade-press feed", "threshold": 3},
    "serper_corroboration": {"label": "Serper corroboration", "threshold": 3},
    "fetch_eia_data": {"label": "EIA price and inventory data", "threshold": 2},
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


def check_source_health():
    """Alert once when a streak reaches its threshold (not on every cycle of a long outage)."""
    alerted = []
    for job_name, cfg in SOURCE_HEALTH_CONFIG.items():
        if current_streak(job_name) == cfg["threshold"]:
            alerts.send_alert(f"{cfg['label']} degraded for {cfg['threshold']} consecutive cycles.",
                              source=job_name)
            alerted.append(job_name)
    return alerted
