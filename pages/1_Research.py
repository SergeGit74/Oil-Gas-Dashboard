"""Research page: descriptive backtest of news sentiment against subsequent Brent returns. Read-only."""
import pandas as pd
import streamlit as st

import auth
import backtest
import config
import demo_mode
import storage

st.set_page_config(page_title="Research | Oil & Gas Morning Intelligence", layout="wide")
auth.require_login()
demo_mode.start_page()  # DEMO_MODE only: stops if the snapshot is unusable, else shows the banner
storage.init_db()

st.title("Research: sentiment versus Brent")
st.warning("Descriptive, not predictive. This shows how past daily news sentiment lined up with the Brent return that "
           "followed, on a small and noisy sample with LLM-generated tags. It does not test any trading rule and is "
           "not investment advice.")

report = backtest.run_backtest(today=demo_mode.today())
st.caption(f"{report['sentiment_days']} sentiment days and {report['price_days']} {report['benchmark']} daily closes "
           f"stored. Only completed days are used; each return starts at that day's close, so no earlier price is used. "
           f"Closes from different providers are never mixed. Fewer than {config.BACKTEST_MIN_OBSERVATIONS} paired "
           f"observations are not analysed.")

for horizon, title in (("next_day", "Next available day"), ("three_day", "Three available days later")):
    res = report["results"][horizon]
    st.subheader(title)
    if res["insufficient"]:
        st.info(f"Insufficient data: {res['n']} paired observations so far, {res['needed']} needed.")
        continue
    c1, c2 = st.columns([1, 2])
    c1.metric("Paired observations", res["n"])
    c1.metric("Correlation (score vs return)", "n/a" if res["correlation"] is None else f"{res['correlation']:+.2f}")
    table = pd.DataFrame([{"label": k, "n": v["n"], "hits": v["hits"],
                           "hit rate": "n/a" if v["hit_rate"] is None else f"{v['hit_rate']:.0%}"}
                          for k, v in res["by_label"].items()])
    c2.dataframe(table, hide_index=True, width="stretch")
    c2.caption("Hit: Bull followed by a rise, Bear by a fall, Neutral by a move under 0.5%.")
    points = pd.DataFrame([o for o in report["observations"] if o.get(horizon) is not None])
    st.scatter_chart(points, x="score", y=horizon, height=260)

with st.expander("Paired observations"):
    st.dataframe(pd.DataFrame(report["observations"]), width="stretch", hide_index=True)
