"""Compare the sentiment label with the Brent move from EIA: Confirmed / Partial / Divergent."""

NEUTRAL_MOVE_PCT = 0.5


def confirm_price(label, eia_data):
    """eia_data: output of data_sources.fetch_eia_data(). Returns benchmark, price, change_pct, status."""
    series = (eia_data or {}).get("brent") or []
    if len(series) < 2 or not series[1][1]:
        return {"benchmark": "Brent", "price": None, "change_pct": None, "status": "Unavailable"}
    latest, previous = series[0][1], series[1][1]
    change = (latest - previous) / previous * 100
    move = "Neutral" if abs(change) < NEUTRAL_MOVE_PCT else "Bull" if change > 0 else "Bear"
    if label == move:
        status = "Confirmed"
    elif "Neutral" in (label, move):
        status = "Partial"
    else:
        status = "Divergent"
    return {"benchmark": "Brent", "price": latest, "change_pct": round(change, 2), "status": status}
