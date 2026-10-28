"""Build the frozen demo snapshot: demo/demo_store.db plus demo/snapshot_meta.json.

    python scripts/make_demo_snapshot.py [--days 7] [--out demo/demo_store.db] [--force]

Reads the live store (SQLite file, or DATABASE_URL through db.py) with SELECT statements only; makes no network or API
call. It copies the last N days of the tables the dashboard shows, drops article snippets and every other body text,
sanitizes the operational logs, scans the result for secret-like strings, e-mail addresses and Windows paths (and
refuses to keep the file if anything matches), then finalizes it as a journal_mode=DELETE database. It never
overwrites an existing snapshot without --force, and it does not `git add` anything: review the summary first.
"""
import argparse
import json
import os
import re
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import db  # noqa: E402
import demo_mode  # noqa: E402
import migrations  # noqa: E402

SCRIPT_VERSION = 1
MAX_BYTES = 25 * 1024 * 1024
TICK_DAYS = 3              # price ticks kept
EIA_ROWS_PER_SERIES = 5    # weekly series are weekly, so "last N days" would keep one point; keep the last few
REASON_MAX = 300           # articles.reason, the model's one-line tag rationale
DETAIL_MAX = 200           # job_runs.detail and each string inside run_costs.details_json
EXCLUDED_TABLES = ("feedback", "email_log", "alert_log", "api_quota", "run_claims")
DROP_KEYS = {"run_id", "runid", "actor_run_id", "dataset_id", "default_dataset_id", "user_id", "account", "account_id",
             "token", "api_key", "email", "username"}

# ---- sanitizing and scanning ------------------------------------------------
URL_WITH_CREDENTIALS = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@\S+", re.I)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
WINDOWS_PATH = re.compile(r"\b[A-Za-z]:\\[^\s\"']*")
UNIX_HOME_PATH = re.compile(r"(?:/home|/Users|/root)/[^\s\"']+")
KEY_LIKE = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|apify_api_\w+|AKIA[0-9A-Z]{16}|xox[abp]-[\w-]+|[A-Za-z0-9_-]{32,})\b")
SCAN_PATTERNS = {
    "Anthropic key (sk-ant-)": re.compile(r"sk-ant-"),
    "Apify token (apify_api_)": re.compile(r"apify_api_"),
    "Slack webhook": re.compile(r"hooks\.slack\.com"),
    "AWS access key (AKIA...)": re.compile(r"AKIA[0-9A-Z]{16}"),
    "KEY= assignment": re.compile(r"KEY=", re.I),
    "PASSWORD= assignment": re.compile(r"PASSWORD=", re.I),
    "URL with credentials": URL_WITH_CREDENTIALS,
    "e-mail address": EMAIL,
    "Windows path": WINDOWS_PATH,
}


def sanitize_text(text, limit=DETAIL_MAX):
    """Remove credentials-in-URLs, e-mail addresses, file paths and key-like strings, then truncate."""
    if text is None:
        return None
    for pattern in (URL_WITH_CREDENTIALS, EMAIL, WINDOWS_PATH, UNIX_HOME_PATH, KEY_LIKE):
        text = pattern.sub("[removed]", str(text))
    return text[:limit]


def sanitize_json(value):
    """Sanitize a decoded JSON value: strings are cleaned and truncated, identifier-like keys are blanked, and long
    lists are cut."""
    if isinstance(value, dict):
        return {k: ("[removed]" if k.lower() in DROP_KEYS else sanitize_json(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_json(v) for v in value[:20]]
    if isinstance(value, str):
        return sanitize_text(value)
    return value


def sanitize_details_json(raw):
    """details_json text -> sanitized JSON text (plain sanitized text if it does not parse)."""
    if not raw:
        return raw
    try:
        return json.dumps(sanitize_json(json.loads(raw)))
    except ValueError:
        return sanitize_text(raw, 500)


def scan(path):
    """Every text value in every table of the snapshot against SCAN_PATTERNS -> list of 'table.column: what' hits."""
    hits = []
    raw = sqlite3.connect(path)
    try:
        tables = [r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for table in tables:
            columns = [r[1] for r in raw.execute(f"PRAGMA table_info({table})")]
            for column in columns:
                for (value,) in raw.execute(f"SELECT {column} FROM {table} WHERE typeof({column}) = 'text'"):
                    for label, pattern in SCAN_PATTERNS.items():
                        if pattern.search(value):
                            hits.append(f"{table}.{column}: {label}")
                            break
    finally:
        raw.close()
    return hits


# ---- reading the live store ---------------------------------------------------
@contextmanager
def open_source():
    """The live store for reading: SQLite opened read-only, or the PostgreSQL connection from DATABASE_URL."""
    if db.backend() == "sqlite":
        raw = sqlite3.connect(Path(config.DB_PATH).resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
        raw.row_factory = sqlite3.Row
        try:
            yield db.Conn(raw, "sqlite")
        finally:
            raw.close()
    else:
        with db.connect() as conn:
            yield conn


def _copy(src, dest, table, columns, where="", params=(), transform=None, order=""):
    """Copy `columns` of the rows selected from the live table into the snapshot; returns the row count."""
    rows = [dict(r) for r in src.execute(f"SELECT {','.join(columns)} FROM {table} {where} {order}", params)]
    if transform:
        rows = [transform(r) for r in rows]
    dest.executemany(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' * len(columns))})",
                     [tuple(r[c] for c in columns) for r in rows])
    return len(rows)


def _capped_reason(row):
    """Limit the tag rationale to REASON_MAX characters."""
    row["reason"] = None if row["reason"] is None else row["reason"][:REASON_MAX]
    return row


def _clean_job_run(row):
    """Sanitize a job_runs row."""
    row["detail"] = sanitize_text(row["detail"])
    return row


def _clean_run_cost(row):
    """Sanitize a run_costs row."""
    row["details_json"] = sanitize_details_json(row["details_json"])
    return row


def build(out, days=7, force=False, now=None):
    """Write the snapshot to `out` (and snapshot_meta.json beside it). Returns the summary dict.

    Raises demo_mode.SnapshotError for a refusal (existing file without force, demo mode, live schema behind the
    code, safety-scan matches, file too large). On any failure nothing is left at `out`."""
    if config.DEMO_MODE:
        raise demo_mode.SnapshotError("Cannot build a snapshot while DEMO_MODE is on: there is no live store to read.")
    out = Path(out)
    meta_path = out.parent / "snapshot_meta.json"
    if out.exists() and not force:
        raise demo_mode.SnapshotError(f"{out} already exists. Review it, or pass --force to replace it.")
    now = now or datetime.now(timezone.utc)
    start = (now - timedelta(days=days)).isoformat(timespec="seconds")
    start_day = start[:10]
    ticks_from = (now - timedelta(days=TICK_DAYS)).isoformat(timespec="seconds")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.unlink(missing_ok=True)

    counts = {}
    dest_raw = sqlite3.connect(tmp)
    dest_raw.row_factory = sqlite3.Row  # migrations read rows by column name
    try:
        dest = db.Conn(dest_raw, "sqlite")
        migrations.migrate(dest)  # the exact current schema, including schema_migrations
        for table in EXCLUDED_TABLES:
            dest_raw.execute(f"DROP TABLE IF EXISTS {table}")
        with open_source() as src:
            missing = {v for v, _, _ in migrations.MIGRATIONS} - migrations.applied_versions(src)
            if missing:
                raise demo_mode.SnapshotError("The live store is behind the code (missing migrations "
                                              f"{sorted(missing)}). Run `python -c \"import storage\"` first.")
            counts["articles"] = _copy(
                src, dest_raw, "articles",
                ["id", "url", "title", "source", "collected_at", "category", "direction", "magnitude", "confidence",
                 "source_tier", "content_type", "reason", "flagged_injection", "cluster_id"],
                "WHERE collected_at >= ?", (start,), _capped_reason)
            # the latest published brief, and the latest held brief in the window if the live store really has one
            published = _copy(src, dest_raw, "briefs",
                              ["date", "created_at", "status", "groundedness", "text", "eval_json",
                               "key_points_json", "sources_json"],
                              "WHERE date = (SELECT MAX(date) FROM briefs WHERE status = 'published')")
            held = _copy(src, dest_raw, "briefs",
                         ["date", "created_at", "status", "groundedness", "text", "eval_json", "key_points_json",
                          "sources_json"],
                         "WHERE status != 'published' AND date >= ? AND date = (SELECT MAX(date) FROM briefs "
                         "WHERE status != 'published' AND date >= ?)", (start_day, start_day))
            counts["briefs"] = published + held
            counts["sentiment_history"] = _copy(
                src, dest_raw, "sentiment_history",
                ["date", "label", "score", "n_articles", "counts_json", "price_benchmark", "price_value",
                 "price_change_pct", "confirmation", "updated_at"], "WHERE date >= ?", (start_day,))
            counts["price_ticks"] = _copy(
                src, dest_raw, "price_ticks",
                ["indicator_key", "ts", "price", "prev_close", "change_pct", "provider", "delayed_minutes",
                 "currency", "fetched_at"],
                # the last TICK_DAYS days, plus each indicator's newest tick so a market that has been closed for
                # longer (TTF, SPX over a weekend) still has a tile value
                "WHERE ts >= ? OR id IN (SELECT id FROM (SELECT id, ROW_NUMBER() OVER (PARTITION BY indicator_key "
                "ORDER BY ts DESC, id DESC) AS rn FROM price_ticks) AS ranked WHERE rn = 1)", (ticks_from,),
                order="ORDER BY ts, id")
            counts["price_daily"] = _copy(
                src, dest_raw, "price_daily",
                ["indicator_key", "day", "open", "high", "low", "close", "n_ticks", "provider"],
                "WHERE day >= ?", (start_day,))
            eia = 0
            for row in src.execute("SELECT DISTINCT series_key FROM eia_weekly").fetchall():
                eia += _copy(src, dest_raw, "eia_weekly", ["series_key", "period", "value", "unit", "fetched_at"],
                             "WHERE series_key = ?", (row["series_key"],),
                             order=f"ORDER BY period DESC LIMIT {EIA_ROWS_PER_SERIES}")
            counts["eia_weekly"] = eia
            counts["events"] = _copy(src, dest_raw, "events",
                                     ["date", "time", "tz", "title", "category", "source_url", "source"],
                                     "WHERE date >= ?", (now.date().isoformat(),))
            counts["job_runs"] = _copy(src, dest_raw, "job_runs", ["job_name", "status", "detail", "created_at"],
                                       "WHERE created_at >= ?", (start,), _clean_job_run)
            counts["run_costs"] = _copy(
                src, dest_raw, "run_costs",
                ["job_name", "started_at", "finished_at", "status", "claude_usd", "apify_usd", "serper_usd",
                 "total_usd", "input_tokens", "output_tokens", "serper_credits", "details_json"],
                "WHERE started_at >= ?", (start,), _clean_run_cost)
        counts["schema_migrations"] = dest_raw.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        dest_raw.commit()
    except BaseException:
        dest_raw.close()
        tmp.unlink(missing_ok=True)
        raise
    dest_raw.close()

    try:
        hits = scan(tmp)
        if hits:
            shown = "\n  ".join(hits[:20]) + (f"\n  ... and {len(hits) - 20} more" if len(hits) > 20 else "")
            raise demo_mode.SnapshotError(f"Safety scan found {len(hits)} secret-like string(s), e-mail address(es) or "
                                          f"path(s); the snapshot was NOT written:\n  {shown}")
        final = sqlite3.connect(tmp)
        final.execute("PRAGMA journal_mode=DELETE")
        final.execute("VACUUM")
        final.close()
        size = tmp.stat().st_size
        if size > MAX_BYTES:
            raise demo_mode.SnapshotError(f"The snapshot is {size / 1e6:.1f} MB, over the {MAX_BYTES // 2**20} MB limit; "
                                          "use fewer --days.")
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, out)
    summary = {"captured_at": now.isoformat(timespec="seconds"), "window_days": days, "window_start": start,
               "row_counts": counts, "schema_version": migrations.LATEST, "script_version": SCRIPT_VERSION,
               "source_backend": db.backend(), "size_bytes": size, "scan": "clean"}
    meta_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main():
    """Command line entry: build the snapshot and print the summary, or print why it was refused."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=7, help="window in days (default 7)")
    parser.add_argument("--out", default=str(config.DEMO_DB_PATH), help="output file (default demo/demo_store.db)")
    parser.add_argument("--force", action="store_true", help="replace an existing snapshot")
    args = parser.parse_args()
    try:
        summary = build(args.out, args.days, args.force)
    except demo_mode.SnapshotError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        sys.exit(2)
    print(f"Snapshot written to {args.out} ({summary['size_bytes'] / 1e6:.2f} MB), captured {summary['captured_at']}")
    print(f"Window: last {summary['window_days']} days from {summary['window_start']}; schema version "
          f"{summary['schema_version']}; safety scan: {summary['scan']}")
    for table, n in summary["row_counts"].items():
        print(f"  {table:<18} {n}")
    print("Excluded tables:", ", ".join(EXCLUDED_TABLES))
    print("Nothing was added to git. Review the file and the summary first.")


if __name__ == "__main__":
    main()
