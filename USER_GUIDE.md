# User Guide: Oil & Gas Morning Intelligence Dashboard

A short guide to starting the system and reading the dashboard. For setup, design and known gaps see `README.md`.

## 1. How it fits together

Two separate programs, each in its own terminal window:

| Program | Command | What it does |
|---|---|---|
| **Scheduler** | `python -m jobs.scheduler` | Collects news every 20 minutes, tags and scores it, and writes the daily brief at 05:30 (Asia/Dubai) |
| **Dashboard** | `streamlit run app.py` | Shows what the scheduler has stored. It never collects data itself |

The dashboard alone shows stored data only. Run **both** for live updates. Closing a window stops that program;
the data stays in the database.

## 2. Starting and stopping

Open a terminal in the project folder (the folder you cloned or downloaded, containing `app.py`). Running the venv's programs directly means
you do not need to activate it.

**PowerShell or Command Prompt**
```
.\venv\Scripts\python.exe -m jobs.scheduler          (window 1: scheduler)
.\venv\Scripts\streamlit.exe run app.py              (window 2: dashboard)
```
**Git Bash**
```
./venv/Scripts/python.exe -m jobs.scheduler
./venv/Scripts/streamlit.exe run app.py
```

- The dashboard opens at **http://localhost:8501**. If no browser opens, type that address in yourself.
- The dashboard listens on this PC only (set in `.streamlit/config.toml`). It has no login and "ask the desk" spends
  your API credit, so do not expose it to a network.
- The first time, Streamlit may ask for an email address. Press **Enter** to skip it.
- The scheduler runs one cycle at startup, so expect log lines within a minute, ending in `Scheduler started`.
- If the scheduler was not running at 05:30, it writes the missed brief on its next collection cycle (at startup
  or within 20 minutes), as long as that cycle is after the brief time and today has no brief yet. This costs
  about $0.07, like a normal brief.
- **Stop** either program with **Ctrl+C** in its window.
- After any code change, stop and restart the program; running programs keep the old code.
- Start only one scheduler and one dashboard. Two schedulers double the cost; a second dashboard moves to port 8502.

### Running with Docker instead

If you use Docker, one command starts both programs (scheduler and dashboard); see `README.md` for setup.

```
docker compose up -d --build          start both (first run builds the image)
docker compose logs -f scheduler      watch the scheduler's log (Ctrl+C stops watching, not the scheduler)
docker compose ps                     status and health
docker compose down                   stop both; your data is kept
docker compose up -d --build          after changing code, rebuild and restart
```

The dashboard is at **http://localhost:8501**, reachable from this PC only. Do not also run the Python scheduler
from section 2 against the same project, and do not use `docker compose down -v` unless you want to delete all
stored data. Settings such as the timezone and brief time go in `.env` (see `.env.example`).

## 3. Reading the dashboard

**Top tiles**
- **Brent (USD/bbl):** latest EIA spot price and its last move. EIA data lags the calendar by days.
- **News sentiment:** Bull, Bear or Neutral, with a score from -1 to +1 (Bull at +0.15 or above, Bear at -0.15 or below).
- **Price check:** Confirmed (news and price agree), Partial (one side is neutral), Divergent (they disagree),
  or Unavailable. Three divergent days in a row raise an alert for analyst review.
- **Articles in score:** verified, direction-tagged articles first seen in the last 24 hours.

**Sentiment breakdown and trend:** counts of Bullish, Bearish, Neutral and Mixed articles, and the daily score history.

**Morning brief:** the day's brief with its groundedness score. If a brief failed its checks, the page says it is held
for analyst review and lists the reasons instead of showing the text.

**Verified feed:** articles from the last 24 hours that passed the trust checks, with category, direction, impact
(1 to 5) and confidence. Click the link column to open the story.

**Unverified panel (expander):** articles kept out of the brief, sentiment and search because the source is not
trusted enough or the text looked like a prompt-injection attempt. Review them yourself; do not rely on them.

**Source health:** status of Apify, Serper and EIA. A source shown as degraded for several cycles in a row raises an alert.

**Pipeline cost:** today's total and the Claude, Apify and Serper split against the daily budget, a 14-day chart,
and recent runs. Any cost warnings are listed underneath.

**Ask the desk:** type a question about recent verified coverage. Answers come from stored headlines only, so they
cannot go deeper than the headline. Each question costs about $0.006.

## 4. Refresh control (sidebar)

| Mode | Behavior |
|---|---|
| **With scheduler** (default) | Checks every 30 seconds and reloads only when a new cycle has finished |
| **Auto-refresh** | Reloads on a timer you pick: 1, 5, 10 or 30 minutes |
| **Every 20 min** | Reloads every 20 minutes |
| **Manual** | No automatic reload; press **Refresh now** or reload the page |

The sidebar also shows when the page loaded and when the last collection cycle finished. A warning appears if no
cycle has finished for 45 minutes, which usually means the scheduler is not running. Reloading the browser page
resets the mode to the default.

## 5. Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| Sidebar says "Is the scheduler running?" | Start the scheduler (section 2) |
| Nothing happens when starting the dashboard | It may be waiting for the email prompt (press Enter), or the browser did not open; use http://localhost:8501 |
| `AttributeError: module 'storage' has no attribute ...` | The dashboard is running old code. Stop it (Ctrl+C) and start it again |
| Dashboard shows old data | Scheduler is not running, or the refresh mode is Manual |
| Brief missing or "held for review" | It failed evaluation; the reasons are shown. Generate a new one with `python -m jobs.run_evaluation` |
| Few verified articles | Many outlets are not in the trust registry (`llm_pipeline.py`); unknown outlets stay unverified unless corroborated |
| Cycle costs rising | Check the Pipeline cost panel; most cycles cost only the Apify run (about $0.0002) when no new articles arrive |

## 6. Good to know

- The system is a working draft. Check the brief and the tags against your own judgment before acting on them.
- Nothing here is investment advice.
- Alerts appear in the scheduler window and the job log; add `SLACK_WEBHOOK_URL` to `.env` to also get them in Slack.
