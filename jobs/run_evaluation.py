"""Generate the morning brief and evaluate it before publishing."""
import logging
from datetime import datetime

import brief_evaluation
import cost_tracker
import data_sources
import llm_pipeline
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
    return "\n".join(lines) or "EIA data unavailable."


def run_evaluation():
    articles = [a for a in storage.verified_articles(storage.hours_ago(24), limit=BRIEF_MAX_SOURCES)
                if a["content_type"] != "commentary"]
    today = storage.utcnow()[:10]
    if not articles:
        storage.log_job("run_evaluation", "skipped", "no verified articles")
        return None
    sentiment = sentiment_analysis.current_sentiment()
    eia_text = eia_context_text(data_sources.fetch_eia_data())
    try:
        history = rag_pipeline.history_context("; ".join(a["title"] for a in articles[:5]))
    except Exception as exc:  # RAG is optional context; the brief can still be generated
        cost_tracker.warn(f"history context unavailable: {exc}")
        history = ""
    brief = llm_pipeline.generate_brief(articles, sentiment, eia_text, history,
                                        brief_date=datetime.fromisoformat(today).date())
    context = (f"{eia_text}\nSentiment reading: {sentiment['label']} (score {sentiment['score']:+.2f}, "
               f"raw {sentiment['score']}, {sentiment['n']} articles)")
    if history:
        context += f"\nArchive headlines:\n{history}"
    evaluation = brief_evaluation.evaluate_brief(brief, articles, context)
    status = "published" if evaluation["passed"] else "flagged_for_review"
    storage.save_brief(today, status, evaluation["groundedness"], brief, evaluation)
    storage.log_job("run_evaluation", "success" if evaluation["passed"] else "alert", "; ".join(evaluation["reasons"]))
    return status


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    with cost_tracker.cost_run("brief_evaluation"):
        print(run_evaluation())
