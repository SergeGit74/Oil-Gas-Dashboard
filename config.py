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
DB_PATH = str(DATA_DIR / "dashboard_store.db")
ARCHIVE_DIR = str(DATA_DIR / "oil_gas_archive")
EVAL_HISTORY_PATH = str(DATA_DIR / "eval_history.jsonl")

# Scheduler settings (all overridable from .env). Times are in DESK_TIMEZONE.
DESK_TIMEZONE = os.getenv("DESK_TIMEZONE") or "Asia/Dubai"
COLLECTION_INTERVAL_MINUTES = int(os.getenv("COLLECTION_INTERVAL_MINUTES") or 20)
BRIEF_HOUR = int(os.getenv("BRIEF_HOUR") or 5)
BRIEF_MINUTE = int(os.getenv("BRIEF_MINUTE") or 30)

# A collection cycle this overdue means the scheduler is probably not running (dashboard warning, healthcheck).
STALE_AFTER_MINUTES = max(45, 2 * COLLECTION_INTERVAL_MINUTES + 5)

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
STALE_COLLECTION_AMBER_MIN = int(os.getenv("STALE_COLLECTION_AMBER_MIN") or 2 * COLLECTION_INTERVAL_MINUTES)
STALE_COLLECTION_RED_MIN = int(os.getenv("STALE_COLLECTION_RED_MIN") or 4 * COLLECTION_INTERVAL_MINUTES)
STALE_PRICE_AMBER_MIN = int(os.getenv("STALE_PRICE_AMBER_MIN") or 3 * PRICE_POLL_INTERVAL_MIN)
STALE_PRICE_RED_MIN = int(os.getenv("STALE_PRICE_RED_MIN") or 12 * PRICE_POLL_INTERVAL_MIN)
STALE_BRIEF_RED_AFTER_MIN = int(os.getenv("STALE_BRIEF_RED_AFTER_MIN") or 180)  # amber past catch-up time, red this much later

QUOTA_SAFETY_FRACTION = 0.9  # refuse calls beyond this share of a provider's daily limit
# provider -> (credits per minute, credits per day). Defaults are conservative guesses except Twelve Data's, which
# is its published Basic plan (8/min, 800/day); override in .env if your plan differs.
PROVIDER_LIMITS = {
    "twelvedata": (int(os.getenv("TWELVE_DATA_MAX_PER_MIN") or 8), int(os.getenv("TWELVE_DATA_MAX_PER_DAY") or 800)),
    "yahoo": (int(os.getenv("YAHOO_MAX_PER_MIN") or 20), int(os.getenv("YAHOO_MAX_PER_DAY") or 2000)),
    "eia": (int(os.getenv("EIA_MAX_PER_MIN") or 10), int(os.getenv("EIA_MAX_PER_DAY") or 500)),
}


def get(name, default=""):
    return os.getenv(name, default)


def require_keys(*names):
    """Raise a clear error if any of the named environment variables is empty. Call it where the key is used."""
    missing = [n for n in names if not os.getenv(n)]
    if missing:
        raise RuntimeError(f"Missing required settings in .env: {', '.join(missing)}")


require = require_keys  # older name, kept so existing call sites keep working
