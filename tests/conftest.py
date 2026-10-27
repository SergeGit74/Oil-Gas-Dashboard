"""Test setup: a throwaway database on each backend, no paid keys, and no real network access.

config.DB_PATH must be changed before `import storage` (it runs init_db() at import), so this happens at module
level, before any test module imports the project.

The `db` fixture runs a test once on SQLite and, when a PostgreSQL server is available, once more on PostgreSQL:
set PG_TEST_URL to use a server you already have (CI does), otherwise the embedded `pgserver` package is started if
installed (pip install pgserver). REQUIRE_PG=1 turns "no PostgreSQL available" into a failure instead of a skip.
"""
import os
import socket
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="oilgas_tests_"))
for _name in ("ANTHROPIC_API_KEY", "VOYAGE_API_KEY", "EIA_API_KEY", "SERPER_API_KEY", "APIFY_TOKEN",
              "TWELVE_DATA_API_KEY", "SLACK_WEBHOOK_URL", "SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD",
              "EMAIL_FROM", "EMAIL_TO", "EMAIL_OWNER", "EMAIL_ENABLED", "DATABASE_URL", "DASHBOARD_PASSWORD"):
    os.environ[_name] = ""  # set (not removed) so load_dotenv cannot fill them from the real .env
# Scheduling settings too: a developer's .env (for example SCHEDULER_MODE=deployment) must not change test results.
for _name in ("SCHEDULER_MODE", "SCHEDULER_TZ", "DEPLOY_RUN_HOURS", "DESK_TIMEZONE", "COLLECTION_INTERVAL_MINUTES",
              "BRIEF_HOUR", "BRIEF_MINUTE", "CATCHUP_AFTER_HOUR", "CATCHUP_AFTER_MINUTE"):
    os.environ[_name] = ""  # blank means "use the built-in default" for every one of these
os.environ["DEMO_MODE"] = ""  # tests turn demo mode on explicitly; a developer's environment must not
os.environ["ENABLE_YAHOO_FALLBACK"] = "false"

import config  # noqa: E402

config.DB_PATH = str(_TMP / "base.db")
config.EVAL_HISTORY_PATH = str(_TMP / "eval_history.jsonl")
config.SLACK_WEBHOOK_URL = ""
config.DATABASE_URL = ""

import storage  # noqa: E402


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Autouse: make any non-loopback socket connection raise, so an unmocked network call fails the test."""
    real_connect = socket.socket.connect

    def guarded_connect(self, address, *args, **kwargs):
        """Allow loopback addresses (asyncio, Streamlit, local PostgreSQL) and refuse everything else."""
        host = address[0] if isinstance(address, tuple) else address
        if host not in ("127.0.0.1", "::1", "localhost"):  # loopback: asyncio, Streamlit's test runner, local Postgres
            raise RuntimeError(f"tests must not open network connections (tried {host}); mock the HTTP call")
        return real_connect(self, address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)


# ---- PostgreSQL for the second backend leg -----------------------------------

@pytest.fixture(scope="session")
def pg_url():
    """URL of a PostgreSQL server for tests, or None. Starts the embedded pgserver when nothing is configured."""
    url = os.environ.get("PG_TEST_URL")
    if url:
        yield url
        return
    try:
        import pgserver
    except ImportError:
        yield None
        return
    server = pgserver.get_server(tempfile.mkdtemp(prefix="oilgas_pg_"), cleanup_mode="stop")
    try:
        yield server.get_uri()
    finally:
        server.cleanup()


def _reset_postgres(url):
    """Drop and recreate the public schema so a PostgreSQL test starts empty."""
    import psycopg

    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")


@pytest.fixture(params=["sqlite", "postgres"])
def db(request, tmp_path, monkeypatch):
    """A fresh empty database for one test, on each available backend."""
    if request.param == "postgres":
        url = request.getfixturevalue("pg_url")
        if not url:
            message = "no PostgreSQL available (set PG_TEST_URL or pip install pgserver)"
            if os.environ.get("REQUIRE_PG"):
                pytest.fail(message)
            pytest.skip(message)
        _reset_postgres(url)
        monkeypatch.setattr(config, "DATABASE_URL", url)
    else:
        monkeypatch.setattr(config, "DATABASE_URL", "")
        monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    storage.init_db()
    return request.param


@pytest.fixture
def pg_clean(pg_url):
    """URL of an empty PostgreSQL database (schema reset), for tests that need PostgreSQL itself."""
    if not pg_url:
        message = "no PostgreSQL available (set PG_TEST_URL or pip install pgserver)"
        if os.environ.get("REQUIRE_PG"):
            pytest.fail(message)
        pytest.skip(message)
    _reset_postgres(pg_url)
    return pg_url
