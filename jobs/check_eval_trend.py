"""Groundedness drift monitoring over eval_history.jsonl."""
import json
import os
from statistics import mean

import alerts
import config

MIN_GROUNDEDNESS = 0.85
DRIFT_DROP = 0.10


def check_eval_trend():
    """Alert if recent brief groundedness is low or has dropped sharply; returns whether it alerted."""
    if not os.path.exists(config.EVAL_HISTORY_PATH):
        return False
    with open(config.EVAL_HISTORY_PATH, encoding="utf-8") as f:
        scores = [json.loads(line)["groundedness"] for line in f if line.strip()]
    if len(scores) < 3:
        return False
    recent = scores[-3:]
    earlier = scores[-10:-3]
    if mean(recent) < MIN_GROUNDEDNESS:
        alerts.send_alert(f"Brief groundedness averaged {mean(recent):.2f} over the last 3 briefs "
                          f"(minimum {MIN_GROUNDEDNESS}).", source="eval_trend")
        return True
    if earlier and mean(earlier) - mean(recent) > DRIFT_DROP:
        alerts.send_alert(f"Brief groundedness dropped from {mean(earlier):.2f} to {mean(recent):.2f}.",
                          source="eval_trend")
        return True
    return False
