"""Test setup: a throwaway database, no paid keys, and no real network access.

config.DB_PATH must be changed before `import storage` (it runs init_db() at import), so this happens at module
level, before any test module imports the project.
"""
import os
import socket
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="oilgas_tests_"))
for _name in ("ANTHROPIC_API_KEY", "VOYAGE_API_KEY", "EIA_API_KEY", "SERPER_API_KEY", "APIFY_TOKEN",
              "TWELVE_DATA_API_KEY", "SLACK_WEBHOOK_URL"):
    os.environ[_name] = ""  # set (not removed) so load_dotenv cannot fill them from the real .env
os.environ["ENABLE_YAHOO_FALLBACK"] = "false"

import config  # noqa: E402

config.DB_PATH = str(_TMP / "base.db")
config.EVAL_HISTORY_PATH = str(_TMP / "eval_history.jsonl")
config.SLACK_WEBHOOK_URL = ""

import storage  # noqa: E402


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    real_connect = socket.socket.connect

    def guarded_connect(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if host not in ("127.0.0.1", "::1", "localhost"):  # loopback is used by asyncio and Streamlit's test runner
            raise RuntimeError(f"tests must not open network connections (tried {host}); mock the HTTP call")
        return real_connect(self, address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A fresh empty database for one test."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    storage.init_db()
    return config.DB_PATH
