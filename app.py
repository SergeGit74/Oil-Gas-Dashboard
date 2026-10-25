"""Streamlit dashboard. Reads from the shared store; the only write path is 'ask the desk',
which is wrapped in cost_run so its Claude spend is recorded."""
import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st

import config
import cost_tracker
import freshness
import market_calendar
import market_data
import storage

st.set_page_config(page_title="Oil & Gas Morning Intelligence", layout="wide")
storage.init_db()

# ---- refresh control ------------------------------------------------------
# The dashboard only reads the store; new data comes from the scheduler process. These modes control when the
# open page re-reads it.
REFRESH_MODES = ["With scheduler", "Auto-refresh", "Every 20 min", "Manual"]
AUTO_INTERVALS = {"1 minute": 60, "5 minutes": 300, "10 minutes": 600, "30 minutes": 1800}
SCHEDULER_POLL_SECONDS = 30  # cheap database check, reloads only if a new cycle finished
EVERY_20_MIN_SECONDS = 1200
STALE_AFTER_MINUTES = config.STALE_AFTER_MINUTES  # a cycle this overdue suggests the scheduler is not running


def _install_refresher(interval_seconds, only_if_new_data):
    """Timed fragment that re-runs the whole page. The first tick after each full run is skipped so the page
    does not reload immediately."""
    st.session_state["refresher_armed"] = False

    @st.fragment(run_every=interval_seconds)
    def _refresher():
        if not st.session_state.get("refresher_armed"):
            st.session_state["refresher_armed"] = True
            return
        if only_if_new_data and storage.latest_marker() == st.session_state["data_marker"]:
            return
        st.rerun()

    _refresher()


st.session_state["data_marker"] = storage.latest_marker()
now_utc = datetime.now(timezone.utc)
with st.sidebar:
    st.header("Refresh")
    mode = st.radio("Refresh mode", REFRESH_MODES, key="refresh_mode",
                    help="With scheduler: reload when the scheduler finishes a new cycle. Auto-refresh: reload on "
                         "a timer you choose. Every 20 min: reload on the scheduler's cycle length. "
                         "Manual: reload the browser page yourself.")
    if mode == "Auto-refresh":
        label = st.selectbox("Reload every", list(AUTO_INTERVALS), index=1, key="auto_interval")
        _install_refresher(AUTO_INTERVALS[label], only_if_new_data=False)
    elif mode == "Every 20 min":
        _install_refresher(EVERY_20_MIN_SECONDS, only_if_new_data=False)
    elif mode == "With scheduler":
        _install_refresher(SCHEDULER_POLL_SECONDS, only_if_new_data=True)
    st.button("Refresh now")  # any interaction re-runs the page against the latest data

    st.caption(f"Page loaded: {now_utc:%H:%M:%S} UTC")
    finished = storage.last_cycle_finished()
    if finished:
        age_min = (now_utc - datetime.fromisoformat(finished)).total_seconds() / 60
        st.caption(f"Last collection cycle: {finished[11:19]} UTC ({age_min:.0f} min ago)")
        if age_min > STALE_AFTER_MINUTES:
            st.warning("No collection cycle for a while. Is the scheduler running? "
                       "Start it with `python -m jobs.scheduler`.")
    else:
        st.caption("No collection cycle recorded yet. Start `python -m jobs.scheduler`.")

st.title("Oil & Gas Morning Intelligence")
st.caption("Automated collection, verification and briefing. Not investment advice; review flagged items.")

# ---- data freshness -------------------------------------------------------
# Computed from job_runs, price_ticks and briefs (see freshness.py); amber and red thresholds are in config.
for col, item in zip(st.columns(3), freshness.header()):
    col.markdown(f"**{item.label}**  \n{item.colored}")

# ---- market prices --------------------------------------------------------
# Reads only price_ticks (filled by the scheduler's price poller); never calls a market-data provider.
PRICE_REFRESH = "60s"


def _fmt_price(value, decimals):
    return f"{value:,.{decimals}f}"


@st.fragment(run_every=PRICE_REFRESH)
def price_panel():
    st.subheader("Market prices")
    indicators = market_data.load_indicators()
    ticks = storage.latest_ticks()
    if not ticks:
        st.info("No prices yet. The scheduler's price poller fills this: `python -m jobs.scheduler` "
                "(needs TWELVE_DATA_API_KEY, ENABLE_YAHOO_FALLBACK=true or EIA_API_KEY in .env).")
        return
    now = datetime.now(timezone.utc)
    since = storage.hours_ago(24)
    for row_keys in (list(indicators)[:4], list(indicators)[4:]):
        for col, key in zip(st.columns(4), row_keys):
            cfg, tick = indicators[key], ticks.get(key)
            with col.container(border=True):
                if not tick:
                    st.metric(f"{cfg['label']} ({cfg['unit']})", "n/a")
                    st.caption("No data yet")
                    continue
                as_of = datetime.fromisoformat(tick["ts"])
                age_min = (now - as_of).total_seconds() / 60
                status = market_calendar.market_status(cfg, now)
                is_open = status.status == "open"
                # A closed market shows its last close, not a change presented as an overnight move.
                st.metric(f"{key} ({cfg['unit']})", _fmt_price(tick["price"], cfg["decimals"]),
                          f"{tick['change_pct']:+.2f}%" if is_open and tick["change_pct"] is not None else None)
                notes = [f"as of {as_of:%d %b %H:%M} UTC", tick["provider"]]
                if tick["delayed_minutes"]:
                    notes.append(f"delayed {tick['delayed_minutes']} min")
                st.caption(" | ".join(notes))
                if is_open:
                    if age_min > config.PRICE_STALE_MINUTES:
                        st.caption(f":orange[Stale: last quote {age_min:.0f} min old]")
                else:
                    last_close = datetime.fromisoformat(status.last_close) if status.last_close else as_of
                    st.caption(f"Closed{' (holiday)' if status.status == 'holiday' else ''}, "
                               f"last close {last_close:%d %b %Y}"
                               + (f". Last session {tick['change_pct']:+.2f}%" if tick["change_pct"] is not None else ""))
                series = storage.price_series(key, since)
                if len(series) > 1:
                    st.line_chart(pd.DataFrame(series).set_index("ts")["price"], height=70)

    derived = market_data.derived_metrics(ticks)
    d1, d2 = st.columns(2)
    spread, ratio = derived["brent_wti_spread"], derived["gold_brent_ratio"]
    d1.metric("Brent-WTI spread (USD/bbl)", f"{spread['value']:+.2f}" if spread else "n/a")
    d1.caption(f"{spread['provider']}, as of {spread['as_of'][:16].replace('T', ' ')} UTC" if spread
               else "Needs both quotes from the same provider within 24 h")
    d2.metric("Gold / Brent (bbl per oz)", f"{ratio['value']:.2f}" if ratio else "n/a")
    d2.caption(f"{ratio['provider']}, as of {ratio['as_of'][:16].replace('T', ' ')} UTC" if ratio
               else "Needs both quotes within 24 h")


price_panel()

# ---- EIA weekly and scheduled events --------------------------------------
# Both read tables filled by the scheduler (eia_weekly, events); the dashboard never calls EIA.
st.subheader("EIA weekly data")
weekly_cols = st.columns(3)
for col, (key, label, unit) in zip(weekly_cols, (("crude_stocks", "US crude stocks", "thousand bbl"),
                                                  ("distillate_stocks", "US distillate stocks", "thousand bbl"),
                                                  ("gas_storage", "Lower 48 gas storage", "Bcf"))):
    rows = storage.eia_weekly(key, 2)
    if rows:
        change = rows[0]["value"] - rows[1]["value"] if len(rows) > 1 else None
        col.metric(f"{label} ({unit})", f"{rows[0]['value']:,.0f}",
                   f"{change:+,.0f} vs prior week" if change is not None else None, delta_color="off")
        col.caption(f"week ending {rows[0]['period']}")
    else:
        col.metric(f"{label} ({unit})", "n/a")

st.subheader("Scheduled events, next 7 days")
today_utc = datetime.now(timezone.utc).date()
upcoming = storage.events_between(today_utc.isoformat(), (today_utc + timedelta(days=7)).isoformat())
if upcoming:
    st.dataframe(pd.DataFrame(upcoming)[["date", "time", "tz", "title", "category", "source_url"]],
                 width="stretch", hide_index=True, column_config={"source_url": st.column_config.LinkColumn("source")})
else:
    st.write("No scheduled events in the table for the next 7 days.")

history = storage.sentiment_history(30)
latest = history[0] if history else None

# ---- price and sentiment tiles --------------------------------------------
c1, c2, c3, c4 = st.columns(4)
if latest:
    price = latest["price_value"]
    c1.metric(f"{latest['price_benchmark'] or 'Brent'} (USD/bbl)", f"{price:.2f}" if price else "n/a",
              f"{latest['price_change_pct']:+.2f}%" if latest["price_change_pct"] is not None else None)
    c2.metric("News sentiment", latest["label"], f"score {latest['score']:+.2f}")
    c3.metric("Price check", latest["confirmation"] or "n/a")
    c4.metric("Articles in score", latest["n_articles"])
else:
    st.info("No sentiment data yet. Start the scheduler: `python -m jobs.scheduler`.")

if latest:
    left, right = st.columns([1, 2])
    with left:
        st.subheader("Sentiment breakdown")
        counts = json.loads(latest["counts_json"] or "{}")
        st.bar_chart(pd.Series(counts, name="articles"))
    with right:
        st.subheader("Sentiment trend")
        trend = pd.DataFrame(history).sort_values("date").set_index("date")[["score"]]
        st.line_chart(trend)

# ---- morning brief --------------------------------------------------------
st.header("Morning brief")
brief = storage.latest_brief()
if not brief:
    st.info("Brief pending. The scheduler writes it daily at "
            f"{config.BRIEF_HOUR:02d}:{config.BRIEF_MINUTE:02d} desk time, or catches up after "
            f"{config.CATCHUP_AFTER_HOUR:02d}:{config.CATCHUP_AFTER_MINUTE:02d} if it was missed.")
else:
    if brief["date"] < now_utc.date().isoformat():
        st.info(f"Today's brief is pending. Showing the brief for {brief['date']}.")
    if brief["status"] == "published":
        st.caption(f"{brief['date']} | published | groundedness {brief['groundedness']:.2f}")
        st.markdown(brief["text"])
    else:
        st.warning(f"The brief for {brief['date']} failed evaluation and is held for analyst review. "
                   "It is not shown here.")
        evaluation = json.loads(brief["eval_json"] or "{}")
        for reason in evaluation.get("reasons", []):
            st.write(f"- {reason}")

# ---- verified feed and unverified panel -----------------------------------
st.header("Verified feed (last 24h)")
verified = storage.verified_articles(storage.hours_ago(24), limit=100)
if verified:
    df = pd.DataFrame(verified)[["title", "source", "category", "direction", "magnitude", "confidence",
                                 "content_type", "url"]]
    st.dataframe(df, width="stretch", hide_index=True,
                 column_config={"url": st.column_config.LinkColumn("link")})
else:
    st.write("No verified articles in the last 24 hours.")

with st.expander("Unverified / low-confidence (not used in the brief, sentiment or search)"):
    unverified = storage.unverified_articles(storage.hours_ago(24))
    if unverified:
        st.dataframe(pd.DataFrame(unverified)[["title", "source", "source_tier", "confidence", "reason", "url"]],
                     width="stretch", hide_index=True)
    else:
        st.write("None.")

# ---- source health --------------------------------------------------------
st.header("Source health")
from jobs.source_health import SOURCE_HEALTH_CONFIG, check_source_health, current_streak  # noqa: E402

# notify=False: read-only, so a page load can never send an alert or write to job_runs.
for job in check_source_health(notify=False):
    st.warning(f"{SOURCE_HEALTH_CONFIG[job]['label']} has been degraded for "
               f"{current_streak(job)} consecutive cycles.")
cols = st.columns(len(SOURCE_HEALTH_CONFIG))
for col, (job, cfg) in zip(cols, SOURCE_HEALTH_CONFIG.items()):
    streak = current_streak(job)
    runs = storage.recent_job_runs(job, 1)
    col.metric(cfg["label"], runs[0]["status"] if runs else "no data",
               f"{streak} consecutive bad" if streak else None, delta_color="inverse")
with st.expander("Recent job log"):
    st.dataframe(pd.DataFrame(storage.latest_job_runs(30)), width="stretch", hide_index=True)

# ---- pipeline cost --------------------------------------------------------
st.header("Pipeline cost")
daily = storage.daily_costs(14)
if daily:
    today = daily[-1]
    m1, m2, m3, m4 = st.columns(4)
    m1.metric(f"Total ({today['day']}, UTC)", f"${today['total_usd']:.2f}",
              f"budget ${config.DAILY_COST_BUDGET_USD:.0f}", delta_color="off")
    m2.metric("Claude", f"${today['claude_usd']:.2f}")
    m3.metric("Apify", f"${today['apify_usd']:.2f}")
    m4.metric("Serper", f"${today['serper_usd']:.2f}")
    st.bar_chart(pd.DataFrame(daily).set_index("day")[["claude_usd", "apify_usd", "serper_usd"]])
    runs = pd.DataFrame(storage.recent_run_costs(15))
    st.dataframe(runs[["started_at", "job_name", "status", "claude_usd", "apify_usd", "serper_usd", "total_usd"]],
                 width="stretch", hide_index=True)
    warnings = {w for r in storage.recent_run_costs(15)
                for w in json.loads(r["details_json"] or "{}").get("warnings", [])}
    for w in sorted(warnings):
        st.caption(f"Cost warning: {w}")
else:
    st.write("No cost data yet.")

# ---- ask the desk ---------------------------------------------------------
st.header("Ask the desk")
question = st.text_input("Question about recent verified coverage")
if question:
    import rag_pipeline  # imported lazily: loads the vector store and embedding client

    try:
        with cost_tracker.cost_run("ask_desk"):
            answer, hits = rag_pipeline.answer_question(question)
        st.markdown(answer)
        with st.expander("Sources"):
            for h in hits:
                st.write(f"- {h['date']} [{h['title']}]({h['url']})")
    except Exception as exc:
        st.error(f"Could not answer: {exc}")
