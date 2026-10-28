"""DEMO_MODE: a read-only, no-keys dashboard on a frozen snapshot, and the script that builds the snapshot."""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import config
import cost_tracker
import demo_mode
import freshness
import migrations
import storage
from jobs import scheduler, source_health
from jobs.check_cost_budget import check_cost_budget
from jobs.check_eval_trend import check_eval_trend
from jobs.load_events import run_load_events
from jobs.run_collection import run_collection
from jobs.run_evaluation import run_evaluation
from jobs.run_ingestion import run_ingestion
from jobs.run_prices import purge_old_ticks, run_prices
from jobs.send_brief_email import send_brief_email

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import check_symbols  # noqa: E402
import make_demo_snapshot  # noqa: E402

CAPTURED = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)  # a Thursday, long before the real clock
DAY = "2026-01-15"
EXCLUDED = ("feedback", "email_log", "alert_log", "api_quota", "run_claims")


def article(url, title, **extra):
    """A complete article row for storage.save_articles, collected shortly before the snapshot time."""
    row = {"url": url, "title": title, "source": "reuters.com", "snippet": "SNIPPET-BODY-TEXT-MUST-NOT-SURVIVE",
           "published_at": "2026-01-15T07:00:00+00:00", "collected_at": "2026-01-15T08:00:00+00:00",
           "category": "supply", "direction": "Bullish", "magnitude": 4, "confidence": 0.9, "source_tier": "wire_trade",
           "content_type": "news", "corroboration": 1, "reason": "short reason", "flagged_injection": 0,
           "corroboration_json": "[]"}
    row.update(extra)
    return row


def fill_live_store():
    """Put one of everything the snapshot copies (and some it must not copy) into the live test store."""
    storage.save_articles([article("https://reuters.com/a", "Refinery strike lifts crude")])
    storage.save_brief(DAY, "published", 0.95, "# Morning Intelligence Brief\n\nBrent rose [1].", {"reasons": []},
                       key_points={"prices": {}}, sources=[{"n": 1, "title": "T", "source": "reuters.com",
                                                            "url": "https://reuters.com/a"}])
    storage.save_sentiment(DAY, {"label": "Neutral", "score": 0.0, "n": 1, "counts": {"Bullish": 1}},
                           {"benchmark": "Brent", "price": 80.0, "change_pct": 1.0, "status": "Partial"})
    storage.save_ticks([{"indicator_key": "BRENT", "ts": "2026-01-15T11:55:00+00:00", "price": 80.0, "prev_close": 79.0,
                         "change_pct": 1.27, "provider": "yahoo", "delayed_minutes": None, "currency": "USD",
                         "fetched_at": "2026-01-15T11:56:00+00:00"}])
    storage.upsert_events([{"date": "2026-01-16", "time": "10:30", "tz": "America/New_York",
                            "title": "EIA Weekly Petroleum Status Report", "category": "data",
                            "source_url": "https://www.eia.gov/", "source": "eia_wpsr"}])
    storage.add_feedback("article_tag", "1", "up", "private analyst note")
    storage.log_job("fetch_prices", "success", "ok at C:\\Users\\jane\\x.txt for jane@example.com")
    with cost_tracker.cost_run("collection_cycle"):
        pass


@pytest.fixture
def snap(db, tmp_path):
    """Build a snapshot (captured long ago) from a filled live store; returns its folder."""
    fill_live_store()
    out = tmp_path / "demo" / "demo_store.db"
    make_demo_snapshot.build(out, days=7, now=CAPTURED)
    return out.parent


def demo_on(monkeypatch, folder):
    """Switch the process to demo mode on the given snapshot folder, with no database URL."""
    monkeypatch.setattr(config, "DEMO_MODE", True)
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(config, "DB_PATH", str(folder / "demo_store.db"))
    monkeypatch.setattr(config, "DEMO_DB_PATH", folder / "demo_store.db")
    monkeypatch.setattr(config, "DEMO_META_PATH", folder / "snapshot_meta.json")


def render(page="app.py"):
    """Render a dashboard page with AppTest."""
    return AppTest.from_file(str(ROOT / page), default_timeout=60).run()


def sha(path):
    """SHA-256 of a file."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---- the dashboard in demo mode -------------------------------------------

def test_dashboard_renders_with_no_keys_and_no_database_url(snap, monkeypatch):
    """With no API keys and no DATABASE_URL, the dashboard and the Research page render from the snapshot."""
    demo_on(monkeypatch, snap)
    for page in ("app.py", "pages/1_Research.py"):
        at = render(page)
        assert not at.exception
        assert any("Demo snapshot captured on 15 January 2026" in i.value and "not investment advice" in i.value
                   for i in at.info)
    at = render()
    assert any("80.00" in m.value for m in at.metric)  # the tick in the snapshot
    assert any("Insufficient data" in i.value for i in render("pages/1_Research.py").info)  # nothing invented


def test_render_leaves_the_snapshot_untouched(snap, monkeypatch):
    """The file's hash is unchanged after rendering both pages, and no -wal, -shm or journal file appears."""
    demo_on(monkeypatch, snap)
    before, listing = sha(snap / "demo_store.db"), sorted(p.name for p in snap.iterdir())
    render()
    render("pages/1_Research.py")
    assert sha(snap / "demo_store.db") == before
    assert sorted(p.name for p in snap.iterdir()) == listing == ["demo_store.db", "snapshot_meta.json"]


def test_writes_to_the_snapshot_fail(snap, monkeypatch):
    """A write attempted through the storage layer raises instead of changing the snapshot."""
    demo_on(monkeypatch, snap)
    before = sha(snap / "demo_store.db")
    with pytest.raises(sqlite3.OperationalError):
        storage.log_job("x", "success", "should not be written")
    with pytest.raises(sqlite3.OperationalError):
        storage.add_feedback("article_tag", "1", "up", "no")
    assert sha(snap / "demo_store.db") == before


def test_feedback_ask_the_desk_and_scheduler_refresh_are_disabled(snap, monkeypatch):
    """Feedback and 'ask the desk' are greyed out, 'With scheduler' is hidden and Manual is the default."""
    demo_on(monkeypatch, snap)
    at = render()
    assert all(t.disabled for t in at.text_input) and all(t.disabled for t in at.text_area)
    assert [b.disabled for b in at.button if b.label == "Save feedback"] == [True]
    assert any("Disabled in the demo" in c.value for c in at.caption)
    refresh = at.sidebar.radio[0]
    assert "With scheduler" not in refresh.options and refresh.value == "Manual"


def test_the_clock_is_frozen_at_capture_time(snap, monkeypatch):
    """An old snapshot is not stale: 'now' is captured_at, so its tick is fresh, its brief is today's, and its event
    is in the next 7 days."""
    demo_on(monkeypatch, snap)
    assert demo_mode.now() == CAPTURED and demo_mode.today().isoformat() == DAY
    assert demo_mode.hours_ago(24) == "2026-01-14T12:00:00+00:00"
    at = render()
    assert not any("Stale" in c.value for c in at.caption)
    assert not any("Today's brief is pending" in i.value for i in at.info)
    assert any("as of 15 Jan 2026" in c.value for c in at.caption)
    events = [d for d in at.dataframe if "EIA Weekly Petroleum Status Report" in d.value.to_json()]
    assert events, "the event dated the day after capture is listed"


def test_freshness_header_is_neutral_in_demo_mode(snap, monkeypatch):
    """No indicator is amber or red, however old the snapshot is."""
    demo_on(monkeypatch, snap)
    assert [i.level for i in freshness.header()] == ["unknown"] * 3


def test_snapshot_would_look_stale_judged_at_a_later_time(snap, monkeypatch):
    """Control: judged three days after capture, the brief indicator is red, so the frozen clock is what keeps the
    demo header neutral."""
    demo_on(monkeypatch, snap)
    later = datetime(2026, 1, 18, 12, 0, tzinfo=timezone.utc)
    assert freshness.brief_freshness(later).level == "red"
    assert freshness.brief_freshness(CAPTURED).level == "ok"


# ---- refusing to run jobs ---------------------------------------------------

JOB_ENTRY_POINTS = [
    ("scheduler.main", scheduler.main), ("scheduler.collection_cycle", scheduler.collection_cycle),
    ("run_collection", run_collection), ("run_ingestion", run_ingestion), ("run_evaluation", run_evaluation),
    ("run_prices", run_prices), ("purge_old_ticks", purge_old_ticks), ("run_load_events", run_load_events),
    ("send_brief_email", send_brief_email), ("check_cost_budget", check_cost_budget),
    ("check_eval_trend", check_eval_trend), ("check_source_health", source_health.check_source_health),
    ("check_symbols.main", check_symbols.main),
]


@pytest.mark.parametrize("name,fn", JOB_ENTRY_POINTS, ids=[n for n, _ in JOB_ENTRY_POINTS])
def test_jobs_and_scheduler_refuse_in_demo_mode(snap, monkeypatch, name, fn):
    """Every job entry point, the scheduler and the symbol check exit with a clear message in demo mode."""
    demo_on(monkeypatch, snap)
    monkeypatch.setattr(sys, "argv", ["prog"])
    with pytest.raises(SystemExit, match="DEMO_MODE"):
        fn()


@pytest.mark.parametrize("module", ["jobs.scheduler", "jobs.run_collection", "jobs.run_ingestion", "jobs.run_evaluation",
                                    "jobs.run_prices", "jobs.load_events", "jobs.send_brief_email"])
def test_command_line_entry_points_refuse_in_a_fresh_process(module):
    """`python -m jobs.<name>` with DEMO_MODE=true exits non-zero with the demo-mode message, before doing anything."""
    env = {**os.environ, "DEMO_MODE": "true", "PYTHONPATH": str(ROOT), "DATA_DIR": str(ROOT / "tests")}
    out = subprocess.run([sys.executable, "-m", module], env=env, cwd=ROOT, capture_output=True, text=True)
    assert out.returncode != 0 and "DEMO_MODE" in out.stderr and "Traceback" not in out.stderr.split("DEMO_MODE")[0][-200:]


def test_source_health_without_notify_still_works_in_demo_mode(snap, monkeypatch):
    """The dashboard's read-only health check is allowed."""
    demo_on(monkeypatch, snap)
    assert source_health.check_source_health(notify=False) == []


# ---- configuration -----------------------------------------------------------

@pytest.mark.parametrize("value,expected", [("true", True), ("1", True), ("YES", True), ("False", False), ("", False)])
def test_demo_mode_flag_and_database_url_are_ignored_in_a_fresh_process(value, expected):
    """DEMO_MODE accepts true/1/yes in any case, and when on the database URL is dropped and the snapshot is used."""
    env = {**os.environ, "DEMO_MODE": value, "DATABASE_URL": "postgresql://u:pw@elsewhere.invalid/db",
           "PYTHONPATH": str(ROOT), "DATA_DIR": str(ROOT / "tests")}
    code = "import config; print(config.DEMO_MODE, bool(config.DATABASE_URL), config.DB_PATH.endswith('demo_store.db'))"
    out = subprocess.run([sys.executable, "-c", code], env=env, cwd=ROOT, capture_output=True, text=True, check=True)
    assert out.stdout.split() == [str(expected), str(not expected), str(expected)]


def test_only_the_demo_snapshot_database_is_tracked_by_git():
    """No .db file other than demo/demo_store.db may be committed."""
    try:
        listed = subprocess.run(["git", "ls-files", "*.db"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    assert set(listed.split()) <= {"demo/demo_store.db"}


# ---- the snapshot check -------------------------------------------------------

def test_schema_version_mismatch_asks_for_a_new_snapshot(snap, monkeypatch):
    """A snapshot older than the code shows the regenerate message and stops before any panel."""
    demo_on(monkeypatch, snap)
    monkeypatch.setattr(migrations, "LATEST", migrations.LATEST + 1)
    at = render()
    assert any("make_demo_snapshot.py" in e.value and "schema version" in e.value for e in at.error)
    assert not at.metric


def test_missing_snapshot_asks_for_a_new_snapshot(tmp_path, monkeypatch):
    """With no snapshot file the dashboard says so instead of crashing."""
    demo_on(monkeypatch, tmp_path)
    at = render()
    assert not at.exception and any("make_demo_snapshot.py" in e.value for e in at.error)


def test_demo_app_entrypoint_runs_the_dashboard(snap, monkeypatch):
    """The Community Cloud entrypoint turns demo mode on and runs the real dashboard; its pages stub does the same."""
    demo_on(monkeypatch, snap)
    for page in ("demo_app/streamlit_app.py", "demo_app/pages/1_Research.py"):
        at = render(page)
        assert not at.exception and any("Demo snapshot captured" in i.value for i in at.info)


# ---- the snapshot script -------------------------------------------------------

def snapshot_tables(folder):
    """{table: [rows as dicts]} of a built snapshot."""
    raw = sqlite3.connect(folder / "demo_store.db")
    raw.row_factory = sqlite3.Row
    names = [r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    tables = {n: [dict(r) for r in raw.execute(f"SELECT * FROM {n}")] for n in names}
    raw.close()
    return tables


def test_snapshot_drops_excluded_tables_and_body_text(snap):
    """Excluded tables are absent, article snippets are gone, and the content that should be kept is there."""
    tables = snapshot_tables(snap)
    assert not set(EXCLUDED) & set(tables)
    art = tables["articles"][0]
    assert art["title"] == "Refinery strike lifts crude" and art["snippet"] is None and art["published_at"] is None
    assert art["direction"] == "Bullish" and art["reason"] == "short reason" and art["source_tier"] == "wire_trade"
    assert "SNIPPET-BODY-TEXT" not in json.dumps(tables) and "private analyst note" not in json.dumps(tables)
    assert [b["date"] for b in tables["briefs"]] == [DAY] and tables["events"] and tables["price_ticks"]
    assert max(r["version"] for r in tables["schema_migrations"]) == migrations.LATEST


def test_snapshot_sanitizes_logs_and_writes_metadata(snap):
    """Paths and e-mail addresses are removed from job logs, and the metadata describes the build."""
    detail = [r["detail"] for r in snapshot_tables(snap)["job_runs"] if r["job_name"] == "fetch_prices"][0]
    assert "jane" not in detail and "C:\\" not in detail and "@" not in detail
    meta = json.loads((snap / "snapshot_meta.json").read_text(encoding="utf-8"))
    assert meta["captured_at"] == "2026-01-15T12:00:00+00:00" and meta["schema_version"] == migrations.LATEST
    assert meta["window_days"] == 7 and meta["row_counts"]["articles"] == 1 and meta["scan"] == "clean"
    assert meta["script_version"] == make_demo_snapshot.SCRIPT_VERSION


def test_reason_is_capped_at_300_characters(db, tmp_path):
    """A longer tag rationale is cut to 300 characters."""
    storage.save_articles([article("https://reuters.com/long", "Long reason", reason="x" * 500)])
    out = tmp_path / "demo_store.db"
    make_demo_snapshot.build(out, now=CAPTURED)
    assert len(snapshot_tables(tmp_path)["articles"][0]["reason"]) == 300


@pytest.mark.parametrize("planted", ["key sk-ant-api03-abcdef", "mail jane@example.com", "file C:\\Users\\jane\\notes.txt",
                                     "https://hooks.slack.com/services/T0/B0/xyz", "PASSWORD=hunter2",
                                     "apify_api_AbCdEf123", "AKIAABCDEFGHIJKLMNOP"])
def test_secret_email_or_path_in_a_source_row_fails_and_removes_the_output(db, tmp_path, planted):
    """A secret-like string, e-mail address or path in a copied column aborts the build and leaves no file."""
    storage.save_articles([article("https://reuters.com/x", f"Headline {planted}")])
    out = tmp_path / "demo" / "demo_store.db"
    with pytest.raises(demo_mode.SnapshotError, match="Safety scan"):
        make_demo_snapshot.build(out, now=CAPTURED)
    assert not out.exists() and not (tmp_path / "demo" / "snapshot_meta.json").exists()
    assert not list((tmp_path / "demo").glob("*.tmp"))


def test_existing_snapshot_needs_force(db, tmp_path):
    """The script will not overwrite a snapshot unless asked."""
    out = tmp_path / "demo_store.db"
    make_demo_snapshot.build(out, now=CAPTURED)
    with pytest.raises(demo_mode.SnapshotError, match="--force"):
        make_demo_snapshot.build(out, now=CAPTURED)
    make_demo_snapshot.build(out, now=CAPTURED, force=True)


def test_snapshot_cannot_be_built_in_demo_mode(snap, monkeypatch, tmp_path):
    """There is no live store to read in demo mode."""
    demo_on(monkeypatch, snap)
    with pytest.raises(demo_mode.SnapshotError, match="DEMO_MODE"):
        make_demo_snapshot.build(tmp_path / "x.db", now=CAPTURED)


def test_snapshot_is_finalized_without_wal_files(snap):
    """The file is in journal_mode=DELETE and has no companion files."""
    raw = sqlite3.connect(snap / "demo_store.db")
    assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    raw.close()
    assert sorted(p.name for p in snap.iterdir()) == ["demo_store.db", "snapshot_meta.json"]
