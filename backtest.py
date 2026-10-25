"""Descriptive backtest: how did each day's news sentiment line up with the Brent return that followed?

Not predictive and not trading advice: it is a small, noisy sample, the sentiment comes from LLM tags, and nothing
here tests whether a rule built on it would have made money.

No look-ahead. The sentiment for day D is the value stored in sentiment_history for D (computed only from articles
collected up to D), and only completed days are used (today's row is still being updated). Its return starts at
D's own Brent close from price_daily and ends at the close of the 1st or 3rd available later day, so a price from
before D is never used and a later price is used only as the outcome. "Available" days skip weekends and holidays
because price_daily only has days with quotes; a horizon is dropped if that close is more than MAX_GAP_DAYS away
or comes from a different provider than the entry close (mixing futures with EIA spot would invent returns).
"""
from datetime import date, datetime, timezone

import numpy as np

import config
import price_confirmation
import storage

BENCHMARK = "BRENT"
HORIZONS = {"next_day": 1, "three_day": 3}
MAX_GAP_DAYS = {1: 4, 3: 10}
LABELS = ("Bull", "Bear", "Neutral")


def build_observations(sentiment_rows, daily_rows, today=None):
    """Pair each completed sentiment day with its forward returns. Inputs are lists of dict rows (any order).
    Returns [{date, label, score, next_day: pct or None, three_day: pct or None}]."""
    today = (today or datetime.now(timezone.utc).date()).isoformat()
    closes = sorted(daily_rows, key=lambda r: r["day"])
    out = []
    for s in sorted(sentiment_rows, key=lambda r: r["date"]):
        if s["date"] >= today:
            continue                                   # today's sentiment is not final yet
        entry = next((r for r in closes if r["day"] == s["date"]), None)
        if entry is None or not entry["close"]:
            continue
        later = [r for r in closes if r["day"] > s["date"]]  # strictly after D: nothing earlier is ever an outcome
        row = {"date": s["date"], "label": s["label"], "score": s["score"]}
        for name, k in HORIZONS.items():
            row[name] = None
            if len(later) >= k:
                target = later[k - 1]
                gap = (date.fromisoformat(target["day"]) - date.fromisoformat(s["date"])).days
                if gap <= MAX_GAP_DAYS[k] and target["provider"] == entry["provider"] and target["close"]:
                    row[name] = round((target["close"] / entry["close"] - 1) * 100, 4)
        out.append(row)
    return out


def _hit(label, ret_pct):
    neutral_band = price_confirmation.NEUTRAL_MOVE_PCT
    if label == "Bull":
        return ret_pct > 0
    if label == "Bear":
        return ret_pct < 0
    return abs(ret_pct) < neutral_band          # Neutral: no meaningful move


def summarize(observations, horizon, min_obs=None):
    """Correlation of score with return and hit rate by label for one horizon, or an insufficient-data marker."""
    min_obs = config.BACKTEST_MIN_OBSERVATIONS if min_obs is None else min_obs
    pairs = [(o["score"], o[horizon], o["label"]) for o in observations if o.get(horizon) is not None]
    n = len(pairs)
    if n < min_obs:
        return {"insufficient": True, "n": n, "needed": min_obs}
    scores = np.array([p[0] for p in pairs], dtype=float)
    rets = np.array([p[1] for p in pairs], dtype=float)
    corr = float(np.corrcoef(scores, rets)[0, 1]) if scores.std() > 0 and rets.std() > 0 else None
    by_label = {}
    for label in LABELS:
        sel = [p for p in pairs if p[2] == label]
        hits = sum(_hit(label, p[1]) for p in sel)
        by_label[label] = {"n": len(sel), "hits": hits, "hit_rate": round(hits / len(sel), 3) if sel else None}
    return {"insufficient": False, "n": n, "correlation": None if corr is None else round(corr, 3),
            "by_label": by_label}


def run_backtest(today=None):
    sentiment = storage.sentiment_history(limit=1000)
    daily = storage.latest_daily(BENCHMARK, limit=1000)
    obs = build_observations(sentiment, daily, today)
    return {"benchmark": BENCHMARK, "observations": obs,
            "sentiment_days": len(sentiment), "price_days": len(daily),
            "results": {h: summarize(obs, h) for h in HORIZONS}}
