"""The dashboard renders with no paid keys, shows 'Brief pending', and never starts a paid or alerting call."""
from pathlib import Path

from streamlit.testing.v1 import AppTest

import storage
from jobs import source_health

APP = str(Path(__file__).resolve().parent.parent / "app.py")


def test_renders_with_no_keys_and_empty_store(db):
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    assert any("Brief pending" in i.value for i in at.info)
    assert any("No prices yet" in i.value for i in at.info)


def test_renders_price_tiles_from_stored_ticks(db):
    now = storage.utcnow()
    storage.save_ticks([{"indicator_key": "BRENT", "ts": now, "price": 80.0, "prev_close": 79.0,
                         "change_pct": 1.27, "provider": "yahoo", "delayed_minutes": None, "currency": "USD",
                         "fetched_at": now}])
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    assert any("80.00" in m.value for m in at.metric)


def test_source_health_without_notify_sends_and_writes_nothing(db, monkeypatch):
    for _ in range(4):
        storage.log_job("fetch_prices", "degraded", "x")
    sent = []
    monkeypatch.setattr(source_health.alerts, "send_alert", lambda *a, **k: sent.append(a))
    before = len(storage.latest_job_runs(100))
    assert source_health.check_source_health(notify=False) == ["fetch_prices"]
    assert sent == [] and len(storage.latest_job_runs(100)) == before
