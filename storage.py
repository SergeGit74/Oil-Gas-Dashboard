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
"""

# Columns added after the first schema version; added to older databases by init_db().
MIGRATIONS = {
    "articles": {
        "direction": "TEXT", "magnitude": "INTEGER", "corroboration": "INTEGER DEFAULT 0",
        "flagged_injection": "INTEGER DEFAULT 0", "indexed": "INTEGER DEFAULT 0",
        "content_type": "TEXT", "source_tier": "TEXT",
    },
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
            "flagged_injection"]
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


def unindexed_verified():
    return _rows("SELECT * FROM articles WHERE indexed = 0 AND flagged_injection = 0 AND confidence >= ?",
                 (config.VERIFIED_MIN_CONFIDENCE,))


def mark_indexed(ids):
    with connect() as conn:
        conn.executemany("UPDATE articles SET indexed = 1 WHERE id = ?", [(i,) for i in ids])


# ---- briefs ---------------------------------------------------------------

def save_brief(date, status, groundedness, text, evaluation):
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO briefs (date, created_at, status, groundedness, text, eval_json) "
            "VALUES (?,?,?,?,?,?)",
            (date, utcnow(), status, groundedness, text, json.dumps(evaluation)))


def latest_brief():
    rows = _rows("SELECT * FROM briefs ORDER BY date DESC LIMIT 1")
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
    rows = _rows("SELECT MAX(id) AS m FROM run_costs WHERE job_name NOT LIKE 'ask_desk%'")
    return rows[0]["m"] or 0


def last_cycle_finished():
    """ISO time the latest scheduled collection cycle finished, or None."""
    rows = _rows("SELECT finished_at FROM run_costs WHERE job_name = 'collection_cycle' ORDER BY id DESC LIMIT 1")
    return rows[0]["finished_at"] if rows else None


def recent_run_costs(limit=20):
    return _rows("SELECT * FROM run_costs ORDER BY id DESC LIMIT ?", (limit,))


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
