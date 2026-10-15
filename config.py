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

REQUIRED_KEYS = ["ANTHROPIC_API_KEY", "VOYAGE_API_KEY", "EIA_API_KEY", "SERPER_API_KEY", "APIFY_TOKEN"]


def get(name, default=""):
    return os.getenv(name, default)


def require(*names):
    """Raise a clear error if any of the named environment variables is empty."""
    missing = [n for n in names if not os.getenv(n)]
    if missing:
        raise RuntimeError(f"Missing required settings in .env: {', '.join(missing)}")
