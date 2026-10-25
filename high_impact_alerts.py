"""Slack alerts for high-impact, verified news: one message per story, capped per hour, never for unverified items.

"Verified" is the project-wide definition (confidence >= config.VERIFIED_MIN_CONFIDENCE and not injection-flagged);
High confidence is 0.8 and above, Medium 0.5 to 0.8. A story is a cluster (see story_cluster.py) and is alerted at
most once ever, tracked in `alert_log`. Alerts over the hourly cap are recorded as suppressed and are not replayed:
the articles are no longer new by the next cycle.
"""
import logging
import re
from datetime import datetime, timezone

import alerts
import config
import storage

log = logging.getLogger("high_impact_alerts")
MAX_TITLE_CHARS = 300
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def slack_escape(text):
    """Scraped text must not trigger Slack markup: escape &, < and > (which also defuses <!channel>, <@U123> and
    <http://...|label> links), drop control characters and cap the length."""
    text = _CONTROL.sub(" ", text or "").strip()
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return text if len(text) <= MAX_TITLE_CHARS else text[:MAX_TITLE_CHARS - 1] + "…"


def is_alertable(article):
    """Whether an article is verified, direction-tagged and at or above the alert magnitude."""
    return (not article.get("flagged_injection") and article.get("direction")
            and (article.get("confidence") or 0) >= config.VERIFIED_MIN_CONFIDENCE
            and (article.get("magnitude") or 0) >= config.ALERT_MIN_MAGNITUDE)


def cluster_key(article):
    """The dedupe key for a story: its cluster, or the article itself when it has none."""
    return f"cluster:{article['cluster_id']}" if article.get("cluster_id") else f"article:{article['id']}"


def verified_outlets(article):
    """Distinct outlets that reported the story, counting verified articles only."""
    if not article.get("cluster_id"):
        return 1
    sources = {m["source"] for m in storage.cluster_members([article["cluster_id"]])
               if not m["flagged_injection"] and (m["confidence"] or 0) >= config.VERIFIED_MIN_CONFIDENCE}
    return max(len(sources), 1)


def format_message(article, outlets):
    """Build the Slack text for a story, with Slack markup in scraped text escaped."""
    level = "High" if article["confidence"] >= 0.8 else "Medium"
    outlet_text = "1 outlet" if outlets == 1 else f"{outlets} outlets"
    url = article.get("url") or ""
    link = f"\n{url}" if url.startswith(("http://", "https://")) else ""
    return (f":rotating_light: *High-impact news* ({article['direction']}, impact {article['magnitude']}/5, "
            f"{level} confidence)\n{slack_escape(article['title'])}\n"
            f"{slack_escape(article['source'] or 'unknown outlet')}, reported by {outlet_text}{link}")


def run_alerts(collected_at, now=None, send=None):
    """Alert on the qualifying articles saved in one collection cycle. Returns {"sent", "suppressed", "failed",
    "not_configured"} counts. Logs `slack_alerts` to job_runs only when a send was attempted."""
    send = send or alerts.send_slack
    now = now or datetime.now(timezone.utc)
    best = {}
    for a in storage.articles_collected_at(collected_at):
        if is_alertable(a):
            key = cluster_key(a)
            if key not in best or (a["confidence"], a["magnitude"]) > (best[key]["confidence"], best[key]["magnitude"]):
                best[key] = a
    counts = {"sent": 0, "suppressed": 0, "failed": 0, "not_configured": 0}
    # Most important first, so the hourly cap drops the least important.
    for key, a in sorted(best.items(), key=lambda kv: (-kv[1]["magnitude"], -kv[1]["confidence"])):
        if not config.SLACK_WEBHOOK_URL:
            counts["not_configured"] += storage.record_alert(key, a, "not_configured", now.isoformat(timespec="seconds"))
        elif storage.alerts_in_last_hour(now) >= config.ALERT_MAX_PER_HOUR:
            counts["suppressed"] += storage.record_alert(key, a, "suppressed_rate_limit", now.isoformat(timespec="seconds"))
        elif storage.record_alert(key, a, "pending", now.isoformat(timespec="seconds")):  # claims the story first
            ok = send(format_message(a, verified_outlets(a)))
            storage.set_alert_status(key, "sent" if ok else "failed")
            counts["sent" if ok else "failed"] += 1
    if counts["sent"] or counts["failed"]:
        storage.log_job("slack_alerts", "degraded" if counts["failed"] else "success",
                        f"{counts['sent']} sent, {counts['failed']} failed, {counts['suppressed']} suppressed")
    elif counts["suppressed"]:
        storage.log_job("slack_alerts", "skipped", f"{counts['suppressed']} suppressed by the hourly cap")
    return counts
