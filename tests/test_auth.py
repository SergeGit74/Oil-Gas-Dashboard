"""The optional dashboard password: nothing is shown before login, attempts are limited, comparison is constant-time."""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import auth
import config
import storage

ROOT = Path(__file__).resolve().parent.parent
APP = str(ROOT / "app.py")
RESEARCH = str(ROOT / "pages" / "1_Research.py")
SECRET = "correct horse battery staple"


@pytest.fixture(autouse=True)
def fresh_limiter(monkeypatch):
    auth.reset_limiter()
    monkeypatch.setattr(auth, "_sleep", lambda s: None)           # no real delay after a failed guess
    yield
    auth.reset_limiter()


@pytest.fixture
def locked(monkeypatch, db):
    """A dashboard with a password and a brief in the database that must stay hidden until login."""
    monkeypatch.setenv("DASHBOARD_PASSWORD", SECRET)
    storage.save_brief(storage.utcnow()[:10], "published", 0.95, "## Headline view\n\nVISIBLE-ONLY-AFTER-LOGIN", {})
    return db


def submit(at, password):
    at.text_input[0].input(password)
    [b for b in at.button if b.label == "Sign in"][0].click()
    return at.run()


def page_text(at):
    return " ".join([m.value for m in at.markdown] + [t.value for t in at.title] + [h.value for h in at.header])


def test_without_a_password_the_dashboard_is_open(db):
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception and "Oil & Gas Morning Intelligence" in page_text(at)
    assert not any(t.value == "Sign in" for t in at.title)


def test_nothing_is_shown_before_login(locked):
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception
    assert [t.value for t in at.title] == ["Sign in"]
    assert "VISIBLE-ONLY-AFTER-LOGIN" not in page_text(at) and "Morning brief" not in page_text(at)
    assert len(at.metric) == 0 and len(at.dataframe) == 0 and len(at.sidebar.radio) == 0   # no refresh controls either


def test_the_research_page_is_gated_too(locked):
    at = AppTest.from_file(RESEARCH, default_timeout=60).run()
    assert [t.value for t in at.title] == ["Sign in"] and len(at.warning) == 0


def test_wrong_password_is_rejected_and_right_one_opens_the_dashboard(locked):
    at = AppTest.from_file(APP, default_timeout=60).run()
    at = submit(at, "nope")
    assert [e.value for e in at.error] == ["Wrong password."] and [t.value for t in at.title] == ["Sign in"]
    at = submit(at, SECRET)
    assert not at.exception and "Oil & Gas Morning Intelligence" in page_text(at)
    assert "VISIBLE-ONLY-AFTER-LOGIN" in page_text(at)
    assert any(b.label == "Sign out" for b in at.sidebar.button)


def test_sign_out_returns_to_the_login_form(locked):
    at = submit(AppTest.from_file(APP, default_timeout=60).run(), SECRET)
    [b for b in at.sidebar.button if b.label == "Sign out"][0].click()
    at.run()
    assert [t.value for t in at.title] == ["Sign in"]


def test_too_many_failures_lock_everyone_out_even_with_the_right_password(locked):
    at = AppTest.from_file(APP, default_timeout=60).run()
    for _ in range(config.DASHBOARD_LOGIN_MAX_ATTEMPTS):
        at = submit(at, "guess")
    assert any("Too many failed attempts" in e.value for e in at.error)
    at = submit(at, SECRET)
    assert [t.value for t in at.title] == ["Sign in"] and any("Too many failed attempts" in e.value for e in at.error)
    other_tab = AppTest.from_file(APP, default_timeout=60).run()                 # a new session shares the lockout
    other_tab = submit(other_tab, SECRET)
    assert [t.value for t in other_tab.title] == ["Sign in"]


def test_limiter_lockout_expires_after_the_window():
    now = {"t": 1000.0}
    limiter = auth.LoginLimiter(clock=lambda: now["t"])
    for _ in range(4):
        limiter.record_failure(max_attempts=5, lockout_s=60)
    assert limiter.locked_for() == 0
    limiter.record_failure(5, 60)
    assert limiter.locked_for() == pytest.approx(60)
    now["t"] += 61
    assert limiter.locked_for() == 0


def test_old_failures_do_not_count_towards_the_lockout():
    now = {"t": 0.0}
    limiter = auth.LoginLimiter(clock=lambda: now["t"])
    for _ in range(4):
        limiter.record_failure(5, 60)
    now["t"] = 120
    assert limiter.record_failure(5, 60) == 1 and limiter.locked_for() == 0


def test_password_comparison():
    assert auth.password_matches(SECRET, SECRET)
    assert not auth.password_matches(SECRET + " ", SECRET) and not auth.password_matches("", SECRET)
    assert not auth.password_matches(None, SECRET)
    assert auth.password_matches("pässwörd-密码", "pässwörd-密码")


def test_comparison_uses_a_constant_time_primitive(monkeypatch):
    calls = []
    real = auth.hmac.compare_digest
    monkeypatch.setattr(auth.hmac, "compare_digest", lambda a, b: calls.append((len(a), len(b))) or real(a, b))
    auth.password_matches("short", "a much longer expected password")
    assert calls == [(32, 32)]                                        # equal-length digests, whatever the inputs
