"""Export analyst feedback as a golden-set CSV (reports/golden_set.csv).

    python scripts/export_golden_set.py [output.csv]

Each row is one feedback entry; article-tag rows include the model's direction, category, magnitude and confidence.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import feedback_tools  # noqa: E402

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "reports" / "golden_set.csv"


def main():
    """Write the feedback golden-set CSV to the given path (default reports/golden_set.csv)."""
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PATH
    path.parent.mkdir(exist_ok=True)
    print(f"Wrote {feedback_tools.export_golden_csv(path)} feedback rows to {path}")


if __name__ == "__main__":
    main()
