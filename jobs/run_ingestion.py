"""Embed verified, not-yet-indexed articles into the vector store."""
import logging

import cost_tracker
import rag_pipeline
import storage


def run_ingestion():
    """Embed verified articles not yet indexed into the vector store; returns how many."""
    articles = storage.unindexed_verified()
    if not articles:
        storage.log_job("run_ingestion", "skipped", "nothing to index")
        return 0
    try:
        chunks = rag_pipeline.ingest_articles(articles)
    except Exception as exc:
        storage.log_job("run_ingestion", "failed", str(exc))
        raise
    storage.mark_indexed([a["id"] for a in articles])
    storage.log_job("run_ingestion", "success", f"{len(articles)} articles, {chunks} chunks")
    return len(articles)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    with cost_tracker.cost_run("ingestion"):
        print(run_ingestion())
