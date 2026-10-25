"""Yesterday's total cost (UTC day) against DAILY_COST_BUDGET_USD."""
from datetime import datetime, timedelta, timezone

import alerts
import config
import storage


def check_cost_budget():
    """Alert if yesterday's (UTC) total cost exceeded the daily budget; returns whether it did."""
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    row = next((r for r in storage.daily_costs(days=3) if r["day"] == yesterday), None)
    total = row["total_usd"] if row else 0.0
    if total > config.DAILY_COST_BUDGET_USD:
        alerts.send_alert(f"Pipeline cost for {yesterday} was ${total:.2f}, above the "
                          f"${config.DAILY_COST_BUDGET_USD:.2f} daily budget.", source="cost_budget")
        return True
    return False
