"""Alert delivery: always logged and printed; posted to Slack when SLACK_WEBHOOK_URL is set."""
import logging

import requests

import config
import storage

log = logging.getLogger("alerts")


def send_alert(message, source="alert"):
    log.warning("ALERT [%s] %s", source, message)
    print(f"ALERT [{source}] {message}")
    storage.log_job(f"alert:{source}", "alert", message)
    if config.SLACK_WEBHOOK_URL:
        try:
            requests.post(config.SLACK_WEBHOOK_URL, json={"text": f":warning: [{source}] {message}"}, timeout=10)
        except requests.RequestException as exc:
            log.error("Slack post failed: %s", exc)
