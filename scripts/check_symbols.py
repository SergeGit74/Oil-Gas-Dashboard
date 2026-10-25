"""Symbol discovery: for each indicator and provider, make ONE call and report whether the symbol resolves.

    python scripts/check_symbols.py [--providers twelvedata,yahoo,eia]

Writes reports/symbol_check.json. Keyed providers without a key (and Yahoo without ENABLE_YAHOO_FALLBACK=true) are
skipped with a message. Calls here do NOT go through the quota table, but they do spend provider credits
(Twelve Data: one per symbol). It never changes config/market_symbols.json: review the report and edit by hand.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import market_data  # noqa: E402

REPORT_PATH = Path(__file__).resolve().parent.parent / "reports" / "symbol_check.json"
DELAY_THRESHOLD_MIN = 15


def _providers(names):
    """Build the requested providers; also return a skip message for each one that is not enabled."""
    out, skipped = [], {}
    for name in names:
        built = market_data.build_providers([name])
        if built:
            out.append(built[0])
        elif name == "twelvedata":
            skipped[name] = "skipped: TWELVE_DATA_API_KEY is not set"
        elif name == "yahoo":
            skipped[name] = "skipped: ENABLE_YAHOO_FALLBACK is not true (unofficial endpoint, off by default)"
        elif name == "eia":
            skipped[name] = "skipped: EIA_API_KEY is not set"
    return out, skipped


def check(names=None, indicators=None, providers=None, now=None):
    """Call each provider once per indicator and report whether the symbol resolves, its price, age and apparent delay."""
    now = now or datetime.now(timezone.utc)
    indicators = indicators or market_data.load_indicators()
    if providers is None:
        providers, skipped = _providers(names or config.PRICE_PROVIDERS)
    else:
        skipped = {}
    results = []
    for key, cfg in indicators.items():
        for name, message in skipped.items():
            results.append({"indicator": key, "provider": name, "symbol": cfg["symbols"].get(name),
                            "status": "skipped", "note": message})
        for provider in providers:
            symbol = cfg["symbols"].get(provider.name)
            row = {"indicator": key, "provider": provider.name, "symbol": symbol}
            if not provider.supports(key, cfg):
                results.append({**row, "status": "no_symbol", "note": "no candidate symbol configured"})
                continue
            quotes = provider.fetch_quotes({key: cfg})
            if not quotes:
                results.append({**row, "status": "failed", "note": provider.last_errors.get(key, "no quote")})
                continue
            q = quotes[0]
            age = (now - datetime.fromisoformat(q.as_of)).total_seconds() / 60
            open_now = market_data.in_window(cfg["window"], now)
            results.append({**row, "status": "resolved", "price": q.price, "currency": q.currency,
                            "as_of": q.as_of, "age_minutes": round(age, 1),
                            "reported_delay_minutes": q.delayed_minutes,
                            "appears_delayed": bool(open_now and age > DELAY_THRESHOLD_MIN),
                            "market_open_approx": open_now,
                            "note": "" if open_now else "market closed (approximate window): age is not a delay"})
    return {"generated_at": now.isoformat(timespec="seconds"), "results": results}


def main():
    """Parse arguments, run the symbol check, save the report and print a table."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--providers", help="comma-separated, default: PRICE_PROVIDERS")
    args = parser.parse_args()
    report = check(args.providers.split(",") if args.providers else None)
    REPORT_PATH.parent.mkdir(exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    for r in report["results"]:
        detail = (f"{r['price']} at {r['as_of']} (age {r['age_minutes']} min"
                  + (", looks delayed" if r["appears_delayed"] else "") + ")") if r["status"] == "resolved" else r["note"]
        print(f"{r['indicator']:<10} {r['provider']:<10} {str(r['symbol']):<10} {r['status']:<9} {detail}")
    print(f"\nSaved {REPORT_PATH}")


if __name__ == "__main__":
    main()
