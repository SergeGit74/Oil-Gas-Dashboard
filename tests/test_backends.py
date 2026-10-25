"""Behaviour that must be identical on SQLite and PostgreSQL, plus the migration runner and the copy script."""
import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest

import config
import db as dbmod
import migrations
import storage

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import copy_sqlite_to_postgres as copier  # noqa: E402


def article(url, **extra):
    base = {"url": url, "title": f"Title {url}", "source": "reuters.com", "snippet": "s", "published_at": "2026-10-26",
            "collected_at": "2026-10-26T12:00:00+00:00", "category": "Supply", "direction": "Bullish", "magnitude": 3,
            "confidence": 0.8, "source_tier": "wire_trade", "content_type": "news", "corroboration": 0, "reason": "r",
            "flagged_injection": 0}
    return {**base, **extra}


# ---- pure helpers ---------------------------------------------------------

def test_upsert_sql_ignore_and_replace_forms():
    assert dbmod.upsert_sql("t", ["a", "b"]) == "INSERT INTO t (a,b) VALUES (?,?) ON CONFLICT DO NOTHING"
    assert dbmod.upsert_sql("t", ["k", "a", "b"], ["k"]) == \
        "INSERT INTO t (k,a,b) VALUES (?,?,?) ON CONFLICT (k) DO UPDATE SET a=excluded.a,b=excluded.b"


def test_postgres_ddl_translation():
    out = dbmod.pg_ddl("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, price REAL NOT NULL, realm TEXT)")
    assert "BIGSERIAL PRIMARY KEY" in out and "price DOUBLE PRECISION NOT NULL" in out and "realm TEXT" in out


def test_safe_url_hides_the_password():
    assert dbmod.safe_url("postgresql://me:s3cret@db.example.test:5432/oil") == "postgresql://me:***@db.example.test:5432/oil"
    assert dbmod.safe_url("postgresql://me@db.example.test/oil") == "postgresql://me@db.example.test/oil"
    assert "s3cret" not in dbmod.safe_url("postgresql://me:s3cret@h/db")


# ---- both backends --------------------------------------------------------

def test_backend_matches_the_parameter(db):
    assert dbmod.backend() == db


def test_migrations_are_recorded_and_rerunning_is_a_noop(db):
    versions = [r["version"] for r in storage._rows("SELECT version FROM schema_migrations ORDER BY version")]
    assert versions == [v for v, _, _ in migrations.MIGRATIONS] == [1, 2, 3]
    with storage.connect() as conn:
        assert migrations.migrate(conn) == []
    storage.init_db()
    assert len(storage._rows("SELECT version FROM schema_migrations")) == 3


def test_a_database_from_before_versioning_is_adopted_without_losing_data(db):
    storage.save_articles([article("keep-me")])
    with storage.connect() as conn:                      # recreate the pre-versioning state
        conn.execute("DROP TABLE schema_migrations")
        conn.execute("DROP INDEX idx_articles_cluster")
        conn.execute("ALTER TABLE articles DROP COLUMN cluster_id")
        conn.execute("ALTER TABLE articles DROP COLUMN corroboration_json")
        conn.execute("ALTER TABLE briefs DROP COLUMN sources_json")
    storage.init_db()
    with storage.connect() as conn:
        assert {"cluster_id", "corroboration_json"} <= conn.columns("articles")
        assert "sources_json" in conn.columns("briefs")
    assert [r["url"] for r in storage._rows("SELECT url FROM articles")] == ["keep-me"]
    assert len(storage._rows("SELECT version FROM schema_migrations")) == 3


def test_percent_signs_in_sql_are_safe(db):
    assert storage.latest_marker() == 0                  # contains LIKE 'ask_desk%'
    storage.save_run_cost({"job_name": "ask_desk", "started_at": "t", "finished_at": "t", "status": "success",
                           "claude_usd": 0, "apify_usd": 0, "serper_usd": 0, "total_usd": 0, "input_tokens": 0,
                           "output_tokens": 0, "serper_credits": 0, "details": {}})
    storage.save_run_cost({"job_name": "collection_cycle", "started_at": "t", "finished_at": "t", "status": "success",
                           "claude_usd": 0, "apify_usd": 0, "serper_usd": 0, "total_usd": 0, "input_tokens": 0,
                           "output_tokens": 0, "serper_credits": 0, "details": {}})
    assert storage.latest_marker() == 2                  # the ask_desk run is excluded


def test_upserts_ignore_or_replace_as_designed(db):
    storage.save_articles([article("same", title="first")])
    storage.save_articles([article("same", title="second")])                          # ignored: URL already stored
    assert [r["title"] for r in storage._rows("SELECT title FROM articles")] == ["first"]
    storage.save_sentiment("2026-10-26", {"label": "Bull", "score": 0.3, "n": 2, "counts": {}}, {})
    storage.save_sentiment("2026-10-26", {"label": "Bear", "score": -0.3, "n": 4, "counts": {}}, {})  # replaced
    assert [r["label"] for r in storage.sentiment_history()] == ["Bear"]
    storage.save_brief("2026-10-26", "published", 0.9, "one", {}, sources=[{"n": 1}])
    storage.save_brief("2026-10-26", "published", 0.95, "two", {})                    # replaced, sources reset
    brief = storage.brief_for_date("2026-10-26")
    assert brief["text"] == "two" and brief["sources_json"] is None


def test_null_magnitude_sorts_last_on_both_backends(db):
    storage.save_articles([article("none", magnitude=None), article("three", magnitude=3), article("five", magnitude=5)])
    rows = storage.verified_articles("2026-01-01T00:00:00+00:00")
    assert [r["url"] for r in rows] == ["five", "three", "none"]


def test_price_series_keeps_one_point_per_quote_time(db):
    t = "2026-10-26T10:00:00+00:00"
    storage.save_ticks([
        {"indicator_key": "BRENT", "ts": t, "price": 80.0, "provider": "eia", "fetched_at": t},
        {"indicator_key": "BRENT", "ts": t, "price": 81.0, "provider": "yahoo", "fetched_at": t},          # newer id wins
        {"indicator_key": "BRENT", "ts": "2026-10-26T11:00:00+00:00", "price": 82.0, "provider": "yahoo", "fetched_at": t}])
    assert [(r["ts"][11:13], r["price"]) for r in storage.price_series("BRENT", "2026-10-26T00:00:00+00:00")] == \
        [("10", 81.0), ("11", 82.0)]


def test_events_unique_key_treats_missing_time_as_one_value(db):
    row = {"date": "2026-10-28", "time": None, "tz": None, "title": "FOMC", "category": "x", "source_url": None}
    storage.upsert_events([row, row])
    storage.upsert_events([row])
    assert len(storage.events_between("2026-10-28", "2026-10-28")) == 1


def test_floats_and_integers_round_trip(db):
    storage.save_articles([article("f", confidence=0.123456789, magnitude=4)])
    got = storage._rows("SELECT confidence, magnitude FROM articles")[0]
    assert got["confidence"] == pytest.approx(0.123456789) and got["magnitude"] == 4 and isinstance(got["magnitude"], int)


# ---- copy script (PostgreSQL only) ----------------------------------------

@pytest.fixture
def sqlite_source(tmp_path, monkeypatch):
    """A populated SQLite database file, built through the normal storage layer."""
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "source.db"))
    storage.init_db()
    storage.save_articles([article(f"u{i}", title=f"Story {i}") for i in range(5)])
    storage.save_brief("2026-10-26", "published", 0.95, "brief", {"reasons": []}, key_points={"a": 1}, sources=[{"n": 1}])
    storage.save_ticks([{"indicator_key": "BRENT", "ts": "2026-10-26T10:00:00+00:00", "price": 80.5,
                         "fetched_at": "2026-10-26T10:00:01+00:00", "provider": "yahoo"}])
    storage.upsert_events([{"date": "2026-10-28", "time": None, "tz": None, "title": "FOMC", "category": "x",
                            "source_url": None}])
    storage.add_feedback("article_tag", "1", "up", "ok")
    return Path(config.DB_PATH)


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pg_count(url, table):
    import psycopg
    with psycopg.connect(url) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_copy_moves_every_table_and_leaves_the_source_untouched(sqlite_source, pg_clean, monkeypatch):
    before = file_hash(sqlite_source)
    report = copier.copy(sqlite_source, pg_clean, log=lambda *_: None)
    assert report["articles"] == {"source": 5, "copied": 5, "target": 5}
    assert {t: r["target"] for t, r in report.items()} == {t: r["source"] for t, r in report.items()}
    assert file_hash(sqlite_source) == before                                    # read-only
    assert pg_count(pg_clean, "briefs") == 1 and pg_count(pg_clean, "feedback") == 1
    # new rows must not collide with copied ids
    monkeypatch.setattr(config, "DATABASE_URL", pg_clean)
    storage.save_articles([article("brand-new")])
    ids = sorted(r["id"] for r in storage._rows("SELECT id FROM articles"))
    assert len(ids) == 6 and len(set(ids)) == 6 and ids[-1] > ids[-2]
    assert storage.brief_for_date("2026-10-26")["text"] == "brief"


def test_copy_refuses_a_non_empty_target_and_append_skips_existing_rows(sqlite_source, pg_clean):
    copier.copy(sqlite_source, pg_clean, log=lambda *_: None)
    with pytest.raises(copier.CopyError, match="already contains data"):
        copier.copy(sqlite_source, pg_clean, log=lambda *_: None)
    report = copier.copy(sqlite_source, pg_clean, append=True, log=lambda *_: None)
    assert report["articles"]["copied"] == 0 and pg_count(pg_clean, "articles") == 5      # nothing duplicated or replaced


def test_dry_run_writes_nothing(sqlite_source, pg_clean):
    report = copier.copy(sqlite_source, pg_clean, dry_run=True, log=lambda *_: None)
    assert report["articles"]["source"] == 5 and report["articles"]["copied"] == 0
    import psycopg
    with psycopg.connect(pg_clean) as conn:
        assert conn.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public'").fetchone()[0] == 0


def test_a_bad_row_rolls_the_whole_copy_back(sqlite_source, pg_clean):
    raw = sqlite3.connect(sqlite_source)
    raw.execute("INSERT INTO articles (url, magnitude) VALUES ('bad', 'not-a-number')")   # SQLite accepts it, PostgreSQL will not
    raw.commit()
    raw.close()
    with pytest.raises(copier.CopyError, match="articles"):
        copier.copy(sqlite_source, pg_clean, log=lambda *_: None)
    import psycopg
    with psycopg.connect(pg_clean) as conn:     # even the schema creation was rolled back: the target is as it was
        assert conn.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public'").fetchone()[0] == 0


def test_copy_reports_a_missing_source_file(tmp_path, pg_clean):
    with pytest.raises(copier.CopyError, match="not found"):
        copier.copy(tmp_path / "nope.db", pg_clean)
