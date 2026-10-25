"""Copy an existing SQLite database into PostgreSQL (one-off migration to DATABASE_URL).

    python scripts/copy_sqlite_to_postgres.py [--sqlite PATH] [--pg-url URL] [--dry-run] [--append]

- The SQLite file is opened read-only and is never changed. Nothing is deleted on either side.
- The PostgreSQL schema is created by the same versioned migrations the app uses.
- The target must be empty unless you pass --append (rows whose key already exists are skipped, never overwritten).
- Everything is copied in ONE transaction: if any table fails, nothing is written.
- Id sequences are advanced past the copied ids, so new rows do not collide.
- Afterwards set DATABASE_URL in .env (and stop writing to the SQLite file) to switch the app over.
"""
import argparse
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import db  # noqa: E402
import migrations  # noqa: E402

BATCH = 1000
SERIAL_TABLES = ("articles", "alert_log", "feedback", "job_runs", "run_costs", "price_ticks", "events")


class CopyError(Exception):
    pass


def _source_tables(src):
    """Names of the SQLite data tables (internal and migration tables excluded)."""
    rows = src.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").fetchall()
    return sorted(r[0] for r in rows if r[0] != "schema_migrations")


def copy(sqlite_path, pg_url, append=False, dry_run=False, log=print):
    """Returns {table: {"source": n, "copied": n, "target": n}}. Raises CopyError on a refusal or failure."""
    import psycopg
    from psycopg.rows import dict_row

    path = Path(sqlite_path).resolve()
    if not path.exists():
        raise CopyError(f"SQLite file not found: {path}")
    src = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)   # read-only: cannot modify the source
    tables = _source_tables(src)
    report = {}
    raw = psycopg.connect(pg_url, row_factory=dict_row, connect_timeout=10)
    try:
        conn = db.Conn(raw, "postgres")
        if not dry_run:
            migrations.migrate(conn)
        known = [t for t in tables if conn.table_exists(t)]
        missing = [t for t in tables if t not in known]
        if missing and dry_run:
            log(f"Target schema not created yet (dry run); tables to be created by migrations: {', '.join(missing)}")
        elif missing:
            raise CopyError(f"Source tables unknown to the schema: {', '.join(missing)}")
        existing = {t: conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"] for t in known}
        if any(existing.values()) and not append:
            raise CopyError("The target already contains data (" +
                            ", ".join(f"{t}={n}" for t, n in existing.items() if n) +
                            "). Use --append to add only rows that are not there yet.")
        for table in tables:
            cols_src = [r[1] for r in src.execute(f"PRAGMA table_info({table})")]
            n_source = src.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            report[table] = {"source": n_source, "copied": 0, "target": existing.get(table, 0)}
            if dry_run or not n_source:
                continue
            cols = [c for c in cols_src if c in conn.columns(table)]  # columns the target has
            sql = db.upsert_sql(table, cols)
            cursor = src.execute(f"SELECT {','.join(cols)} FROM {table}")
            while True:
                batch = cursor.fetchmany(BATCH)
                if not batch:
                    break
                try:
                    report[table]["copied"] += conn.executemany(sql, batch).rowcount
                except psycopg.Error as exc:
                    raise CopyError(f"{table}: {exc}") from None
        if not dry_run:
            for table in SERIAL_TABLES:
                if table in report:
                    conn.execute("SELECT setval(pg_get_serial_sequence(?, 'id'), "
                                 f"COALESCE((SELECT MAX(id) FROM {table}), 1), (SELECT MAX(id) FROM {table}) IS NOT NULL)",
                                 (table,))
            for table, info in report.items():
                info["target"] = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
                if not append and info["target"] != info["source"]:
                    raise CopyError(f"{table}: {info['source']} rows in SQLite but {info['target']} in PostgreSQL")
            raw.commit()
        else:
            raw.rollback()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()
        src.close()
    return report


def main():
    """Command-line entry point: parse arguments, run the copy and print the per-table report."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--sqlite", default=config.DB_PATH, help="source SQLite file (default: DB_PATH)")
    parser.add_argument("--pg-url", default=os.environ.get("DATABASE_URL"), help="target URL (default: DATABASE_URL)")
    parser.add_argument("--dry-run", action="store_true", help="count rows and check the target; write nothing")
    parser.add_argument("--append", action="store_true", help="allow a non-empty target; existing keys are skipped")
    args = parser.parse_args()
    if not args.pg_url:
        sys.exit("No target: pass --pg-url or set DATABASE_URL")
    print(f"Source: {args.sqlite} (read-only)\nTarget: {db.safe_url(args.pg_url)}")
    try:
        report = copy(args.sqlite, args.pg_url, append=args.append, dry_run=args.dry_run)
    except CopyError as exc:
        sys.exit(f"Nothing was written. {exc}")
    for table, info in report.items():
        print(f"  {table:<18} source {info['source']:>7}  copied {info['copied']:>7}  target now {info['target']:>7}")
    print("Dry run only: nothing was written." if args.dry_run else
          "Done. Set DATABASE_URL in .env and restart the scheduler and dashboard to use PostgreSQL.")


if __name__ == "__main__":
    main()
