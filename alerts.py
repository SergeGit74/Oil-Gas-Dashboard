"""Alert delivery: always logged and printed; posted to Slack when SLACK_WEBHOOK_URL is set."""
import logging

import requests

import config
import storage

log = logging.getLogger("alerts")


def send_slack(text):
    """Post one message to the Slack webhook. True if Slack accepted it; False if there is no webhook or the post
    failed (logged, never raised). No other side effects: callers do their own logging."""
    if not config.SLACK_WEBHOOK_URL:
        return False
    try:
        resp = requests.post(config.SLACK_WEBHOOK_URL, json={"text": text}, timeout=10)
        resp.raise_for_status()
        return True
    except requests.RequestException as exc:
        log.error("Slack post failed: %s", exc)
        return False


def send_alert(message, source="alert"):
    """Report a system problem: log it, print it, record it in job_runs and post it to Slack when configured."""
    log.warning("ALERT [%s] %s", source, message)
    print(f"ALERT [{source}] {message}")
    storage.log_job(f"alert:{source}", "alert", message)
    if config.SLACK_WEBHOOK_URL:
        try:
            requests.post(config.SLACK_WEBHOOK_URL, json={"text": f":warning: [{source}] {message}"}, timeout=10)
        except requests.RequestException as exc:
            log.error("Slack post failed: %s", exc)
