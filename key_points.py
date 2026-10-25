"""Structured key points per brief and a deterministic day-over-day diff.

The key points (top verified stories, sentiment, latest prices) are stored with each brief. The diff between two
days is computed here, in code; the model is only asked to phrase it and may not add facts to it.
"""
from datetime import date, timedelta

import config
import story_cluster

TOP_STORIES = 5
UNAVAILABLE_TEXT = "Yesterday's brief is not available, so no day-over-day comparison can be made."


def build_key_points(brief_date, articles, sentiment, ticks):
    """articles: the verified, non-commentary articles the brief was written from. ticks: storage.latest_ticks()."""
    top = sorted(story_cluster.one_per_cluster(articles),
                 key=lambda a: (-(a.get("magnitude") or 0), -(a.get("confidence") or 0)))[:TOP_STORIES]
    return {
        "date": brief_date.isoformat() if isinstance(brief_date, date) else str(brief_date),
        "top_stories": [{"title": a["title"], "source": a.get("source"), "magnitude": a.get("magnitude"),
                         "direction": a.get("direction"), "cluster_id": a.get("cluster_id"), "url": a.get("url")}
                        for a in top],
        "sentiment": {"label": sentiment["label"], "score": sentiment["score"], "n": sentiment["n"]},
        "prices": {k: {"price": t["price"], "change_pct": t.get("change_pct"), "as_of": t["ts"],
                       "provider": t.get("provider")} for k, t in ticks.items()},
    }


def _same_story(a, b):
    return story_cluster.title_overlap(a["title"], b["title"]) >= config.CLUSTER_SIMILARITY_THRESHOLD


def diff_key_points(today, yesterday):
    """Deterministic comparison. `yesterday` None (no brief, or one stored before key points existed) gives
    {"available": False}. Nothing here calls a model."""
    if not yesterday:
        return {"available": False}
    new = [s for s in today["top_stories"] if not any(_same_story(s, y) for y in yesterday["top_stories"])]
    dropped = [y for y in yesterday["top_stories"] if not any(_same_story(y, s) for s in today["top_stories"])]
    t_sent, y_sent = today["sentiment"], yesterday["sentiment"]
    prices = {}
    for key, now in today["prices"].items():
        before = yesterday["prices"].get(key)
        if not before or not before["price"]:
            continue
        prices[key] = {"from": before["price"], "to": now["price"],
                       "change_pct": round((now["price"] - before["price"]) / before["price"] * 100, 2),
                       "unchanged_quote": now["as_of"] == before["as_of"]}
    return {"available": True, "new_stories": new, "dropped_stories": dropped,
            "sentiment": {"from": y_sent["label"], "to": t_sent["label"], "from_score": y_sent["score"],
                          "to_score": t_sent["score"], "score_change": round(t_sent["score"] - y_sent["score"], 4)},
            "prices": prices}


def diff_text(diff, today, yesterday):
    """Plain text of the diff for the prompt and the evaluator's reference data."""
    if not diff["available"]:
        return UNAVAILABLE_TEXT
    lines = [f"Changes since yesterday ({yesterday['date']} to {today['date']}), computed by code:"]
    groups = (("New top stories", diff["new_stories"]), ("No longer among the top stories", diff["dropped_stories"]))
    for label, stories in groups:
        lines.append(f"- {label}: " + ("; ".join(f"{s['title']} ({s['source']}, impact {s['magnitude']}, {s['direction']})"
                                                  for s in stories) if stories else "none"))
    s = diff["sentiment"]
    lines.append(f"- Sentiment: {s['from']} (score {s['from_score']:+.2f}) -> {s['to']} (score {s['to_score']:+.2f}), "
                 f"change {s['score_change']:+.2f}")
    moves = [f"{k} {p['from']:g} -> {p['to']:g} ({p['change_pct']:+.2f}%)" if not p["unchanged_quote"]
             else f"{k} unchanged (same quote time as yesterday)" for k, p in diff["prices"].items()]
    lines.append("- Prices (latest quote at each brief): " + ("; ".join(moves) if moves else "no comparable quotes"))
    return "\n".join(lines)


def previous_key_points(brief_date, load_brief):
    """Key points of the brief for the day before `brief_date`, or None. load_brief(date_str) -> brief row or None."""
    import json

    row = load_brief((brief_date - timedelta(days=1)).isoformat())
    if not row or not row.get("key_points_json"):
        return None
    return json.loads(row["key_points_json"])
