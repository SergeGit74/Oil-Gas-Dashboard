"""Fetch, normalize, skip known, tag new, corroborate, then sentiment and price check."""
import json
import logging

import config
import cost_tracker
import data_sources
import llm_pipeline
import price_confirmation
import sentiment_analysis
import storage
import story_cluster

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


def matching_corroboration(article, news):
    """Trusted-source Serper results that report the same story as `article`.

    A result counts only if (1) its site is in a trusted tier, (2) it is not the article's own outlet, and (3) its
    title is similar enough to the article's headline (config.CORROBORATION_MIN_SIMILARITY). Several results from
    one outlet count once. Returns a list of {site, url, title, similarity}, best match first."""
    sims = story_cluster.text_similarity(article["title"], [n.get("title", "") for n in news])
    own = llm_pipeline.site_key(article["source"])
    best = {}
    for n, sim in zip(news, sims):
        domain = data_sources.domain_of(n.get("link", ""))
        site = llm_pipeline.site_key(domain)
        if llm_pipeline.source_tier(domain) not in llm_pipeline.TRUSTED_TIERS or site == own:
            continue
        if sim >= config.CORROBORATION_MIN_SIMILARITY and sim > best.get(site, {}).get("similarity", 0):
            best[site] = {"site": site, "url": n.get("link", ""), "title": n.get("title", ""),
                          "similarity": round(sim, 3)}
    return sorted(best.values(), key=lambda m: -m["similarity"])


def corroborate(articles_with_tags):
    """Serper check for new, lower-trust, higher-impact articles. Returns {index: [matches]} (see
    matching_corroboration); the count of matches is the article's corroboration."""
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
        result[i] = matching_corroboration(a, news)
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
        matches = corroboration.get(i, [])
        corr = len(matches)
        rows.append({**a, "collected_at": now, "category": a["tag"]["category"], "direction": a["tag"]["direction"],
                     "magnitude": a["tag"]["magnitude"], "reason": a["tag"]["reason"],
                     "source_tier": a["tier"], "content_type": a["ctype"], "corroboration": corr,
                     "corroboration_json": json.dumps(matches) if matches else None,
                     "confidence": llm_pipeline.compute_confidence(a["tier"], a["ctype"], corr),
                     "flagged_injection": 0})
    storage.save_articles(rows)  # untagged articles are not saved, so they are retried next cycle
    cost_tracker.incr("articles_clustered", story_cluster.cluster_new_articles())  # free: local TF-IDF

    # Sentiment and price check run every cycle from stored articles, not from this cycle's new ones.
    eia = data_sources.fetch_eia_data()
    data_sources.refresh_eia_weekly()  # weekly stocks and gas storage for the dashboard; gated to every few hours
    sentiment = sentiment_analysis.current_sentiment()
    price = price_confirmation.confirm_price(sentiment["label"], eia)
    storage.save_sentiment(now[:10], sentiment, price)
    return {"fetched": len(fetched), "new": len(new), "saved": len(rows), "flagged": len(flagged_rows),
            "sentiment": sentiment["label"], "price_status": price["status"]}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    with cost_tracker.cost_run("collection_cycle"):
        print(run_collection())
