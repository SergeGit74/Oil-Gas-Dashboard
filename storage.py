"""Shared store between the scheduler and the dashboard: SQLite by default, PostgreSQL when DATABASE_URL is set.

All SQL here is written once, in a dialect both backends accept (see db.py): `?` placeholders, ON CONFLICT upserts
and portable queries. The schema lives in migrations.py.
"""
import json
from datetime import datetime, timedelta, timezone

import config
import db
import migrations

connect = db.connect


def utcnow():
    """Current UTC time as an ISO string (seconds)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hours_ago(hours):
    """UTC ISO time `hours` ago."""
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


def init_db():
    """Bring the schema up to date. A no-op (one small read) when every migration is already applied."""
    with connect() as conn:
        if {v for v, _, _ in migrations.MIGRATIONS} <= migrations.applied_versions(conn):
            return
        migrations.migrate(conn)


def _rows(sql, params=()):
    """Run a query and return all rows as dicts."""
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params)]


# ---- articles -------------------------------------------------------------

def known_urls(urls):
    """Which of the given URLs are already stored."""
    urls = list(urls)
    found = set()
    with connect() as conn:
        for i in range(0, len(urls), 500):
            chunk = urls[i:i + 500]
            marks = ",".join("?" * len(chunk))
            found.update(r["url"] for r in conn.execute(f"SELECT url FROM articles WHERE url IN ({marks})", chunk))
    return found


def save_articles(rows):
    """Insert new articles; a URL already stored is ignored."""
    cols = ["url", "title", "source", "snippet", "published_at", "collected_at", "category", "direction",
            "magnitude", "confidence", "source_tier", "content_type", "corroboration", "reason",
            "flagged_injection", "corroboration_json"]
    sql = db.upsert_sql("articles", cols)  # a URL already stored is ignored
    with connect() as conn:
        conn.executemany(sql, [tuple(r.get(c) for c in cols) for r in rows])


def verified_articles(since_iso, directional_only=False, limit=None):
    """Verified articles collected since a time (confidence at the threshold, not injection-flagged), highest impact first."""
    sql = ("SELECT * FROM articles WHERE collected_at >= ? AND flagged_injection = 0 AND confidence >= ?")
    if directional_only:
        sql += " AND direction IS NOT NULL"
    sql += " ORDER BY magnitude DESC NULLS LAST, collected_at DESC"
    params = [since_iso, config.VERIFIED_MIN_CONFIDENCE]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return _rows(sql, params)


def unverified_articles(since_iso, limit=50):
    """Articles kept out of the pipeline: injection-flagged or low confidence, newest first."""
    return _rows(
        "SELECT * FROM articles WHERE collected_at >= ? AND (flagged_injection = 1 OR confidence IS NULL "
        "OR confidence < ?) ORDER BY collected_at DESC LIMIT ?",
        (since_iso, config.VERIFIED_MIN_CONFIDENCE, limit))


def articles_needing_cluster(since_iso):
    """Articles collected since `since_iso` that have no cluster yet, in id order."""
    return _rows("SELECT id, title, collected_at, cluster_id FROM articles WHERE cluster_id IS NULL "
                 "AND collected_at >= ? ORDER BY id", (since_iso,))


def clustered_articles_since(since_iso):
    """Articles collected since a time that already have a cluster, in id order."""
    return _rows("SELECT id, title, collected_at, cluster_id FROM articles WHERE cluster_id IS NOT NULL "
                 "AND collected_at >= ? ORDER BY id", (since_iso,))


def set_clusters(assigned):
    """assigned: {article_id: cluster_id}. Only fills rows that have no cluster, so it never rewrites one."""
    with connect() as conn:
        conn.executemany("UPDATE articles SET cluster_id = ? WHERE id = ? AND cluster_id IS NULL",
                         [(c, i) for i, c in assigned.items()])


def cluster_members(cluster_ids):
    """Basic fields of every article in the given clusters."""
    cluster_ids = list(cluster_ids)
    if not cluster_ids:
        return []
    marks = ",".join("?" * len(cluster_ids))
    return _rows(f"SELECT id, cluster_id, source, confidence, flagged_injection FROM articles "
                 f"WHERE cluster_id IN ({marks})", cluster_ids)


def unindexed_verified():
    """Verified articles not yet embedded into the vector store."""
    return _rows("SELECT * FROM articles WHERE indexed = 0 AND flagged_injection = 0 AND confidence >= ?",
                 (config.VERIFIED_MIN_CONFIDENCE,))


def mark_indexed(ids):
    """Mark articles as embedded."""
    with connect() as conn:
        conn.executemany("UPDATE articles SET indexed = 1 WHERE id = ?", [(i,) for i in ids])


# ---- briefs ---------------------------------------------------------------

def save_brief(date, status, groundedness, text, evaluation, key_points=None, sources=None):
    """sources: the numbered source list the brief cites, [{"n", "title", "source", "url"}, ...]."""
    with connect() as conn:
        conn.execute(
            db.upsert_sql("briefs", ["date", "created_at", "status", "groundedness", "text", "eval_json",
                                     "key_points_json", "sources_json"], ["date"]),
            (date, utcnow(), status, groundedness, text, json.dumps(evaluation),
             json.dumps(key_points) if key_points is not None else None,
             json.dumps(sources) if sources is not None else None))


def brief_for_date(date):
    """The brief row for a date, or None."""
    rows = _rows("SELECT * FROM briefs WHERE date = ?", (date,))
    return rows[0] if rows else None


def latest_brief():
    """The most recent brief row, or None."""
    rows = _rows("SELECT * FROM briefs ORDER BY date DESC LIMIT 1")
    return rows[0] if rows else None


# ---- daily run claims -----------------------------------------------------

def claim_daily_run(job, day, max_attempts=2, cooldown_min=30, now=None):
    """Try to claim one attempt of `job` for `day`. True means the caller may run it now.

    One atomic UPDATE: it succeeds only while attempts < max_attempts and the last attempt is older than the
    cooldown, so two concurrent claimers cannot both win, a failing job is retried at most max_attempts times a
    day, and retries are spaced by the cooldown."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(minutes=cooldown_min)).isoformat(timespec="seconds")
    stamp = now.isoformat(timespec="seconds")
    with connect() as conn:
        conn.execute("INSERT INTO run_claims (job, day, attempts, last_attempt) VALUES (?,?,0,NULL) "
                     "ON CONFLICT DO NOTHING", (job, day))
        cur = conn.execute(
            "UPDATE run_claims SET attempts = attempts + 1, last_attempt = ? "
            "WHERE job = ? AND day = ? AND attempts < ? AND (last_attempt IS NULL OR last_attempt < ?)",
            (stamp, job, day, max_attempts, cutoff))
        return cur.rowcount == 1


# ---- market prices --------------------------------------------------------

def save_ticks(rows):
    """Insert fetched price quotes."""
    cols = ["indicator_key", "ts", "price", "prev_close", "change_pct", "provider", "delayed_minutes",
            "currency", "fetched_at"]
    with connect() as conn:
        conn.executemany(f"INSERT INTO price_ticks ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                         [tuple(r.get(c) for c in cols) for r in rows])


def latest_ticks():
    """Newest tick (by quote time) per indicator, as {indicator_key: row}."""
    rows = _rows("SELECT * FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY indicator_key "
                 "ORDER BY ts DESC, id DESC) AS rn FROM price_ticks) AS ranked WHERE rn = 1")
    return {r["indicator_key"]: r for r in rows}


def last_fetch_times():
    """{indicator_key: ISO time of the most recent successful fetch}. Used to decide what is due."""
    return {r["indicator_key"]: r["t"] for r in
            _rows("SELECT indicator_key, MAX(fetched_at) AS t FROM price_ticks GROUP BY indicator_key")}


def price_series(indicator_key, since_iso):
    # One point per quote time (the newest row if several providers quoted the same instant).
    """(ts, price) points since a time, one per quote time, oldest first."""
    return _rows("SELECT ts, price FROM price_ticks WHERE id IN (SELECT MAX(id) FROM price_ticks "
                 "WHERE indicator_key = ? AND ts >= ? GROUP BY ts) ORDER BY ts", (indicator_key, since_iso))


def latest_daily(indicator_key, limit=30):
    """Daily price rows for an indicator, newest first."""
    return _rows("SELECT * FROM price_daily WHERE indicator_key = ? ORDER BY day DESC LIMIT ?",
                 (indicator_key, limit))


def purge_ticks(retention_days=30, now=None):
    """Roll every day still held as ticks into price_daily, then delete ticks of days older than the retention.

    Only whole days are deleted (the cutoff is a UTC midnight), so a day is never rolled up from a partial set
    after its first roll-up. Safe to run repeatedly. Returns (days_rolled_up, ticks_deleted)."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=retention_days)).date().isoformat()
    days = {}
    with connect() as conn:
        for r in conn.execute("SELECT indicator_key, ts, price, provider FROM price_ticks ORDER BY ts, id"):
            days.setdefault((r["indicator_key"], r["ts"][:10]), []).append(r)
        for (key, day), ticks in days.items():
            prices = [t["price"] for t in ticks]
            conn.execute(
                db.upsert_sql("price_daily", ["indicator_key", "day", "open", "high", "low", "close", "n_ticks",
                                              "provider"], ["indicator_key", "day"]),
                (key, day, prices[0], max(prices), min(prices), prices[-1], len(prices), ticks[-1]["provider"]))
        deleted = conn.execute("DELETE FROM price_ticks WHERE substr(ts,1,10) < ?", (cutoff,)).rowcount
    return len(days), deleted


# ---- EIA weekly series ----------------------------------------------------

def save_eia_weekly(series_key, rows, unit):
    """rows: [(period, value), ...]. Upserts, so a revised value replaces the old one."""
    now = utcnow()
    with connect() as conn:
        conn.executemany(db.upsert_sql("eia_weekly", ["series_key", "period", "value", "unit", "fetched_at"],
                                       ["series_key", "period"]), [(series_key, p, v, unit, now) for p, v in rows])


def eia_weekly(series_key, limit=5):
    """Newest first: [{period, value, unit, fetched_at}, ...]."""
    return _rows("SELECT period, value, unit, fetched_at FROM eia_weekly WHERE series_key = ? "
                 "ORDER BY period DESC LIMIT ?", (series_key, limit))


def last_eia_weekly_fetch():
    """ISO time of the newest weekly EIA fetch, or None."""
    rows = _rows("SELECT MAX(fetched_at) AS t FROM eia_weekly")
    return rows[0]["t"]


# ---- events ---------------------------------------------------------------

def upsert_events(rows):
    """Insert new events, ignore ones already present (same date, time and title). Never deletes."""
    cols = ["date", "time", "tz", "title", "category", "source_url", "source"]
    with connect() as conn:
        conn.executemany(db.upsert_sql("events", cols),
                         [tuple(r.get(c) for c in cols) for r in rows])


def replace_future_events(source, from_date, rows):
    """For events regenerated from an external schedule: swap that source's rows dated from `from_date` on.
    One transaction, so a failed insert leaves the old rows in place."""
    cols = ["date", "time", "tz", "title", "category", "source_url", "source"]
    with connect() as conn:
        conn.execute("DELETE FROM events WHERE source = ? AND date >= ?", (source, from_date))
        conn.executemany(db.upsert_sql("events", cols),
                         [tuple({**r, "source": source}.get(c) for c in cols) for r in rows])


def events_between(start_date, end_date):
    """Events with start_date <= date <= end_date (ISO dates), earliest first; undated times sort first."""
    return _rows("SELECT * FROM events WHERE date >= ? AND date <= ? ORDER BY date, COALESCE(time, ''), title",
                 (start_date, end_date))


# ---- freshness ------------------------------------------------------------

def last_success_time(job_names):
    """ISO time of the newest 'success' job_runs row among these jobs, or None."""
    marks = ",".join("?" * len(job_names))
    rows = _rows(f"SELECT MAX(created_at) AS t FROM job_runs WHERE status = 'success' AND job_name IN ({marks})",
                 tuple(job_names))
    return rows[0]["t"]


def last_price_fetch():
    """ISO time of the newest price fetch, or None."""
    rows = _rows("SELECT MAX(fetched_at) AS t FROM price_ticks")
    return rows[0]["t"]


# ---- API quota ------------------------------------------------------------

def reserve_quota(provider, day_utc, credits, cap):
    """Atomically add `credits` to today's count unless that would exceed `cap`. True if reserved."""
    with connect() as conn:
        conn.execute("INSERT INTO api_quota (provider, day_utc, calls) VALUES (?,?,0) ON CONFLICT DO NOTHING",
                     (provider, day_utc))
        cur = conn.execute("UPDATE api_quota SET calls = calls + ? WHERE provider = ? AND day_utc = ? "
                           "AND calls + ? <= ?", (credits, provider, day_utc, credits, cap))
        return cur.rowcount == 1


def quota_used(provider, day_utc):
    """Credits used by a provider on a UTC day."""
    rows = _rows("SELECT calls FROM api_quota WHERE provider = ? AND day_utc = ?", (provider, day_utc))
    return rows[0]["calls"] if rows else 0


# ---- alerts and email delivery -------------------------------------------

def articles_collected_at(collected_at):
    """Articles saved with exactly this collected_at (one collection cycle shares a single timestamp)."""
    return _rows("SELECT * FROM articles WHERE collected_at = ?", (collected_at,))


def record_alert(cluster_key, article, status, now=None):
    """Insert the alert row for a story. False means this story already has one, so nobody may alert it again."""
    with connect() as conn:
        cur = conn.execute(
            db.upsert_sql("alert_log", ["cluster_key", "article_id", "title", "source", "magnitude", "direction",
                                        "status", "created_at"]),
            (cluster_key, article["id"], article["title"], article["source"], article["magnitude"],
             article["direction"], status, now or utcnow()))
        return cur.rowcount == 1


def set_alert_status(cluster_key, status):
    """Update the status of a story's alert row."""
    with connect() as conn:
        conn.execute("UPDATE alert_log SET status = ? WHERE cluster_key = ?", (status, cluster_key))


def alerts_in_last_hour(now=None):
    """Alerts sent (or being sent) in the last hour, for the hourly cap."""
    since = ((now or datetime.now(timezone.utc)) - timedelta(hours=1)).isoformat(timespec="seconds")
    rows = _rows("SELECT COUNT(*) AS n FROM alert_log WHERE status IN ('sent', 'pending') AND created_at >= ?", (since,))
    return rows[0]["n"]


def recent_alerts(limit=20):
    """The newest alert_log rows."""
    return _rows("SELECT * FROM alert_log ORDER BY id DESC LIMIT ?", (limit,))


def email_sent(day, kind):
    """Whether this kind of email was already sent for a day."""
    return bool(_rows("SELECT 1 FROM email_log WHERE day = ? AND kind = ?", (day, kind)))


def record_email_sent(day, kind, recipients):
    """Mark an email kind as sent for a day."""
    with connect() as conn:
        conn.execute(db.upsert_sql("email_log", ["day", "kind", "recipients", "sent_at"], ["day", "kind"]),
                     (day, kind, recipients, utcnow()))


# ---- analyst feedback -----------------------------------------------------
FEEDBACK_TYPES = ("article_tag", "brief_section")
FEEDBACK_RATINGS = ("up", "down")
FEEDBACK_LABELS = ("Bullish", "Bearish", "Neutral", "Mixed")
MAX_COMMENT_CHARS = 1000


def add_feedback(item_type, item_id, rating, comment="", analyst_label=None):
    """The dashboard's only write: a plain insert, validated and length-capped."""
    if item_type not in FEEDBACK_TYPES or rating not in FEEDBACK_RATINGS:
        raise ValueError("invalid feedback type or rating")
    if analyst_label not in (None, "") and analyst_label not in FEEDBACK_LABELS:
        raise ValueError("invalid analyst label")
    with connect() as conn:
        conn.execute("INSERT INTO feedback (item_type, item_id, rating, comment, created_at, analyst_label) "
                     "VALUES (?,?,?,?,?,?)",
                     (item_type, str(item_id)[:200], rating, (comment or "")[:MAX_COMMENT_CHARS], utcnow(),
                      analyst_label or None))


def all_feedback():
    """All analyst feedback, oldest first."""
    return _rows("SELECT * FROM feedback ORDER BY id")


def article_by_id(article_id):
    """One article row by id, or None."""
    rows = _rows("SELECT * FROM articles WHERE id = ?", (article_id,))
    return rows[0] if rows else None


# ---- job runs -------------------------------------------------------------

def log_job(job_name, status, detail=""):
    """Record a job or source status in job_runs (detail is truncated)."""
    with connect() as conn:
        conn.execute("INSERT INTO job_runs (job_name, status, detail, created_at) VALUES (?,?,?,?)",
                     (job_name, status, str(detail)[:2000], utcnow()))


def recent_job_runs(job_name, limit=10):
    """The newest job_runs rows for one job."""
    return _rows("SELECT * FROM job_runs WHERE job_name = ? ORDER BY id DESC LIMIT ?", (job_name, limit))


def latest_job_runs(limit=30):
    """The newest job_runs rows across all jobs."""
    return _rows("SELECT * FROM job_runs ORDER BY id DESC LIMIT ?", (limit,))


# ---- sentiment ------------------------------------------------------------

def save_sentiment(date, sentiment, price):
    """Save (or replace) a day's sentiment and price-check snapshot."""
    with connect() as conn:
        conn.execute(
            db.upsert_sql("sentiment_history", ["date", "label", "score", "n_articles", "counts_json",
                                                "price_benchmark", "price_value", "price_change_pct",
                                                "confirmation", "updated_at"], ["date"]),
            (date, sentiment["label"], sentiment["score"], sentiment["n"], json.dumps(sentiment["counts"]),
             price.get("benchmark"), price.get("price"), price.get("change_pct"), price.get("status"), utcnow()))


def sentiment_history(limit=30):
    """The newest daily sentiment rows."""
    return _rows("SELECT * FROM sentiment_history ORDER BY date DESC LIMIT ?", (limit,))


# ---- costs ----------------------------------------------------------------

def save_run_cost(rec):
    """Save one finished run's cost record."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO run_costs (job_name, started_at, finished_at, status, claude_usd, apify_usd, serper_usd, "
            "total_usd, input_tokens, output_tokens, serper_credits, details_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec["job_name"], rec["started_at"], rec["finished_at"], rec["status"], rec["claude_usd"],
             rec["apify_usd"], rec["serper_usd"], rec["total_usd"], rec["input_tokens"], rec["output_tokens"],
             rec["serper_credits"], json.dumps(rec["details"])))


def latest_marker():
    """Id of the newest finished run (any job except 'ask the desk'). Changes whenever a cycle completes,
    so the dashboard can tell cheaply that there is new data."""
    rows = _rows("SELECT MAX(id) AS m FROM run_costs WHERE job_name NOT LIKE 'ask_desk%' AND job_name != 'price_poll'")
    return rows[0]["m"] or 0


def last_cycle_finished():
    """ISO time the latest scheduled collection cycle (local or deployment mode) finished, or None."""
    rows = _rows("SELECT finished_at FROM run_costs WHERE job_name IN ('collection_cycle', 'deployment_cycle') "
                 "ORDER BY id DESC LIMIT 1")
    return rows[0]["finished_at"] if rows else None


def recent_run_costs(limit=20):
    # The price poller records a zero-cost run every few minutes; leave it out so real runs stay visible.
    """The newest run_costs rows, leaving out the zero-cost price polls."""
    return _rows("SELECT * FROM run_costs WHERE job_name != 'price_poll' ORDER BY id DESC LIMIT ?", (limit,))


def daily_costs(days=14):
    """Per-UTC-day totals by service."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    return _rows(
        "SELECT substr(started_at,1,10) AS day, SUM(claude_usd) AS claude_usd, SUM(apify_usd) AS apify_usd, "
        "SUM(serper_usd) AS serper_usd, SUM(total_usd) AS total_usd FROM run_costs "
        "WHERE substr(started_at,1,10) >= ? GROUP BY day ORDER BY day", (since,))


if __name__ == "__main__":
    init_db()

init_db()
