"""Container healthchecks. Exit code 0 = healthy, 1 = unhealthy.

    python healthcheck.py dashboard   # Streamlit answers on its health endpoint
    python healthcheck.py scheduler   # a collection cycle finished recently
"""
import sys
import urllib.request
from datetime import datetime, timezone

import config


def check_dashboard(port=8501):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/_stcore/health", timeout=5) as resp:
            return resp.status == 200, "dashboard answering"
    except Exception as exc:
        return False, f"dashboard not answering: {exc}"


def check_scheduler():
    import storage  # imported here so the dashboard check has no database dependency

    finished = storage.last_cycle_finished()
    if not finished:
        return False, "no collection cycle recorded yet"
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(finished)).total_seconds() / 60
    return age <= config.STALE_AFTER_MINUTES, f"last collection cycle {age:.0f} min ago"


if __name__ == "__main__":
    checks = {"dashboard": check_dashboard, "scheduler": check_scheduler}
    if len(sys.argv) != 2 or sys.argv[1] not in checks:
        sys.exit(f"usage: python healthcheck.py {{{'|'.join(checks)}}}")
    ok, message = checks[sys.argv[1]]()
    print(message)
    sys.exit(0 if ok else 1)
