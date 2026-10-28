"""Demo mode helpers: the frozen clock, the snapshot check, the page banner and the guard for jobs.

DEMO_MODE=true (config.DEMO_MODE) serves a committed, sanitized SQLite snapshot read-only. Everything the dashboard
judges relative to "now" uses now() here, which is the snapshot's capture time in demo mode and the real clock
otherwise. This module imports Streamlit only inside functions, so jobs and tests can import it without it.
"""
import functools
import json
from datetime import datetime, timedelta, timezone

import config

BANNER = ("Demo snapshot captured on {day}. The data is frozen and nothing here is live. "
          "AI-generated content, not investment advice.")
REGENERATE = "Regenerate it with: python scripts/make_demo_snapshot.py --force"


class SnapshotError(RuntimeError):
    """The demo snapshot is missing, unreadable or out of date."""


def refuse(name):
    """Stop with a clear message when called in demo mode. SystemExit passes through the scheduler's step wrapper."""
    if config.DEMO_MODE:
        raise SystemExit(f"{name} is disabled in DEMO_MODE: the demo is a frozen, read-only snapshot, so it never "
                         "collects data, calls a provider or sends a message. Unset DEMO_MODE to run the pipeline.")


def block_in_demo(name):
    """Decorator: the wrapped job entry point refuses to run in demo mode."""
    def decorate(fn):
        """Wrap `fn` with the demo-mode check."""
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            """Refuse in demo mode, otherwise call the job."""
            refuse(name)
            return fn(*args, **kwargs)
        return wrapper
    return decorate


def captured_at():
    """When the snapshot was captured (aware UTC datetime), from snapshot_meta.json."""
    try:
        meta = json.loads(config.DEMO_META_PATH.read_text(encoding="utf-8"))
        stamp = datetime.fromisoformat(meta["captured_at"])
    except (OSError, ValueError, KeyError) as exc:
        raise SnapshotError(f"The demo snapshot metadata ({config.DEMO_META_PATH.name}) is missing or unreadable. "
                            + REGENERATE) from exc
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def now():
    """The dashboard's notion of the current time: the capture time in demo mode, the real UTC time otherwise."""
    return captured_at() if config.DEMO_MODE else datetime.now(timezone.utc)


def today():
    """The dashboard's current UTC date."""
    return now().date()


def hours_ago(hours):
    """UTC ISO time `hours` before now() (same format as storage.hours_ago)."""
    return (now() - timedelta(hours=hours)).isoformat(timespec="seconds")


def check_snapshot():
    """None when the snapshot is usable, otherwise a one-paragraph message for the owner.

    Usable means: the database file and metadata exist, and the highest recorded migration version equals the
    latest entry in migrations.MIGRATIONS. Nothing is written."""
    import db
    import migrations

    if not config.DEMO_DB_PATH.exists():
        return f"The demo snapshot {config.DEMO_DB_PATH.name} is missing. " + REGENERATE
    try:
        captured_at()
        with db.connect() as conn:
            versions = migrations.applied_versions(conn)
    except SnapshotError as exc:
        return str(exc)
    except Exception as exc:  # a corrupt or non-SQLite file
        return f"The demo snapshot cannot be read ({type(exc).__name__}). " + REGENERATE
    if max(versions, default=0) != migrations.LATEST:
        return (f"The demo snapshot has schema version {max(versions, default=0)} but the code expects "
                f"{migrations.LATEST}. " + REGENERATE)
    return None


def start_page():
    """Call right after login on every page: in demo mode stop with a message if the snapshot is unusable,
    otherwise show the banner. A no-op outside demo mode."""
    if not config.DEMO_MODE:
        return
    import streamlit as st

    problem = check_snapshot()
    if problem:
        st.error(problem)
        st.stop()
    st.info(BANNER.format(day=f"{captured_at():%d %B %Y}"))
