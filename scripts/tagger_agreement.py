"""Report how often the model's article direction matches analyst feedback.

    python scripts/tagger_agreement.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import feedback_tools  # noqa: E402


def main():
    report = feedback_tools.agreement_report()
    print(json.dumps(report, indent=2))
    if not report["judged"]:
        print("\nNo judged articles yet: use the Analyst feedback form on the dashboard first.")


if __name__ == "__main__":
    main()
