"""Price poller: fetch due indicators through the provider chain, store ticks, log status and counters."""
import logging
from datetime import datetime, timezone

import config
import cost_tracker
import market_data
import storage

log = logging.getLogger("run_prices")


def _chunks(items, size):
    """Split a list into consecutive pieces of at most `size`."""
    for i in range(0, len(items), max(1, size)):
        yield items[i:i + size]


def run_prices(now=None, providers=None, quota=None, indicators=None):
    """One poll. Returns a summary dict. `providers`, `quota` and `indicators` can be injected (tests)."""
    now = now or datetime.now(timezone.utc)
    indicators = indicators or market_data.load_indicators()
    providers = market_data.build_providers() if providers is None else providers
    quota = quota or market_data.QuotaManager()
    if not providers:
        storage.log_job("fetch_prices", "degraded", "no price provider enabled (set TWELVE_DATA_API_KEY, "
                        "ENABLE_YAHOO_FALLBACK=true or EIA_API_KEY)")
        return {"due": 0, "stored": 0, "missing": list(indicators)}

    floor = market_data.budget_floor_minutes(market_data.assign_providers(indicators, providers), quota.limits)
    due = market_data.due_indicators(indicators, storage.last_fetch_times(), now, floor)
    if not due:
        storage.log_job("fetch_prices", "skipped", "nothing due")
        return {"due": 0, "stored": 0, "missing": []}

    remaining, quotes, notes = list(due), [], []
    for provider in providers:
        todo = [k for k in remaining if provider.supports(k, indicators[k])]
        if not todo:
            continue
        # Twelve Data charges one credit per symbol, so a request is split to fit the per-minute limit.
        for chunk in _chunks(todo, quota.per_minute(provider.name)):
            if not quota.reserve(provider.name, len(chunk)):
                notes.append(f"{provider.name}: daily quota cap reached, {len(chunk)} call(s) refused")
                continue
            quota.pace(provider.name, len(chunk))
            got = provider.fetch_quotes({k: indicators[k] for k in chunk})
            cost_tracker.incr(f"price_calls_{provider.name}", len(chunk))
            cost_tracker.incr(f"price_quotes_{provider.name}", len(got))
            quotes += got
            remaining = [k for k in remaining if k not in {q.indicator_key for q in got}]
            notes += [f"{provider.name} {k}: {e}" for k, e in provider.last_errors.items()]

    fetched_at = now.isoformat(timespec="seconds")
    storage.save_ticks([{**q.__dict__, "ts": q.as_of, "fetched_at": fetched_at} for q in quotes])
    if not quotes:
        status = "degraded"
    else:
        status = "success" if not remaining else "degraded"
    storage.log_job("fetch_prices", status,
                    f"{len(quotes)}/{len(due)} stored" + (f"; missing {','.join(remaining)}" if remaining else "")
                    + (f"; {'; '.join(notes[:4])}" if notes else ""))
    return {"due": len(due), "stored": len(quotes), "missing": remaining}


def purge_old_ticks():
    """Roll old ticks into daily closes, purge them, and log the result."""
    days, deleted = storage.purge_ticks(config.PRICE_TICK_RETENTION_DAYS)
    storage.log_job("price_retention", "success", f"{days} day rows rolled up, {deleted} ticks purged")
    return days, deleted


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    with cost_tracker.cost_run("price_poll"):
        print(run_prices())
