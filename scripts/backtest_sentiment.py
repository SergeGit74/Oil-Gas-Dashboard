"""Descriptive backtest of news sentiment against subsequent Brent returns (see backtest.py).

    python scripts/backtest_sentiment.py

Reads sentiment_history and price_daily only; writes reports/backtest.json. Descriptive, not predictive.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backtest  # noqa: E402

REPORT_PATH = Path(__file__).resolve().parent.parent / "reports" / "backtest.json"


def main():
    report = backtest.run_backtest()
    REPORT_PATH.parent.mkdir(exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Sentiment days: {report['sentiment_days']}, {report['benchmark']} daily closes: {report['price_days']}")
    for horizon, res in report["results"].items():
        if res["insufficient"]:
            print(f"{horizon}: insufficient data ({res['n']} paired observations, {res['needed']} needed)")
            continue
        print(f"{horizon}: n={res['n']}, correlation={res['correlation']}")
        for label, v in res["by_label"].items():
            print(f"   {label:<8} n={v['n']:<4} hit rate={v['hit_rate']}")
    print("\nDescriptive only: small samples, LLM-tagged sentiment, no test of any trading rule.")
    print(f"Saved {REPORT_PATH}")


if __name__ == "__main__":
    main()
