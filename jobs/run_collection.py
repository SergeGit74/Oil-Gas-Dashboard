"""Fetch, normalize, skip known, tag new, corroborate, then sentiment and price check."""
import logging

import cost_tracker
import data_sources
import llm_pipeline
import price_confirmation
import sentiment_analysis
import storage

log = logging.getLogger("run_collection")

SERPER_MAX_PER_CYCLE = 10
SERPER_MIN_MAGNITUDE = 3


def _dedupe(articles):
    seen, out = set(), []
    for a in articles:
        if a["url"] not in seen:
            seen.add(a["url"])
            out.append(a)
    return out


def corroborate(articles_with_tags):
    """Count distinct trusted domains in Serper news results for new, lower-trust, higher-impact articles.
    Returns {index: corroboration_count}."""
    candidates = [i for i, a in articles_with_tags.items()
                  if a["tier"] not in llm_pipeline.TRUSTED_TIERS and a["tag"]["magnitude"] >= SERPER_MIN_MAGNITUDE]
    candidates = sorted(candidates, key=lambda i: -articles_with_tags[i]["tag"]["magnitude"])[:SERPER_MAX_PER_CYCLE]
    if not candidates:
        storage.log_job("serper_corroboration", "skipped", "no candidates")
        return {}
    result, errors = {}, []
    for i in candidates:
        a = articles_with_tags[i]
        try:
            news = data_sources.serper_news(a["title"], num=10)
        except Exception as exc:  # network, quota or missing key: degrade, keep going
            errors.append(str(exc))
            continue
        domains = {data_sources.domain_of(n.get("link", "")) for n in news}
        trusted = {d for d in domains if llm_pipeline.source_tier(d) in llm_pipeline.TRUSTED_TIERS}
        result[i] = len(trusted - {a["source"]})
    storage.log_job("serper_corroboration", "degraded" if errors else "success", "; ".join(errors[:3]))
    return result


def run_collection():
    """One collection cycle. Returns a summary dict."""
    fetched = _dedupe(data_sources.fetch_apify_commodity_news() + data_sources.fetch_gdelt())
    cost_tracker.incr("articles_fetched", len(fetched))
    known = storage.known_urls(a["url"] for a in fetched)
    new = [a for a in fetched if a["url"] not in known]
    cost_tracker.incr("articles_skipped_known", len(fetched) - len(new))
    cost_tracker.incr("articles_new", len(new))
    now = storage.utcnow()

    flagged_rows, to_tag = [], []
    for a in new:
        a["tier"] = llm_pipeline.source_tier(a["source"])
        a["ctype"] = llm_pipeline.content_type(a)
        if llm_pipeline.flag_injection(a):
            # Excluded from every prompt; stored only so it shows in the unverified panel.
            flagged_rows.append({**a, "collected_at": now, "source_tier": a["tier"], "content_type": a["ctype"],
                                 "flagged_injection": 1, "confidence": 0.0, "reason": "possible prompt injection"})
        else:
            to_tag.append(a)

    tags = llm_pipeline.tag_articles(to_tag) if to_tag else {}
    tagged = {i: {**to_tag[i], "tag": tag} for i, tag in tags.items()}
    corroboration = corroborate(tagged) if tagged else {}

    rows = list(flagged_rows)
    for i, a in tagged.items():
        corr = corroboration.get(i, 0)
        rows.append({**a, "collected_at": now, "category": a["tag"]["category"], "direction": a["tag"]["direction"],
                     "magnitude": a["tag"]["magnitude"], "reason": a["tag"]["reason"],
                     "source_tier": a["tier"], "content_type": a["ctype"], "corroboration": corr,
                     "confidence": llm_pipeline.compute_confidence(a["tier"], a["ctype"], corr),
                     "flagged_injection": 0})
    storage.save_articles(rows)  # untagged articles are not saved, so they are retried next cycle

    # Sentiment and price check run every cycle from stored articles, not from this cycle's new ones.
    eia = data_sources.fetch_eia_data()
    sentiment = sentiment_analysis.current_sentiment()
    price = price_confirmation.confirm_price(sentiment["label"], eia)
    storage.save_sentiment(now[:10], sentiment, price)
    return {"fetched": len(fetched), "new": len(new), "saved": len(rows), "flagged": len(flagged_rows),
            "sentiment": sentiment["label"], "price_status": price["status"]}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    with cost_tracker.cost_run("collection_cycle"):
        print(run_collection())
