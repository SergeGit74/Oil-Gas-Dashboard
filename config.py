"""Central configuration: .env loading, required-key checks, shared constants."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
DAILY_COST_BUDGET_USD = float(os.getenv("DAILY_COST_BUDGET_USD", "10"))
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")

# Where the database, vector store and evaluation log live. Defaults to the project folder; the Docker image
# sets DATA_DIR=/data (a volume) so data survives container rebuilds.
DATA_DIR = Path(os.getenv("DATA_DIR") or BASE_DIR)
DATA_DIR.mkdir(parents=True, exist_ok=True)

# DEMO_MODE=true serves a frozen, sanitized snapshot (demo/demo_store.db) read-only: no keys, no scheduler, no
# network, no writes. DATABASE_URL is ignored completely in this mode so the demo can never read another database.
DEMO_MODE = (os.getenv("DEMO_MODE") or "").strip().lower() in ("1", "true", "yes")
DEMO_DIR = BASE_DIR / "demo"
DEMO_DB_PATH = DEMO_DIR / "demo_store.db"
DEMO_META_PATH = DEMO_DIR / "snapshot_meta.json"

DB_PATH = str(DEMO_DB_PATH if DEMO_MODE else DATA_DIR / "dashboard_store.db")
# Optional PostgreSQL (postgresql://user:password@host:5432/dbname). When set it replaces the SQLite file; the
# vector store and the other files stay under DATA_DIR. Never print this value: it contains a password.
DATABASE_URL = "" if DEMO_MODE else (os.getenv("DATABASE_URL") or "").strip()
ARCHIVE_DIR = str(DATA_DIR / "oil_gas_archive")
EVAL_HISTORY_PATH = str(DATA_DIR / "eval_history.jsonl")

# Scheduler settings (all overridable from .env). Times are in DESK_TIMEZONE.
DESK_TIMEZONE = os.getenv("DESK_TIMEZONE") or "Asia/Dubai"
COLLECTION_INTERVAL_MINUTES = int(os.getenv("COLLECTION_INTERVAL_MINUTES") or 20)

# SCHEDULER_MODE=local (default) runs every job; deployment runs only jobs.scheduler.deployment_cycle at the
# DEPLOY_RUN_HOURS (minute 0, in SCHEDULER_TZ). The scheduler validates these strictly at startup; here a bad value
# only falls back so that importing config (and the dashboard) never fails.
SCHEDULER_MODE = (os.getenv("SCHEDULER_MODE") or "local").strip().lower()
SCHEDULER_TZ = (os.getenv("SCHEDULER_TZ") or "").strip() or DESK_TIMEZONE
DEPLOY_RUN_HOURS_RAW = (os.getenv("DEPLOY_RUN_HOURS") or "6,10,14,18,22").strip()


def parse_run_hours(raw):
    """Parse '6,10,14' into a sorted list of distinct hours 0-23. Raises ValueError on anything else."""
    parts = [p.strip() for p in str(raw).split(",") if p.strip()]
    if not parts:
        raise ValueError("DEPLOY_RUN_HOURS is empty")
    hours = set()
    for part in parts:
        if not part.isdigit() or not 0 <= int(part) <= 23:
            raise ValueError(f"DEPLOY_RUN_HOURS has {part!r}; each entry must be a whole hour from 0 to 23")
        hours.add(int(part))
    return sorted(hours)


def longest_gap_minutes(hours):
    """Longest wait between two consecutive runs, wrapping past midnight (one run a day = 24 h)."""
    return max(((b - a) % 24 or 24) * 60 for a, b in zip(hours, hours[1:] + hours[:1]))


try:
    DEPLOY_RUN_HOURS = parse_run_hours(DEPLOY_RUN_HOURS_RAW)
except ValueError:
    DEPLOY_RUN_HOURS = [6, 10, 14, 18, 22]
DEPLOYMENT = SCHEDULER_MODE == "deployment"
# Deployment runs a few times a day, so the 20-minute freshness defaults below would be red between every run.
_gap = longest_gap_minutes(DEPLOY_RUN_HOURS) if DEPLOYMENT else None

# The brief is made by the first run of the day, so in deployment mode its default time is that run's hour.
BRIEF_HOUR = int(os.getenv("BRIEF_HOUR") or (DEPLOY_RUN_HOURS[0] if DEPLOYMENT else 5))
BRIEF_MINUTE = int(os.getenv("BRIEF_MINUTE") or (0 if DEPLOYMENT else 30))

# A collection cycle this overdue means the scheduler is probably not running (dashboard warning, healthcheck).
STALE_AFTER_MINUTES = _gap + 60 if DEPLOYMENT else max(45, 2 * COLLECTION_INTERVAL_MINUTES + 5)

# An article is "verified" when confidence is at or above this and it is not injection-flagged.
VERIFIED_MIN_CONFIDENCE = 0.5

# Keys the news pipeline needs. Nothing checks them at import time: each call site uses require_keys(...) so the
# dashboard (and the price poller) can start without any paid keys.
REQUIRED_KEYS = ["ANTHROPIC_API_KEY", "VOYAGE_API_KEY", "EIA_API_KEY", "SERPER_API_KEY", "APIFY_TOKEN"]

# Catch-up for a missed morning brief: from this local time (DESK_TIMEZONE) the scheduler generates it if absent.
# Defaults to 30 minutes after the scheduled brief time (06:00 with the default 05:30).
_brief_minutes = BRIEF_HOUR * 60 + BRIEF_MINUTE
_catchup_default = (_brief_minutes + 30) % (24 * 60)
CATCHUP_AFTER_HOUR = int(os.getenv("CATCHUP_AFTER_HOUR") or _catchup_default // 60)
CATCHUP_AFTER_MINUTE = int(os.getenv("CATCHUP_AFTER_MINUTE") or _catchup_default % 60)
BRIEF_MAX_ATTEMPTS = 2        # per day, shared by the scheduled run and the catch-up
BRIEF_RETRY_COOLDOWN_MIN = 30

# ---- market prices (Phase 1) -----------------------------------------------
MARKET_SYMBOLS_PATH = BASE_DIR / "config" / "market_symbols.json"
ENABLE_YAHOO_FALLBACK = (os.getenv("ENABLE_YAHOO_FALLBACK") or "false").strip().lower() in ("1", "true", "yes")
PRICE_PROVIDERS = [p.strip() for p in (os.getenv("PRICE_PROVIDERS") or "twelvedata,yahoo,eia").split(",") if p.strip()]
PRICE_POLL_INTERVAL_MIN = int(os.getenv("PRICE_POLL_INTERVAL_MIN") or 5)             # inside the trading window
PRICE_POLL_OFFHOURS_INTERVAL_MIN = int(os.getenv("PRICE_POLL_OFFHOURS_INTERVAL_MIN") or 60)
PRICE_TICK_RETENTION_DAYS = int(os.getenv("PRICE_TICK_RETENTION_DAYS") or 30)
PRICE_STALE_MINUTES = int(os.getenv("PRICE_STALE_MINUTES") or 30)  # an open-market tile older than this is stale
EIA_WEEKLY_REFRESH_HOURS = int(os.getenv("EIA_WEEKLY_REFRESH_HOURS") or 6)  # weekly series change once a week

# ---- staleness thresholds in the dashboard header (minutes) ----
STALE_COLLECTION_AMBER_MIN = int(os.getenv("STALE_COLLECTION_AMBER_MIN")
                                 or (_gap + 60 if DEPLOYMENT else 2 * COLLECTION_INTERVAL_MINUTES))
STALE_COLLECTION_RED_MIN = int(os.getenv("STALE_COLLECTION_RED_MIN")
                               or (_gap + 180 if DEPLOYMENT else 4 * COLLECTION_INTERVAL_MINUTES))
STALE_PRICE_AMBER_MIN = int(os.getenv("STALE_PRICE_AMBER_MIN") or 3 * PRICE_POLL_INTERVAL_MIN)
STALE_PRICE_RED_MIN = int(os.getenv("STALE_PRICE_RED_MIN") or 12 * PRICE_POLL_INTERVAL_MIN)
STALE_BRIEF_RED_AFTER_MIN = int(os.getenv("STALE_BRIEF_RED_AFTER_MIN") or 180)  # amber past catch-up time, red this much later

# ---- trust and quality (Phase 3) ----
# Tuned on real stored titles (2026-10-25 sample): pairs at 0.40-0.45 were still the same event.
CLUSTER_SIMILARITY_THRESHOLD = float(os.getenv("CLUSTER_SIMILARITY_THRESHOLD") or 0.45)
# Tuned with 8 real Serper queries on 2026-10-25; title-to-title cosine similarity.
CORROBORATION_MIN_SIMILARITY = float(os.getenv("CORROBORATION_MIN_SIMILARITY") or 0.40)
BACKTEST_MIN_OBSERVATIONS = int(os.getenv("BACKTEST_MIN_OBSERVATIONS") or 30)

# ---- dashboard password (optional; see auth.py) ----
DASHBOARD_LOGIN_MAX_ATTEMPTS = int(os.getenv("DASHBOARD_LOGIN_MAX_ATTEMPTS") or 5)
DASHBOARD_LOGIN_LOCKOUT_MIN = int(os.getenv("DASHBOARD_LOGIN_LOCKOUT_MIN") or 15)

# ---- delivery (Phase 4) ----
ALERT_MIN_MAGNITUDE = int(os.getenv("ALERT_MIN_MAGNITUDE") or 4)   # verified articles at or above this get a Slack alert
ALERT_MAX_PER_HOUR = int(os.getenv("ALERT_MAX_PER_HOUR") or 5)
EMAIL_OUTBOX_DIR = Path(os.getenv("EMAIL_OUTBOX_DIR") or DATA_DIR / "outbox")  # dry-run .eml files
EMAIL_MAX_ATTEMPTS = 3          # per day and kind, shared via storage.claim_daily_run
EMAIL_RETRY_COOLDOWN_MIN = 30

QUOTA_SAFETY_FRACTION = 0.9  # refuse calls beyond this share of a provider's daily limit
# provider -> (credits per minute, credits per day). Defaults are conservative guesses except Twelve Data's, which
# is its published Basic plan (8/min, 800/day); override in .env if your plan differs.
PROVIDER_LIMITS = {
    "twelvedata": (int(os.getenv("TWELVE_DATA_MAX_PER_MIN") or 8), int(os.getenv("TWELVE_DATA_MAX_PER_DAY") or 800)),
    "yahoo": (int(os.getenv("YAHOO_MAX_PER_MIN") or 20), int(os.getenv("YAHOO_MAX_PER_DAY") or 2000)),
    "eia": (int(os.getenv("EIA_MAX_PER_MIN") or 10), int(os.getenv("EIA_MAX_PER_DAY") or 500)),
}


def get(name, default=""):
    """Read an environment setting at call time (so .env edits and test patches are seen)."""
    return os.getenv(name, default)


def email_enabled():
    """EMAIL_ENABLED is off unless set to true. Read at call time so tests and .env edits behave the same."""
    return (os.getenv("EMAIL_ENABLED") or "false").strip().lower() in ("1", "true", "yes")


def require_keys(*names):
    """Raise a clear error if any of the named environment variables is empty. Call it where the key is used."""
    missing = [n for n in names if not os.getenv(n)]
    if missing:
        raise RuntimeError(f"Missing required settings in .env: {', '.join(missing)}")


require = require_keys  # older name, kept so existing call sites keep working
