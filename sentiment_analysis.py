"""Weighted Bull/Bear/Neutral aggregation over stored, verified, direction-tagged articles."""
import alerts
import storage
import story_cluster

DIRECTION_VALUE = {"Bullish": 1, "Bearish": -1, "Neutral": 0, "Mixed": 0}
BULL_THRESHOLD = 0.15
BEAR_THRESHOLD = -0.15
DIVERGENCE_ALERT_DAYS = 3


def score_articles(articles):
    """Score = mean of direction (+1/-1/0) x (magnitude/5) x confidence. Mixed counts as zero."""
    counts = {"Bullish": 0, "Bearish": 0, "Neutral": 0, "Mixed": 0}
    total, n = 0.0, 0
    for a in articles:
        if a.get("direction") not in DIRECTION_VALUE:
            continue
        counts[a["direction"]] += 1
        total += DIRECTION_VALUE[a["direction"]] * (a["magnitude"] / 5) * a["confidence"]
        n += 1
    score = total / n if n else 0.0
    label = "Bull" if score >= BULL_THRESHOLD else "Bear" if score <= BEAR_THRESHOLD else "Neutral"
    return {"label": label, "score": round(score, 4), "n": n, "counts": counts}


def current_sentiment(hours=24):
    """Sentiment from stored verified articles first seen in the last `hours` hours, counting one article per story
    (cluster): the highest-confidence, then highest-magnitude one. `n` is the number of stories."""
    articles = storage.verified_articles(storage.hours_ago(hours), directional_only=True)
    return score_articles(story_cluster.one_per_cluster(articles))


def check_sentiment_divergence():
    """Alert after 3 consecutive divergent days. Never adjusts the score or the brief."""
    rows = storage.sentiment_history(DIVERGENCE_ALERT_DAYS)
    if len(rows) == DIVERGENCE_ALERT_DAYS and all(r["confirmation"] == "Divergent" for r in rows):
        alerts.send_alert(
            f"News sentiment has diverged from the Brent price move for {DIVERGENCE_ALERT_DAYS} consecutive days. "
            "Analyst review recommended.", source="sentiment_divergence")
        return True
    return False
