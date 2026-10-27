"""Data-freshness indicators for the dashboard header, computed from job_runs, price_ticks and briefs.
Pure reads: nothing here calls an external service or writes to the store."""
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import config
import demo_mode
import market_calendar
import market_data
import storage

COLLECTION_JOBS = ("fetch_apify_commodity_news", "fetch_gdelt")  # a success from either means collection ran


@dataclass
class Freshness:
    label: str
    text: str
    level: str  # "ok", "amber", "red" or "unknown"

    @property
    def colored(self):
        """The text wrapped in Streamlit color markup for its level."""
        color = {"ok": "green", "amber": "orange", "red": "red"}.get(self.level)
        return f":{color}[{self.text}]" if color else self.text


def level_for(age_min, amber, red):
    """Classify an age against amber and red thresholds: ok, amber, red, or unknown when the age is None."""
    if age_min is None:
        return "unknown"
    return "ok" if age_min < amber else "amber" if age_min < red else "red"


def minutes_since(iso, now):
    """Minutes between an ISO timestamp and `now`, or None for no timestamp."""
    return None if not iso else (now - datetime.fromisoformat(iso)).total_seconds() / 60


def _ago(age_min):
    """Human text for an age in minutes ('12 min ago', '3.5 h ago', 'never')."""
    return "never" if age_min is None else f"{age_min:.0f} min ago" if age_min < 120 else f"{age_min / 60:.1f} h ago"


def collection_freshness(now):
    """How long ago a collection last succeeded (Apify or GDELT fetch in job_runs)."""
    iso = storage.last_success_time(COLLECTION_JOBS) or storage.last_cycle_finished()
    age = minutes_since(iso, now)
    return Freshness("Last collection", _ago(age),
                     level_for(age, config.STALE_COLLECTION_AMBER_MIN, config.STALE_COLLECTION_RED_MIN))


def price_freshness(now):
    """Age of the last successful price fetch. Judged only while some market is open: when every market is closed
    the poller runs hourly and an old tick is expected. Deployment mode never polls prices, so nothing is judged."""
    if config.DEPLOYMENT:
        return Freshness("Last price tick", "not polled (deployment mode)", "unknown")
    age = minutes_since(storage.last_price_fetch(), now)
    indicators = market_data.load_indicators()
    any_open = any(market_calendar.market_status(cfg, now).status == "open" for cfg in indicators.values())
    if age is None:
        return Freshness("Last price tick", "never", "unknown")
    if not any_open:
        return Freshness("Last price tick", f"{_ago(age)} (markets closed)", "ok")
    return Freshness("Last price tick", _ago(age), level_for(age, config.STALE_PRICE_AMBER_MIN, config.STALE_PRICE_RED_MIN))


def brief_freshness(now):
    """Today's brief (UTC date, the key briefs are saved under). Pending is fine until the catch-up time passes,
    then amber, then red STALE_BRIEF_RED_AFTER_MIN later."""
    today = now.date().isoformat()
    tz = ZoneInfo(config.DESK_TIMEZONE)
    brief = storage.brief_for_date(today)
    if brief:
        made = datetime.fromisoformat(brief["created_at"]).astimezone(tz)
        flag = " (held for review)" if brief["status"] != "published" else ""
        return Freshness("Brief generated", f"{made:%H:%M} {config.DESK_TIMEZONE}{flag}",
                         "ok" if brief["status"] == "published" else "amber")
    local = now.astimezone(tz)
    due = local.replace(hour=config.CATCHUP_AFTER_HOUR, minute=config.CATCHUP_AFTER_MINUTE, second=0, microsecond=0)
    late = (local - due).total_seconds() / 60
    if late < 0:
        return Freshness("Brief generated", f"pending (due {config.BRIEF_HOUR:02d}:{config.BRIEF_MINUTE:02d})", "ok")
    return Freshness("Brief generated", f"not yet today ({late:.0f} min past catch-up time)",
                     "red" if late >= config.STALE_BRIEF_RED_AFTER_MIN else "amber")


def header(now=None):
    """The three freshness indicators shown under the dashboard title.

    In demo mode they are judged at the snapshot's capture time (demo_mode.now()) and never amber or red: the data
    is frozen on purpose, so the level is neutral."""
    now = now or demo_mode.now()
    items = [collection_freshness(now), price_freshness(now), brief_freshness(now)]
    if config.DEMO_MODE:
        for item in items:
            item.level = "unknown"
    return items
