"""Versioned schema migrations, applied in order by storage.init_db() on SQLite and PostgreSQL alike.

Rules: append a new (version, name, function) entry; never edit or reorder an applied one. Every step must be
idempotent (CREATE ... IF NOT EXISTS, guarded ADD COLUMN), so a database created before versioning existed adopts
the list on first start by simply re-running it, and nothing is dropped or rewritten. Applied versions are recorded
in `schema_migrations`.
"""
from datetime import datetime, timezone

BASELINE = """
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

# Columns added after the first schema version.
ADDED_COLUMNS = {
    "articles": {
        "direction": "TEXT", "magnitude": "INTEGER", "corroboration": "INTEGER DEFAULT 0",
        "flagged_injection": "INTEGER DEFAULT 0", "indexed": "INTEGER DEFAULT 0",
        "content_type": "TEXT", "source_tier": "TEXT",
        "cluster_id": "INTEGER", "corroboration_json": "TEXT",
    },
    "briefs": {"key_points_json": "TEXT", "sources_json": "TEXT"},
    "sentiment_history": {"price_value": "REAL"},
}


def _baseline(conn):
    conn.script(BASELINE)


def _added_columns(conn):
    for table, columns in ADDED_COLUMNS.items():
        existing = conn.columns(table)
        for col, decl in columns.items():
            if col not in existing:
                conn.ddl(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def _cluster_index(conn):
    conn.ddl("CREATE INDEX IF NOT EXISTS idx_articles_cluster ON articles(cluster_id)")


MIGRATIONS = [
    (1, "baseline schema", _baseline),
    (2, "columns added after the baseline (articles, briefs, sentiment_history)", _added_columns),
    (3, "index on articles.cluster_id", _cluster_index),
]
LATEST = MIGRATIONS[-1][0]


def applied_versions(conn):
    if not conn.table_exists("schema_migrations"):
        return set()
    return {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}


def migrate(conn):
    """Apply every missing migration in order. Returns the versions applied by this call."""
    if conn.kind == "postgres":  # serialise concurrent starts (scheduler + dashboard) for the whole transaction
        conn.execute("SELECT pg_advisory_xact_lock(7272741)")
    conn.ddl("CREATE TABLE IF NOT EXISTS schema_migrations "
             "(version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)")
    done = applied_versions(conn)
    newly = []
    for version, name, step in MIGRATIONS:
        if version in done:
            continue
        step(conn)
        conn.execute("INSERT INTO schema_migrations (version, name, applied_at) VALUES (?,?,?) ON CONFLICT DO NOTHING",
                     (version, name, datetime.now(timezone.utc).isoformat(timespec="seconds")))
        newly.append(version)
    return newly
