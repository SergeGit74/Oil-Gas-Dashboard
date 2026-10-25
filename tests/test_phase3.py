import csv
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import backtest
import config
import data_sources
import feedback_tools
import key_points
import llm_pipeline
import sentiment_analysis
import storage
import story_cluster
from jobs import run_collection, run_evaluation

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


def iso(dt):
    """A datetime as an ISO string to the second."""
    return dt.isoformat(timespec="seconds")


def article(url, title, source="example.com", hours_ago=1.0, **extra):
    """A complete article row, collected `hours_ago` before the test's 'now'."""
    base = {"url": url, "title": title, "source": source, "snippet": "", "collected_at": iso(NOW - timedelta(hours=hours_ago)),
            "direction": "Bullish", "magnitude": 3, "confidence": 0.8, "content_type": "news",
            "flagged_injection": 0, "category": "Supply"}
    return {**base, **extra}


def clusters():
    """{url: cluster_id} for every stored article."""
    return {r["url"]: r["cluster_id"] for r in storage._rows("SELECT url, cluster_id FROM articles")}


# ---- story clustering -----------------------------------------------------

UKRAINE = ["Ukraine resumes attacks on Russian oil refineries, officials say",
           "Ukraine resumes attacks on Russian oil refineries",
           "Ukraine Resumes Attacks On Russian Oil Refineries As Tensions Deepen With US"]


def test_duplicates_merge_and_distinct_stories_do_not(db):
    """Near-duplicate headlines share a cluster; unrelated ones get their own."""
    rows = [article(f"u{i}", t, f"outlet{i}.com") for i, t in enumerate(UKRAINE)]
    rows += [article("s1", "Saudi Arabia intercepts missile near Red Sea oil hub"),
             article("o1", "OPEC+ weighs December output increase, delegates say")]
    storage.save_articles(rows)
    assert story_cluster.cluster_new_articles(now=NOW) == 5
    c = clusters()
    assert c["u0"] == c["u1"] == c["u2"]
    assert len({c["u0"], c["s1"], c["o1"]}) == 3


def test_cluster_id_is_the_earliest_member_and_is_stable(db):
    """A cluster's id is its first article's id, later articles join it, and re-running assigns nothing."""
    storage.save_articles([article("u0", UKRAINE[0])])
    story_cluster.cluster_new_articles(now=NOW)
    first = clusters()["u0"]
    storage.save_articles([article("u1", UKRAINE[1], hours_ago=0.5)])
    assert story_cluster.cluster_new_articles(now=NOW) == 1
    assert clusters() == {"u0": first, "u1": first}                         # joined, original id untouched
    assert story_cluster.cluster_new_articles(now=NOW) == 0                 # idempotent


def test_same_headline_a_day_apart_is_a_new_story(db):
    """The same headline more than 24 hours later starts a new cluster."""
    storage.save_articles([article("old", UKRAINE[0], hours_ago=30), article("new", UKRAINE[0], hours_ago=1)])
    story_cluster.cluster_new_articles(now=NOW)
    c = clusters()
    assert c["old"] != c["new"]


def test_similarity_measure():
    """Similar headlines score high and unrelated ones low; no candidates gives an empty list."""
    sims = story_cluster.text_similarity(UKRAINE[0], [UKRAINE[1], "Saudi Arabia intercepts missile near Red Sea"])
    assert sims[0] > 0.6 > 0.1 > sims[1]
    assert story_cluster.text_similarity("anything", []) == []


def test_sentiment_counts_one_article_per_story(db):
    """Sentiment counts one article per cluster, the highest-confidence one."""
    storage.save_articles([
        article("a", UKRAINE[0], "reuters.com", confidence=0.8, magnitude=4),
        article("b", UKRAINE[1], "bloomberg.com", confidence=0.95, magnitude=4),
        article("c", UKRAINE[2], "oilprice.com", confidence=0.6, magnitude=5, direction="Bearish"),
        article("d", "Saudi Arabia intercepts missile near Red Sea oil hub", direction="Neutral", magnitude=2)])
    story_cluster.cluster_new_articles(now=NOW)
    rows = storage.verified_articles(iso(NOW - timedelta(hours=24)), directional_only=True)
    assert len(rows) == 4
    got = sentiment_analysis.score_articles(story_cluster.one_per_cluster(rows))
    assert got["n"] == 2                                                    # two stories, not four articles
    # the Ukraine story is represented by its highest-confidence article (b, Bullish), not the Bearish outlier
    assert got["counts"]["Bullish"] == 1 and got["counts"]["Bearish"] == 0


def test_one_per_cluster_keeps_unclustered_rows_separate():
    """Articles without a cluster are never merged with each other."""
    rows = [{"id": 1, "confidence": 0.9, "magnitude": 3}, {"id": 2, "confidence": 0.7, "magnitude": 3}]
    assert len(story_cluster.one_per_cluster(rows)) == 2


# ---- trust matching by domain suffix --------------------------------------

def test_subdomains_match_the_registry_on_whole_labels():
    """uk.reuters.com matches reuters.com but notreuters.com does not."""
    assert llm_pipeline.source_tier("uk.reuters.com") == "wire_trade"
    assert llm_pipeline.source_tier("www.reuters.com") == "wire_trade"
    assert llm_pipeline.source_tier("markets.ft.com") == "wire_trade"
    assert llm_pipeline.source_tier("notreuters.com") == "unknown"
    assert llm_pipeline.source_tier("reuters.com.evil.example") == "unknown"
    assert llm_pipeline.source_tier("finance.yahoo.com") == "aggregator"
    assert llm_pipeline.source_tier("yahoo.com") == "unknown"
    assert llm_pipeline.source_tier("") == "unknown"
    assert llm_pipeline.site_key("uk.reuters.com") == llm_pipeline.site_key("reuters.com") == "reuters.com"
    assert llm_pipeline.site_key("www.example.org") == "example.org"


# ---- corroboration by matching content ------------------------------------

def serper(title, link, snippet=""):
    """A fake Serper news result."""
    return {"title": title, "link": link, "snippet": snippet}


ART = {"title": UKRAINE[0], "source": "kcra.com"}


def test_only_trusted_matching_results_count():
    """Only trusted, similar-titled results from other outlets corroborate."""
    news = [serper(UKRAINE[1], "https://uk.reuters.com/world/x"),                                  # match, subdomain
            serper("EXCLUSIVE: Trump on rising gas prices during Iran operation", "https://www.bloomberg.com/y"),  # trusted, unrelated
            serper(UKRAINE[2], "https://some-local-paper.com/z"),                                  # matches, not trusted
            serper(UKRAINE[0], "https://www.apnews.com/story")]                                    # match
    got = run_collection.matching_corroboration(ART, news)
    assert {m["site"] for m in got} == {"reuters.com", "apnews.com"}
    assert all(m["similarity"] >= config.CORROBORATION_MIN_SIMILARITY for m in got)


def test_own_outlet_and_repeats_do_not_count():
    """The article's own outlet is excluded and repeats from one outlet count once."""
    art = {"title": UKRAINE[0], "source": "reuters.com"}
    news = [serper(UKRAINE[1], "https://uk.reuters.com/a"), serper(UKRAINE[1], "https://www.apnews.com/b"),
            serper(UKRAINE[2], "https://apnews.com/c")]
    got = run_collection.matching_corroboration(art, news)
    assert [m["site"] for m in got] == ["apnews.com"]                       # reuters is the article's own outlet


def test_corroborate_returns_matches_and_they_are_stored(db, monkeypatch):
    """corroborate returns the matches, and they are saved in corroboration_json."""
    monkeypatch.setattr(data_sources, "serper_news", lambda q, num=10: [serper(UKRAINE[1], "https://reuters.com/a")])
    tagged = {0: {**ART, "tier": "unknown", "tag": {"magnitude": 4}}}
    result = run_collection.corroborate(tagged)
    assert [m["site"] for m in result[0]] == ["reuters.com"]
    storage.save_articles([article("u0", UKRAINE[0], "kcra.com", corroboration=1,
                                   corroboration_json=json.dumps(result[0]))])
    stored = storage._rows("SELECT corroboration, corroboration_json FROM articles")[0]
    assert json.loads(stored["corroboration_json"])[0]["url"] == "https://reuters.com/a"


# ---- backtest -------------------------------------------------------------

def closes(*pairs, provider="yahoo"):
    """Daily close rows for the backtest tests."""
    return [{"day": d, "close": c, "provider": provider} for d, c in pairs]


def test_backtest_uses_the_sentiment_day_close_and_only_later_prices():
    """Returns start at the sentiment day's close and use only later prices."""
    sent = [{"date": "2026-10-20", "label": "Bull", "score": 0.3}]
    daily = closes(("2026-10-19", 50.0), ("2026-10-20", 100.0), ("2026-10-21", 110.0),
                   ("2026-10-22", 121.0), ("2026-10-23", 133.1))
    obs = backtest.build_observations(sent, daily, today=date(2026, 10, 30))
    assert obs[0]["next_day"] == pytest.approx(10.0)
    assert obs[0]["three_day"] == pytest.approx(33.1)                       # the 10-19 close (50) is never used


def test_backtest_skips_weekends_without_inventing_days():
    """Weekend gaps use the next available closes."""
    sent = [{"date": "2026-10-23", "label": "Bear", "score": -0.3}]        # a Friday
    daily = closes(("2026-10-23", 100.0), ("2026-10-26", 98.0), ("2026-10-27", 97.0), ("2026-10-28", 96.0))
    obs = backtest.build_observations(sent, daily, today=date(2026, 11, 1))
    assert obs[0]["next_day"] == pytest.approx(-2.0) and obs[0]["three_day"] == pytest.approx(-4.0)


def test_backtest_drops_incomplete_far_or_mixed_provider_outcomes_and_today():
    """Outcomes that are too far away, from another provider, or not final yet are dropped."""
    sent = [{"date": "2026-10-01", "label": "Bull", "score": 0.3}, {"date": "2026-10-21", "label": "Bull", "score": 0.3},
            {"date": "2026-10-30", "label": "Bull", "score": 0.3}]
    daily = closes(("2026-10-01", 100.0)) +         [{"day": "2026-10-21", "close": 100.0, "provider": "yahoo"}, {"day": "2026-10-22", "close": 90.0, "provider": "eia"}]
    obs = {o["date"]: o for o in backtest.build_observations(sent, daily, today=date(2026, 10, 30))}
    assert obs["2026-10-01"]["next_day"] is None        # the next close is 20 days later
    assert obs["2026-10-21"]["next_day"] is None        # EIA spot vs futures: not comparable
    assert "2026-10-30" not in obs                      # today is not final


def test_backtest_insufficient_below_thirty_then_stats():
    """Under 30 observations gives 'insufficient'; at 30 the correlation and hit rates appear."""
    obs = [{"date": f"d{i}", "label": "Bull" if i % 2 else "Bear", "score": 0.4 if i % 2 else -0.4,
            "next_day": 1.0 if i % 2 else -1.0, "three_day": None} for i in range(29)]
    assert backtest.summarize(obs, "next_day") == {"insufficient": True, "n": 29, "needed": 30}
    obs.append({"date": "d29", "label": "Bull", "score": 0.4, "next_day": 1.0, "three_day": None})
    res = backtest.summarize(obs, "next_day")
    assert res["n"] == 30 and res["correlation"] == pytest.approx(1.0)
    assert res["by_label"]["Bull"]["hit_rate"] == 1.0 and res["by_label"]["Bear"]["hit_rate"] == 1.0
    assert res["by_label"]["Neutral"] == {"n": 0, "hits": 0, "hit_rate": None}
    assert backtest.summarize(obs, "three_day")["insufficient"] is True


def test_backtest_runs_on_stored_data_and_says_insufficient(db):
    """The backtest runs against the database and reports insufficient data."""
    storage.save_sentiment("2026-10-20", {"label": "Bull", "score": 0.3, "n": 5, "counts": {}}, {})
    result = backtest.run_backtest(today=date(2026, 10, 30))
    assert result["results"]["next_day"]["insufficient"] is True


# ---- key points and the day-over-day diff ---------------------------------

def kp(day, stories, label="Neutral", score=0.0, prices=None):
    """A key-points dict for the diff tests."""
    return {"date": day, "top_stories": [{"title": t, "source": "s", "magnitude": 4, "direction": "Bullish"}
                                         for t in stories],
            "sentiment": {"label": label, "score": score, "n": 5}, "prices": prices or {}}


def test_diff_finds_new_dropped_sentiment_and_price_changes():
    """The diff lists new and dropped stories, the sentiment change and price changes, and renders them as text."""
    y = kp("2026-10-25", [UKRAINE[0], "Saudi Arabia intercepts missile near Red Sea oil hub"], "Neutral", 0.05,
           {"BRENT": {"price": 80.0, "as_of": "t1"}, "WTI": {"price": 76.0, "as_of": "t1"}})
    t = kp("2026-10-26", [UKRAINE[1], "OPEC+ weighs December output increase, delegates say"], "Bull", 0.25,
           {"BRENT": {"price": 82.0, "as_of": "t2"}, "WTI": {"price": 76.0, "as_of": "t1"}, "GOLD": {"price": 4000.0, "as_of": "t2"}})
    d = key_points.diff_key_points(t, y)
    assert [s["title"] for s in d["new_stories"]] == ["OPEC+ weighs December output increase, delegates say"]
    assert [s["title"] for s in d["dropped_stories"]] == ["Saudi Arabia intercepts missile near Red Sea oil hub"]
    assert d["sentiment"] == {"from": "Neutral", "to": "Bull", "from_score": 0.05, "to_score": 0.25, "score_change": 0.2}
    assert d["prices"]["BRENT"]["change_pct"] == 2.5 and not d["prices"]["BRENT"]["unchanged_quote"]
    assert d["prices"]["WTI"]["unchanged_quote"] is True
    assert "GOLD" not in d["prices"]                                         # no yesterday quote to compare
    text = key_points.diff_text(d, t, y)
    assert "New top stories: OPEC+ weighs" in text and "Neutral (score +0.05) -> Bull (score +0.25)" in text
    assert "WTI unchanged" in text and "BRENT 80 -> 82 (+2.50%)" in text


def test_diff_without_yesterday_says_so():
    """With no yesterday the diff is unavailable and the text says so."""
    d = key_points.diff_key_points(kp("2026-10-26", ["x"]), None)
    assert d == {"available": False}
    assert key_points.diff_text(d, None, None) == key_points.UNAVAILABLE_TEXT


def test_previous_key_points_needs_a_stored_brief_with_key_points(db):
    """Yesterday's key points exist only if its brief was stored with them."""
    assert key_points.previous_key_points(date(2026, 10, 26), storage.brief_for_date) is None
    storage.save_brief("2026-10-25", "published", 0.95, "text", {})                    # old brief, no key points
    assert key_points.previous_key_points(date(2026, 10, 26), storage.brief_for_date) is None
    storage.save_brief("2026-10-25", "published", 0.95, "text", {}, key_points=kp("2026-10-25", ["x"]))
    assert key_points.previous_key_points(date(2026, 10, 26), storage.brief_for_date)["date"] == "2026-10-25"


def _run_eval(monkeypatch, seen):
    """Stub the pipeline pieces so run_evaluation can run offline and record what it passes on."""
    monkeypatch.setattr(run_evaluation.data_sources, "refresh_eia_weekly", lambda: None)
    monkeypatch.setattr(run_evaluation.data_sources, "fetch_eia_data", lambda: {})
    monkeypatch.setattr(run_evaluation.sentiment_analysis, "current_sentiment",
                        lambda: {"label": "Bull", "score": 0.2, "n": 1, "counts": {}})
    monkeypatch.setattr(run_evaluation.rag_pipeline, "history_context", lambda q: "")

    def fake_generate(articles, sentiment, eia, history, brief_date, **kw):
        """Fake generate_brief: capture the keyword arguments and return a small brief."""
        seen["kw"] = kw
        return "# t\n\n" + (f"## What changed since yesterday\n\n{kw['changes_missing_text']}\n\n" if kw["changes_missing_text"] else "") + "body"

    monkeypatch.setattr(run_evaluation.llm_pipeline, "generate_brief", fake_generate)
    monkeypatch.setattr(run_evaluation.brief_evaluation, "evaluate_brief",
                        lambda brief, articles, context: seen.update(context=context) or
                        {"passed": True, "groundedness": 1.0, "reasons": []})


def test_run_evaluation_with_no_yesterday_states_it_and_stores_key_points(db, monkeypatch):
    """Without yesterday the brief says so, and today's key points are stored."""
    storage.save_articles([article("a", "Oil rises on supply fears", "reuters.com", collected_at=storage.utcnow())])
    seen = {}
    _run_eval(monkeypatch, seen)
    assert run_evaluation.run_evaluation() == "published"
    assert seen["kw"]["changes_text"] == "" and seen["kw"]["changes_missing_text"] == key_points.UNAVAILABLE_TEXT
    assert key_points.UNAVAILABLE_TEXT in seen["context"]                    # trusted reference for the evaluator
    brief = storage.latest_brief()
    assert key_points.UNAVAILABLE_TEXT in brief["text"]
    assert json.loads(brief["key_points_json"])["top_stories"][0]["title"] == "Oil rises on supply fears"


def test_run_evaluation_with_yesterday_passes_the_diff(db, monkeypatch):
    """With yesterday's key points the diff goes to the generator and the evaluator."""
    today = datetime.fromisoformat(storage.utcnow()).date()
    storage.save_brief((today - timedelta(days=1)).isoformat(), "published", 0.95, "old", {},
                       key_points=kp((today - timedelta(days=1)).isoformat(), ["An older story about gas"], "Bear", -0.3))
    storage.save_articles([article("a", "Oil rises on supply fears", "reuters.com", collected_at=storage.utcnow())])
    seen = {}
    _run_eval(monkeypatch, seen)
    run_evaluation.run_evaluation()
    assert "Changes since yesterday" in seen["kw"]["changes_text"] and seen["kw"]["changes_missing_text"] == ""
    assert "Bear (score -0.30)" in seen["context"]


# ---- analyst feedback -----------------------------------------------------

def test_add_feedback_validates_and_caps(db):
    """Invalid feedback is rejected and long comments are truncated."""
    with pytest.raises(ValueError):
        storage.add_feedback("nonsense", "1", "up")
    with pytest.raises(ValueError):
        storage.add_feedback("article_tag", "1", "meh")
    with pytest.raises(ValueError):
        storage.add_feedback("article_tag", "1", "up", analyst_label="Sideways")
    storage.add_feedback("article_tag", "1", "down", "x" * 5000, "Bearish")
    row = storage.all_feedback()[0]
    assert len(row["comment"]) == storage.MAX_COMMENT_CHARS and row["analyst_label"] == "Bearish"


def _tagged(db_url, direction):
    """Store an article with a direction and return its id."""
    storage.save_articles([article(db_url, f"Headline {db_url}", direction=direction)])
    return storage._rows("SELECT id FROM articles WHERE url = ?", (db_url,))[0]["id"]


def test_golden_export_and_agreement_report(db, tmp_path):
    """The CSV export and the agreement report count agree, disagree and uncorrected feedback correctly."""
    a1, a2, a3, a4 = (_tagged("u1", "Bullish"), _tagged("u2", "Bullish"), _tagged("u3", "Bearish"), _tagged("u4", "Neutral"))
    storage.add_feedback("article_tag", a1, "up")                              # agree, no correction
    storage.add_feedback("article_tag", a2, "down", "wrong", "Bearish")        # disagree, corrected
    storage.add_feedback("article_tag", a3, "down", "", None)                  # down, no correction
    storage.add_feedback("article_tag", a4, "down", "", "Mixed")
    storage.add_feedback("article_tag", a4, "up", "changed my mind")           # latest feedback per article wins
    storage.add_feedback("brief_section", "2026-10-26#Headline view", "up", "good")
    path = tmp_path / "golden.csv"
    assert feedback_tools.export_golden_csv(path) == 6
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    first = next(r for r in rows if r["item_id"] == str(a2))
    assert first["model_direction"] == "Bullish" and first["analyst_label"] == "Bearish" and first["article_title"]
    assert any(r["item_type"] == "brief_section" and r["article_title"] == "" for r in rows)
    report = feedback_tools.agreement_report()
    assert (report["judged"], report["agree"], report["disagree"]) == (3, 2, 1)
    assert report["agreement_rate"] == pytest.approx(0.667, abs=1e-3)
    assert report["thumbs_down_without_correction"] == 1
    assert report["confusion"] == {"Bullish -> Bearish": 1, "Bullish -> Bullish": 1, "Neutral -> Neutral": 1}


def test_dashboard_feedback_form_inserts_a_row(db):
    """Submitting the dashboard form stores one feedback row."""
    storage.save_articles([article("fb1", "Oil rises on supply fears", "reuters.com", collected_at=storage.utcnow())])
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60).run()
    assert not at.exception
    at.radio(key="fb_rating").set_value("Disagree / not useful")
    at.selectbox(key="fb_label").set_value("Bearish")
    [b for b in at.button if b.label == "Save feedback"][0].click()
    at.run()
    assert not at.exception
    row = storage.all_feedback()[0]
    assert (row["item_type"], row["rating"], row["analyst_label"]) == ("article_tag", "down", "Bearish")


def test_dashboard_shows_one_row_per_story_with_outlet_count(db):
    """The verified feed shows one row per story with its outlet count."""
    storage.save_articles([article(f"u{i}", t, f"outlet{i}.com", collected_at=storage.utcnow())
                           for i, t in enumerate(UKRAINE)])
    story_cluster.cluster_new_articles()
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60).run()
    assert not at.exception
    feed = next(d.value for d in at.dataframe if "reported by" in d.value)
    assert len(feed) == 1 and feed["reported by"].iloc[0] == "3 outlets"


def test_research_page_renders_with_insufficient_data(db):
    """The Research page renders and says 'Insufficient data'."""
    at = AppTest.from_file(str(ROOT / "pages" / "1_Research.py"), default_timeout=60).run()
    assert not at.exception
    assert any("Insufficient data" in i.value for i in at.info)
    assert any("not predictive" in w.value for w in at.warning)
