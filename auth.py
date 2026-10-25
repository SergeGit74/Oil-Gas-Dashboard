"""Optional dashboard password. Off unless DASHBOARD_PASSWORD is set.

Call require_login() at the top of every page, before anything is read or rendered. It is a single shared password
for local use: constant-time comparison, a per-session login flag, and a lockout that is shared by all sessions in
the server process (so reloading the page or opening a new tab does not reset the attempt count). It is NOT a
substitute for platform-level authentication: a real deployment should put the dashboard behind its own login or
VPN (see the README).
"""
import hashlib
import hmac
import threading
import time

import streamlit as st

import config


class LoginLimiter:
    """After `max_attempts` failures within `lockout_s` seconds, refuse all logins for `lockout_s` seconds."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.failures = []
        self.locked_until = 0.0

    def locked_for(self):
        """Seconds of lockout left, 0 if logins are allowed."""
        with self.lock:
            return max(0.0, self.locked_until - self.clock())

    def record_failure(self, max_attempts, lockout_s):
        with self.lock:
            now = self.clock()
            self.failures = [t for t in self.failures if now - t < lockout_s] + [now]
            if len(self.failures) >= max_attempts:
                self.locked_until = now + lockout_s
                self.failures = []
            return len(self.failures)

    def record_success(self):
        with self.lock:
            self.failures = []


_sleep = time.sleep  # a module attribute so tests can replace it without touching time.sleep globally
_LIMITER = LoginLimiter()  # module state: shared by every session of the server process


def reset_limiter():
    global _LIMITER
    _LIMITER = LoginLimiter()


def password_matches(given, expected):
    """Constant-time comparison (compares fixed-length digests, so length is not leaked either)."""
    return hmac.compare_digest(hashlib.sha256((given or "").encode()).digest(),
                               hashlib.sha256(expected.encode()).digest())


def require_login():
    """Stop the page with a login form until the password is entered. Does nothing when no password is set."""
    expected = config.get("DASHBOARD_PASSWORD")
    if not expected:
        return
    if st.session_state.get("authenticated"):
        if st.sidebar.button("Sign out", key="auth_sign_out"):
            st.session_state["authenticated"] = False
            st.rerun()
        return

    st.title("Sign in")
    max_attempts = config.DASHBOARD_LOGIN_MAX_ATTEMPTS
    lockout_s = config.DASHBOARD_LOGIN_LOCKOUT_MIN * 60
    with st.form("login_form"):
        given = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in")
    wait = _LIMITER.locked_for()
    if wait:
        st.error(f"Too many failed attempts. Try again in {int(wait // 60) + 1} minute(s).")
    elif submitted:
        if password_matches(given, expected):
            _LIMITER.record_success()
            st.session_state["authenticated"] = True
            st.rerun()
        failures = _LIMITER.record_failure(max_attempts, lockout_s)
        _sleep(min(float(failures), 3.0))  # slow guessing down; the lockout is the hard limit
        st.error("Wrong password." if failures else "Too many failed attempts. Login is locked for a while.")
    st.stop()
