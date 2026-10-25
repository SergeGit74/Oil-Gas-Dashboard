"""SQLite shared store between the scheduler and the dashboard."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT UNIQUE NOT NULL,
    title TEXT,
    source TEXT,
    snippet TEXT,
    published_at TEXT,
    collected_at TEXT,
    category TEXT,
    direction TEXT,
    magnitude INTEGER,
    confidence REAL,
    source_tier TEXT,
    content_type TEXT,
    corroboration INTEGER DEFAULT 0,
    reason TEXT,
    flagged_injection INTEGER DEFAULT 0,
    indexed INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_articles_collected ON articles(collected_at);

CREATE TABLE IF NOT EXISTS alert_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_key TEXT NOT NULL UNIQUE,
    article_id INTEGER,
    title TEXT,
    source TEXT,
    magnitude INTEGER,
    direction TEXT,
    status TEXT NOT NULL,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS email_log (
    day TEXT NOT NULL,
    kind TEXT NOT NULL,
    recipients INTEGER,
    sent_at TEXT,
    PRIMARY KEY (day, kind)
);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_type TEXT NOT NULL,
    item_id TEXT NOT NULL,
    rating TEXT NOT NULL,
    comment TEXT,
    created_at TEXT,
    analyst_label TEXT
);
CREATE INDEX IF NOT EXISTS idx_feedback_item ON feedback(item_type, item_id);

CREATE TABLE IF NOT EXISTS briefs (
    date TEXT PRIMARY KEY,
    created_at TEXT,
    status TEXT,
    groundedness REAL,
    text TEXT,
    eval_json TEXT
);

CREATE TABLE IF NOT EXISTS sentiment_history (
    date TEXT PRIMARY KEY,
    label TEXT,
    score REAL,
    n_articles INTEGER,
    counts_json TEXT,
    price_benchmark TEXT,
    price_value REAL,
    price_change_pct REAL,
    confirmation TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS job_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_name TEXT,
    status TEXT,
    detail TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_runs_name ON job_runs(job_name, id);

CREATE TABLE IF NOT EXISTS run_costs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_name TEXT,
    started_at TEXT,
    finished_at TEXT,
    status TEXT,
    claude_usd REAL,
    apify_usd REAL,
    serper_usd REAL,
    total_usd REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    serper_credits INTEGER,
    details_json TEXT
);

CREATE TABLE IF NOT EXISTS run_claims (
    job TEXT NOT NULL,
    day TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_attempt TEXT,
    PRIMARY KEY (job, day)
);

CREATE TABLE IF NOT EXISTS price_ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    indicator_key TEXT NOT NULL,
    ts TEXT NOT NULL,
    price REAL NOT NULL,
    prev_close REAL,
    change_pct REAL,
    provider TEXT,
    delayed_minutes INTEGER,
    currency TEXT,
    fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_price_ticks_key_ts ON price_ticks(indicator_key, ts);

CREATE TABLE IF NOT EXISTS price_daily (
    indicator_key TEXT NOT NULL,
    day TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    n_ticks INTEGER,
    provider TEXT,
    PRIMARY KEY (indicator_key, day)
);

CREATE TABLE IF NOT EXISTS eia_weekly (
    series_key TEXT NOT NULL,
    period TEXT NOT NULL,
    value REAL NOT NULL,
    unit TEXT,
    fetched_at TEXT,
    PRIMARY KEY (series_key, period)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    time TEXT,
    tz TEXT,
    title TEXT NOT NULL,
    category TEXT,
    source_url TEXT,
    source TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_date ON events(date);
CREATE UNIQUE INDEX IF NOT EXISTS ux_events ON events(date, COALESCE(time, ''), title);

CREATE TABLE IF NOT EXISTS api_quota (
    provider TEXT NOT NULL,
    day_utc TEXT NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (provider, day_utc)
);
"""

# Columns added after the first schema version; added to older databases by init_db().
MIGRATIONS = {
    "articles": {
        "direction": "TEXT", "magnitude": "INTEGER", "corroboration": "INTEGER DEFAULT 0",
        "flagged_injection": "INTEGER DEFAULT 0", "indexed": "INTEGER DEFAULT 0",
        "content_type": "TEXT", "source_tier": "TEXT",
        "cluster_id": "INTEGER", "corroboration_json": "TEXT",
    },
    "briefs": {"key_points_json": "TEXT", "sources_json": "TEXT"},
    "sentiment_history": {"price_value": "REAL"},
}


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hours_ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


@contextmanager
def connect():
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with connect() as conn:
        conn.executescript(SCHEMA)
        for table, columns in MIGRATIONS.items():
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for col, decl in columns.items():
                if col not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_cluster ON articles(cluster_id)")


def _rows(sql, params=()):
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params)]


# ---- articles -------------------------------------------------------------

def known_urls(urls):
    urls = list(urls)
    found = set()
    with connect() as conn:
        for i in range(0, len(urls), 500):
            chunk = urls[i:i + 500]
            marks = ",".join("?" * len(chunk))
            found.update(r["url"] for r in conn.execute(f"SELECT url FROM articles WHERE url IN ({marks})", chunk))
    return found


def save_articles(rows):
    cols = ["url", "title", "source", "snippet", "published_at", "collected_at", "category", "direction",
            "magnitude", "confidence", "source_tier", "content_type", "corroboration", "reason",
            "flagged_injection", "corroboration_json"]
    sql = f"INSERT OR IGNORE INTO articles ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
    with connect() as conn:
        conn.executemany(sql, [tuple(r.get(c) for c in cols) for r in rows])


def verified_articles(since_iso, directional_only=False, limit=None):
    sql = ("SELECT * FROM articles WHERE collected_at >= ? AND flagged_injection = 0 AND confidence >= ?")
    if directional_only:
        sql += " AND direction IS NOT NULL"
    sql += " ORDER BY magnitude DESC, collected_at DESC"
    params = [since_iso, config.VERIFIED_MIN_CONFIDENCE]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return _rows(sql, params)


def unverified_articles(since_iso, limit=50):
    return _rows(
        "SELECT * FROM articles WHERE collected_at >= ? AND (flagged_injection = 1 OR confidence IS NULL "
        "OR confidence < ?) ORDER BY collected_at DESC LIMIT ?",
        (since_iso, config.VERIFIED_MIN_CONFIDENCE, limit))


def articles_needing_cluster(since_iso):
    """Articles collected since `since_iso` that have no cluster yet, in id order."""
    return _rows("SELECT id, title, collected_at, cluster_id FROM articles WHERE cluster_id IS NULL "
                 "AND collected_at >= ? ORDER BY id", (since_iso,))


def clustered_articles_since(since_iso):
    return _rows("SELECT id, title, collected_at, cluster_id FROM articles WHERE cluster_id IS NOT NULL "
                 "AND collected_at >= ? ORDER BY id", (since_iso,))


def set_clusters(assigned):
    """assigned: {article_id: cluster_id}. Only fills rows that have no cluster, so it never rewrites one."""
    with connect() as conn:
        conn.executemany("UPDATE articles SET cluster_id = ? WHERE id = ? AND cluster_id IS NULL",
                         [(c, i) for i, c in assigned.items()])


def cluster_members(cluster_ids):
    cluster_ids = list(cluster_ids)
    if not cluster_ids:
        return []
    marks = ",".join("?" * len(cluster_ids))
    return _rows(f"SELECT id, cluster_id, source, confidence, flagged_injection FROM articles "
                 f"WHERE cluster_id IN ({marks})", cluster_ids)


def unindexed_verified():
    return _rows("SELECT * FROM articles WHERE indexed = 0 AND flagged_injection = 0 AND confidence >= ?",
                 (config.VERIFIED_MIN_CONFIDENCE,))


def mark_indexed(ids):
    with connect() as conn:
        conn.executemany("UPDATE articles SET indexed = 1 WHERE id = ?", [(i,) for i in ids])


# ---- briefs ---------------------------------------------------------------

def save_brief(date, status, groundedness, text, evaluation, key_points=None, sources=None):
    """sources: the numbered source list the brief cites, [{"n", "title", "source", "url"}, ...]."""
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO briefs (date, created_at, status, groundedness, text, eval_json, key_points_json, "
            "sources_json) VALUES (?,?,?,?,?,?,?,?)",
            (date, utcnow(), status, groundedness, text, json.dumps(evaluation),
             json.dumps(key_points) if key_points is not None else None,
             json.dumps(sources) if sources is not None else None))


def brief_for_date(date):
    rows = _rows("SELECT * FROM briefs WHERE date = ?", (date,))
    return rows[0] if rows else None


def latest_brief():
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
        conn.execute("INSERT OR IGNORE INTO run_claims (job, day, attempts, last_attempt) VALUES (?,?,0,NULL)",
                     (job, day))
        cur = conn.execute(
            "UPDATE run_claims SET attempts = attempts + 1, last_attempt = ? "
            "WHERE job = ? AND day = ? AND attempts < ? AND (last_attempt IS NULL OR last_attempt < ?)",
            (stamp, job, day, max_attempts, cutoff))
        return cur.rowcount == 1


# ---- market prices --------------------------------------------------------

def save_ticks(rows):
    cols = ["indicator_key", "ts", "price", "prev_close", "change_pct", "provider", "delayed_minutes",
            "currency", "fetched_at"]
    with connect() as conn:
        conn.executemany(f"INSERT INTO price_ticks ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                         [tuple(r.get(c) for c in cols) for r in rows])


def latest_ticks():
    """Newest tick (by quote time) per indicator, as {indicator_key: row}."""
    rows = _rows("SELECT * FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY indicator_key "
                 "ORDER BY ts DESC, id DESC) AS rn FROM price_ticks) WHERE rn = 1")
    return {r["indicator_key"]: r for r in rows}


def last_fetch_times():
    """{indicator_key: ISO time of the most recent successful fetch}. Used to decide what is due."""
    return {r["indicator_key"]: r["t"] for r in
            _rows("SELECT indicator_key, MAX(fetched_at) AS t FROM price_ticks GROUP BY indicator_key")}


def price_series(indicator_key, since_iso):
    return _rows("SELECT ts, price FROM price_ticks WHERE indicator_key = ? AND ts >= ? "
                 "GROUP BY ts ORDER BY ts", (indicator_key, since_iso))


def latest_daily(indicator_key, limit=30):
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
                "INSERT OR REPLACE INTO price_daily (indicator_key, day, open, high, low, close, n_ticks, provider) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (key, day, prices[0], max(prices), min(prices), prices[-1], len(prices), ticks[-1]["provider"]))
        deleted = conn.execute("DELETE FROM price_ticks WHERE substr(ts,1,10) < ?", (cutoff,)).rowcount
    return len(days), deleted


# ---- EIA weekly series ----------------------------------------------------

def save_eia_weekly(series_key, rows, unit):
    """rows: [(period, value), ...]. Upserts, so a revised value replaces the old one."""
    now = utcnow()
    with connect() as conn:
        conn.executemany("INSERT OR REPLACE INTO eia_weekly (series_key, period, value, unit, fetched_at) "
                         "VALUES (?,?,?,?,?)", [(series_key, p, v, unit, now) for p, v in rows])


def eia_weekly(series_key, limit=5):
    """Newest first: [{period, value, unit, fetched_at}, ...]."""
    return _rows("SELECT period, value, unit, fetched_at FROM eia_weekly WHERE series_key = ? "
                 "ORDER BY period DESC LIMIT ?", (series_key, limit))


def last_eia_weekly_fetch():
    rows = _rows("SELECT MAX(fetched_at) AS t FROM eia_weekly")
    return rows[0]["t"]


# ---- events ---------------------------------------------------------------

def upsert_events(rows):
    """Insert new events, ignore ones already present (same date, time and title). Never deletes."""
    cols = ["date", "time", "tz", "title", "category", "source_url", "source"]
    with connect() as conn:
        conn.executemany(f"INSERT OR IGNORE INTO events ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                         [tuple(r.get(c) for c in cols) for r in rows])


def replace_future_events(source, from_date, rows):
    """For events regenerated from an external schedule: swap that source's rows dated from `from_date` on.
    One transaction, so a failed insert leaves the old rows in place."""
    cols = ["date", "time", "tz", "title", "category", "source_url", "source"]
    with connect() as conn:
        conn.execute("DELETE FROM events WHERE source = ? AND date >= ?", (source, from_date))
        conn.executemany(f"INSERT OR IGNORE INTO events ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
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
    rows = _rows("SELECT MAX(fetched_at) AS t FROM price_ticks")
    return rows[0]["t"]


# ---- API quota ------------------------------------------------------------

def reserve_quota(provider, day_utc, credits, cap):
    """Atomically add `credits` to today's count unless that would exceed `cap`. True if reserved."""
    with connect() as conn:
        conn.execute("INSERT OR IGNORE INTO api_quota (provider, day_utc, calls) VALUES (?,?,0)",
                     (provider, day_utc))
        cur = conn.execute("UPDATE api_quota SET calls = calls + ? WHERE provider = ? AND day_utc = ? "
                           "AND calls + ? <= ?", (credits, provider, day_utc, credits, cap))
        return cur.rowcount == 1


def quota_used(provider, day_utc):
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
            "INSERT OR IGNORE INTO alert_log (cluster_key, article_id, title, source, magnitude, direction, status, "
            "created_at) VALUES (?,?,?,?,?,?,?,?)",
            (cluster_key, article["id"], article["title"], article["source"], article["magnitude"],
             article["direction"], status, now or utcnow()))
        return cur.rowcount == 1


def set_alert_status(cluster_key, status):
    with connect() as conn:
        conn.execute("UPDATE alert_log SET status = ? WHERE cluster_key = ?", (status, cluster_key))


def alerts_in_last_hour(now=None):
    """Alerts sent (or being sent) in the last hour, for the hourly cap."""
    since = ((now or datetime.now(timezone.utc)) - timedelta(hours=1)).isoformat(timespec="seconds")
    rows = _rows("SELECT COUNT(*) AS n FROM alert_log WHERE status IN ('sent', 'pending') AND created_at >= ?", (since,))
    return rows[0]["n"]


def recent_alerts(limit=20):
    return _rows("SELECT * FROM alert_log ORDER BY id DESC LIMIT ?", (limit,))


def email_sent(day, kind):
    return bool(_rows("SELECT 1 FROM email_log WHERE day = ? AND kind = ?", (day, kind)))


def record_email_sent(day, kind, recipients):
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO email_log (day, kind, recipients, sent_at) VALUES (?,?,?,?)",
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
    return _rows("SELECT * FROM feedback ORDER BY id")


def article_by_id(article_id):
    rows = _rows("SELECT * FROM articles WHERE id = ?", (article_id,))
    return rows[0] if rows else None


# ---- job runs -------------------------------------------------------------

def log_job(job_name, status, detail=""):
    with connect() as conn:
        conn.execute("INSERT INTO job_runs (job_name, status, detail, created_at) VALUES (?,?,?,?)",
                     (job_name, status, str(detail)[:2000], utcnow()))


def recent_job_runs(job_name, limit=10):
    return _rows("SELECT * FROM job_runs WHERE job_name = ? ORDER BY id DESC LIMIT ?", (job_name, limit))


def latest_job_runs(limit=30):
    return _rows("SELECT * FROM job_runs ORDER BY id DESC LIMIT ?", (limit,))


# ---- sentiment ------------------------------------------------------------

def save_sentiment(date, sentiment, price):
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sentiment_history (date, label, score, n_articles, counts_json, "
            "price_benchmark, price_value, price_change_pct, confirmation, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (date, sentiment["label"], sentiment["score"], sentiment["n"], json.dumps(sentiment["counts"]),
             price.get("benchmark"), price.get("price"), price.get("change_pct"), price.get("status"), utcnow()))


def sentiment_history(limit=30):
    return _rows("SELECT * FROM sentiment_history ORDER BY date DESC LIMIT ?", (limit,))


# ---- costs ----------------------------------------------------------------

def save_run_cost(rec):
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
    """ISO time the latest scheduled collection cycle finished, or None."""
    rows = _rows("SELECT finished_at FROM run_costs WHERE job_name = 'collection_cycle' ORDER BY id DESC LIMIT 1")
    return rows[0]["finished_at"] if rows else None


def recent_run_costs(limit=20):
    # The price poller records a zero-cost run every few minutes; leave it out so real runs stay visible.
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
