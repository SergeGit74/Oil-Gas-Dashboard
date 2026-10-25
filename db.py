"""Database access for SQLite (default) and PostgreSQL (when DATABASE_URL is set).

The storage layer writes one SQL dialect: `?` placeholders, `ON CONFLICT` upserts and portable queries. This module
adapts it per backend: it converts placeholders, returns rows as dicts, and translates the few DDL keywords that
differ (AUTOINCREMENT, REAL). The PostgreSQL driver (psycopg) is imported only when DATABASE_URL is set.
"""
import re
import sqlite3
from contextlib import contextmanager
from urllib.parse import urlsplit

import config


def backend():
    return "postgres" if config.DATABASE_URL else "sqlite"


def safe_url(url):
    """The database URL without its password, for logs and messages."""
    parts = urlsplit(url or "")
    if parts.password:
        host = parts.hostname or ""
        netloc = f"{parts.username}:***@{host}" + (f":{parts.port}" if parts.port else "")
        return parts._replace(netloc=netloc).geturl()
    return url or ""


def pg_ddl(sql):
    """SQLite-flavoured DDL -> PostgreSQL."""
    sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
    return re.sub(r"\bREAL\b", "DOUBLE PRECISION", sql)


def statements(script):
    return [s.strip() for s in script.split(";") if s.strip()]


class Conn:
    """A connection with one calling convention for both backends."""

    def __init__(self, raw, kind):
        self.raw, self.kind = raw, kind

    def execute(self, sql, params=()):
        """Run one statement; returns a cursor (iterate for dict-like rows, `.rowcount` for writes)."""
        if self.kind == "postgres":
            return self.raw.execute(sql.replace("%", "%%").replace("?", "%s"), list(params))
        return self.raw.execute(sql, params)

    def executemany(self, sql, seq):
        if self.kind == "postgres":
            cur = self.raw.cursor()
            cur.executemany(sql.replace("%", "%%").replace("?", "%s"), [list(p) for p in seq])
            return cur
        return self.raw.executemany(sql, seq)

    def ddl(self, sql):
        """Run DDL written for SQLite; translated for PostgreSQL."""
        self.execute(pg_ddl(sql) if self.kind == "postgres" else sql)

    def script(self, text):
        for stmt in statements(text):
            self.ddl(stmt)

    def columns(self, table):
        if self.kind == "postgres":
            rows = self.execute("SELECT column_name AS name FROM information_schema.columns "
                                "WHERE table_schema = current_schema() AND table_name = ?", (table,))
        else:
            rows = self.execute(f"PRAGMA table_info({table})")
        return {r["name"] for r in rows}

    def table_exists(self, table):
        return bool(self.columns(table))


@contextmanager
def connect():
    if config.DATABASE_URL:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # psycopg is only needed for PostgreSQL
            raise RuntimeError("DATABASE_URL is set but psycopg is not installed: pip install 'psycopg[binary]'") from exc
        raw = psycopg.connect(config.DATABASE_URL, row_factory=dict_row, connect_timeout=10)
        kind = "postgres"
    else:
        raw = sqlite3.connect(config.DB_PATH, timeout=30)
        raw.row_factory = sqlite3.Row
        raw.execute("PRAGMA journal_mode=WAL")
        kind = "sqlite"
    try:
        yield Conn(raw, kind)
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()


def upsert_sql(table, columns, conflict=None):
    """INSERT ... ON CONFLICT statement that works on SQLite (3.24+) and PostgreSQL.

    conflict=None: ignore a row that violates any unique constraint (INSERT OR IGNORE).
    conflict=[cols]: replace the other columns when those key columns already exist (INSERT OR REPLACE)."""
    base = f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' * len(columns))})"
    if conflict is None:
        return base + " ON CONFLICT DO NOTHING"
    updates = [c for c in columns if c not in conflict]
    return base + f" ON CONFLICT ({','.join(conflict)}) DO UPDATE SET " + ",".join(f"{c}=excluded.{c}" for c in updates)
