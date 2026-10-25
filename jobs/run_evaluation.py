"""Generate the morning brief and evaluate it before publishing."""
import logging
from datetime import datetime, timezone

import brief_evaluation
import cost_tracker
import data_sources
import event_calendar
import key_points
import llm_pipeline
import market_calendar
import rag_pipeline
import sentiment_analysis
import storage

BRIEF_MAX_SOURCES = 40


def eia_context_text(eia):
    lines = []
    for name, label in (("brent", "Brent spot USD/bbl"), ("wti", "WTI spot USD/bbl"),
                        ("crude_stocks", "US crude stocks (thousand bbl)")):
        series = eia.get(name) or []
        if series:
            lines.append(f"{label}: " + ", ".join(f"{p}: {v:g}" for p, v in series[:3]))
    # Weekly series kept in eia_weekly (refreshed by data_sources.refresh_eia_weekly), each with its own date.
    for key, label in (("distillate_stocks", "US distillate fuel oil stocks (thousand bbl)"),
                       ("gas_storage", "Lower 48 natural gas storage (Bcf)")):
        rows = storage.eia_weekly(key, 3)
        if rows:
            lines.append(f"{label}: " + ", ".join(f"{r['period']}: {r['value']:g}" for r in rows))
    return "\n".join(lines) or "EIA data unavailable."


def run_evaluation():
    articles = [a for a in storage.verified_articles(storage.hours_ago(24), limit=BRIEF_MAX_SOURCES)
                if a["content_type"] != "commentary"]
    today = storage.utcnow()[:10]
    if not articles:
        storage.log_job("run_evaluation", "skipped", "no verified articles")
        return None
    sentiment = sentiment_analysis.current_sentiment()
    data_sources.refresh_eia_weekly()  # gated: calls EIA at most every EIA_WEEKLY_REFRESH_HOURS
    eia_text = eia_context_text(data_sources.fetch_eia_data())
    brief_date = datetime.fromisoformat(today).date()
    status_line = market_calendar.status_line(datetime.now(timezone.utc))
    events_text = event_calendar.events_text(event_calendar.brief_events(brief_date))
    kp = key_points.build_key_points(brief_date, articles, sentiment, storage.latest_ticks())
    yesterday = key_points.previous_key_points(brief_date, storage.brief_for_date)
    diff = key_points.diff_key_points(kp, yesterday)
    changes = key_points.diff_text(diff, kp, yesterday)
    try:
        history = rag_pipeline.history_context("; ".join(a["title"] for a in articles[:5]))
    except Exception as exc:  # RAG is optional context; the brief can still be generated
        cost_tracker.warn(f"history context unavailable: {exc}")
        history = ""
    brief = llm_pipeline.generate_brief(articles, sentiment, eia_text, history, brief_date=brief_date,
                                        market_status=status_line, events_text=events_text,
                                        changes_text=changes if diff["available"] else "",
                                        changes_missing_text="" if diff["available"] else changes)
    context = (f"{eia_text}\n{status_line}\nScheduled events:\n{events_text or 'none'}\n{changes}\nSentiment reading: {sentiment['label']} (score {sentiment['score']:+.2f}, "
               f"raw {sentiment['score']}, {sentiment['n']} articles)")
    if history:
        context += f"\nArchive headlines:\n{history}"
    evaluation = brief_evaluation.evaluate_brief(brief, articles, context)
    status = "published" if evaluation["passed"] else "flagged_for_review"
    sources = [{"n": i, "title": a["title"], "source": a["source"], "url": a.get("url")}
               for i, a in enumerate(articles, start=1)]  # same numbering the brief's [n] citations use
    storage.save_brief(today, status, evaluation["groundedness"], brief, evaluation, key_points=kp, sources=sources)
    storage.log_job("run_evaluation", "success" if evaluation["passed"] else "alert", "; ".join(evaluation["reasons"]))
    return status


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    with cost_tracker.cost_run("brief_evaluation"):
        print(run_evaluation())
