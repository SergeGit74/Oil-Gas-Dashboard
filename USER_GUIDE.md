# User Guide: Oil & Gas Morning Intelligence Dashboard

A short guide to starting the system, turning features on, and reading the dashboard. For setup, design and known gaps
see `docs/REFERENCE.md`.

## 1. How it fits together

Two separate programs, each in its own terminal window:

| Program | Command | What it does |
|---|---|---|
| **Scheduler** | `python -m jobs.scheduler` | Collects news every 20 minutes, polls market prices every 5 minutes, loads the event calendar, writes the daily brief at 05:30 (Asia/Dubai), and sends alerts and the brief email if you turned them on |
| **Dashboard** | `streamlit run app.py` | Shows what the scheduler has stored. It never collects data or calls a market-data provider itself |

The dashboard alone shows stored data only. Run **both** for live updates. Closing a window stops that program;
the data stays in the database.

## 2. Starting and stopping

Open a terminal in the project folder (the folder containing `app.py`). Running the venv's programs directly means
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
- The dashboard listens on this PC only (set in `.streamlit/config.toml`). "Ask the desk" spends your API credit, so
  do not expose it to a network. It has no login unless you set `DASHBOARD_PASSWORD` (section 3).
- The first time, Streamlit may ask for an email address. Press **Enter** to skip it.
- The scheduler runs an event load, a price poll and a collection cycle at startup, so expect log lines within a
  minute, ending in `Scheduler started`.
- **Missed brief.** If the scheduler was not running at 05:30, it writes the missed brief on its next collection cycle
  (at startup or within 20 minutes), once it is past 06:00 desk time and today has no brief yet. It tries at most
  twice a day, 30 minutes apart, and costs about $0.07 like a normal brief. Until then the dashboard shows "Brief
  pending".
- **Stop** either program with **Ctrl+C** in its window.
- After any code change or any `.env` change, stop and restart the program; running programs keep the old code and
  settings.
- Start only one scheduler and one dashboard. Two schedulers double the cost; a second dashboard moves to port 8502.

### Running with Docker instead

If you use Docker, one command starts both programs (scheduler and dashboard); see `docs/REFERENCE.md` for setup. The Docker
image is built and smoke-tested in GitHub Actions (the "Docker" workflow passed on 2026-10-27: build, contents, demo
dashboard health and render). The `scheduler` and `init` containers and the data volume have not been run yet, so
treat the full stack as untested.

```
docker compose up -d --build          start both (first run builds the image)
docker compose logs -f scheduler      watch the scheduler's log (Ctrl+C stops watching, not the scheduler)
docker compose ps                     status and health
docker compose down                   stop both; your data is kept
docker compose up -d --build          after changing code, rebuild and restart
```

To try the read-only demo with no keys and no `.env` (a frozen snapshot, nothing live, no scheduler):

```
docker compose up --build dashboard-demo      then open http://localhost:8502
```

Always name the service. A bare `docker compose --profile demo up` would also start `init`, `scheduler` and
`dashboard`, because services without a profile are always started.

The dashboard is at **http://localhost:8501**, reachable from this PC only. Do not also run the Python scheduler
from section 2 against the same project, and do not use `docker compose down -v` unless you want to delete all
stored data. Settings such as the timezone and brief time go in `.env` (see `.env.example`).

### Demo mode (no keys, nothing live)

The dashboard can run from a small frozen snapshot of the data, with no API keys, no scheduler and no network:

```
pip install -r demo_app/requirements.txt
$env:DEMO_MODE = "true"; .\venv\Scripts\streamlit.exe run app.py
```

(`streamlit run demo_app/streamlit_app.py` is the same thing without the variable; Streamlit Community Cloud uses it.)
A banner on every page says the data is frozen; "today", staleness and market open/closed are judged at the moment the
snapshot was captured; feedback and "ask the desk" are disabled; the scheduler and every job refuse to run.

To refresh the snapshot from your own data (read-only on your store, no API calls):

```
.\venv\Scripts\python.exe scripts/make_demo_snapshot.py --force      # --days 7 by default
```

It prints counts per table and the result of a safety scan for keys, e-mail addresses and file paths, and it refuses
to write the file if the scan finds any. Review the summary and the file before you commit `demo/demo_store.db`
and `demo/snapshot_meta.json`; the script never runs `git add`. After a schema change the dashboard tells you to
regenerate the snapshot.

## 3. Turning features on (`.env`)

Edit `.env` in the project folder (never commit it or paste it anywhere), then restart the scheduler and dashboard.
`.env.example` lists every setting with its default. Values are unquoted: `NAME=value`.

| Feature | Settings | Notes |
|---|---|---|
| **Market prices** | `ENABLE_YAHOO_FALLBACK=true` and/or `TWELVE_DATA_API_KEY`; `EIA_API_KEY` is already used | Without Yahoo or Twelve Data the price tiles show only Brent and WTI (EIA daily spot). Yahoo is an unofficial endpoint with no published terms; check them before relying on it. `python scripts/check_symbols.py` shows which symbols work |
| **Slack alerts** | `SLACK_WEBHOOK_URL` | Each verified story with impact 4 or 5 is posted once, at most 5 an hour (`ALERT_MIN_MAGNITUDE`, `ALERT_MAX_PER_HOUR`). Nothing unverified is ever posted. Without a webhook nothing is sent |
| **Brief email** | `EMAIL_ENABLED=true`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_FROM`, `EMAIL_TO` (comma-separated), optional `EMAIL_OWNER` | A published brief goes to everyone in `EMAIL_TO`. A brief that failed its checks is never mailed to the list: only the owner (`EMAIL_OWNER`, else the first `EMAIL_TO` address) gets a short "needs review" note. The connection must use TLS. Preview without sending: `python -m jobs.send_brief_email --dry-run` (the `.eml` file appears in `outbox/`) |
| **Dashboard password** | `DASHBOARD_PASSWORD` | One shared password for local use; five wrong guesses lock the login for 15 minutes for everyone. Real deployments need proper authentication in front of the dashboard |
| **PostgreSQL** | `DATABASE_URL` | Optional; SQLite is the default. Create an empty database, run `python scripts/copy_sqlite_to_postgres.py --pg-url <url> --dry-run`, then without `--dry-run`, then set `DATABASE_URL` and restart both programs. Your SQLite file is never changed or deleted |
| **OPEC+ meetings** | `config/events.yaml` | They appear in "Scheduled events" only if you add them there with a source link. FOMC dates and EIA releases are loaded automatically |

## 4. Reading the dashboard

**Header row (under the title):** the age of the last collection, the last price fetch and when today's brief was
generated. Green is normal, orange is late and red means something is probably not running. The price age is judged
only while a market is open.

**Market prices:** eight tiles (Brent, WTI, Henry Hub, TTF, gold, DXY, S&P 500, ULSD) with the latest price, change,
quote time, the data source, a "delayed N min" label when the provider reports one, and a 24-hour chart. A tile for
a closed market says "Closed, last close <date>" instead of showing a move, and an open market with an old quote is
marked stale. Below them are the Brent-WTI spread and the gold/Brent ratio; they show n/a if their two quotes are
more than a day apart. Brent and TTF use proxy exchange calendars, so their open/closed labels are indicative.

**EIA weekly data:** US crude stocks, US distillate stocks and Lower 48 gas storage with the week-on-week change.

**Scheduled events, next 7 days:** EIA release times, FOMC dates and anything you added to `config/events.yaml`.

**Top tiles**
- **Brent (USD/bbl):** latest EIA spot price and its last move. EIA data lags the calendar by days.
- **News sentiment:** Bull, Bear or Neutral, with a score from -1 to +1 (Bull at +0.15 or above, Bear at -0.15 or below).
- **Price check:** Confirmed (news and price agree), Partial (one side is neutral), Divergent (they disagree),
  or Unavailable. Three divergent days in a row raise an alert for analyst review.
- **Articles in score:** the number of verified, direction-tagged stories first seen in the last 24 hours. Near-
  duplicate headlines count once.

**Sentiment breakdown and trend:** counts of Bullish, Bearish, Neutral and Mixed stories, and the daily score history.

**Morning brief:** the day's brief with its groundedness score. From the second day on it has a "What changed since
yesterday" section, computed from the two days' key points. If a brief failed its checks, the page says it is held for
analyst review and lists the reasons instead of showing the text. "Brief pending" means none exists yet today.

**Verified feed:** stories from the last 24 hours that passed the trust checks, one row per story with the number of
outlets that reported it, plus category, direction, impact (1 to 5) and confidence. Click the link column to open it.

**Unverified panel (expander):** articles kept out of the brief, sentiment, alerts and search because the source is not
trusted enough or the text looked like a prompt-injection attempt. Review them yourself; do not rely on them.

**Source health:** status of Apify, Serper, EIA, market prices, the event calendar, Slack alerts and the brief email.
A source shown as degraded for several cycles in a row raises an alert and a warning here.

**Pipeline cost:** today's total and the Claude, Apify and Serper split against the daily budget, a 14-day chart,
and recent runs. Any cost warnings are listed underneath. Price polling costs nothing and is not listed.

**Analyst feedback (expander):** mark a verified article's tag, or a section of the published brief, as right or wrong,
optionally with the correct direction and a comment. `python scripts/tagger_agreement.py` shows how often the model
agrees with you and `python scripts/export_golden_set.py` writes the feedback to `reports/golden_set.csv`.

**Ask the desk:** type a question about recent verified coverage. Answers come from stored headlines only, so they
cannot go deeper than the headline. Each question costs about $0.006.

**Research page (sidebar):** compares each past day's sentiment with Brent's next-day and three-day moves. It shows
"insufficient data" until about 30 days of both exist, and it is descriptive only, not a forecast.

## 5. Refresh control (sidebar)

| Mode | Behavior |
|---|---|
| **With scheduler** (default) | Checks every 30 seconds and reloads only when a new cycle has finished |
| **Auto-refresh** | Reloads on a timer you pick: 1, 5, 10 or 30 minutes |
| **Every 20 min** | Reloads every 20 minutes |
| **Manual** | No automatic reload; press **Refresh now** or reload the page |

The price tiles refresh themselves every 60 seconds in every mode. The sidebar also shows when the page loaded and
when the last collection cycle finished, with a warning if no cycle has finished for 45 minutes (usually the
scheduler is not running). Reloading the browser page resets the mode to the default.

## 6. Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| Sidebar says "Is the scheduler running?" | Start the scheduler (section 2) |
| Nothing happens when starting the dashboard | It may be waiting for the email prompt (press Enter), or the browser did not open; use http://localhost:8501 |
| `AttributeError: module 'storage' has no attribute ...` | The dashboard is running old code. Stop it (Ctrl+C) and start it again |
| Dashboard shows old data | Scheduler is not running, or the refresh mode is Manual |
| Only Brent and WTI have prices | Only the EIA fallback is enabled. Set `ENABLE_YAHOO_FALLBACK=true` or add a Twelve Data key, then restart the scheduler |
| "Market prices" shows degraded in Source health | Some indicators got no quote from any provider. The job log shows which ones and why |
| Brief missing or "held for review" | It failed evaluation; the reasons are shown. Generate a new one with `python -m jobs.run_evaluation` |
| No brief email arrived | `EMAIL_ENABLED` is not `true`, the brief was held for review (only the owner is told), or SMTP failed: check "Brief email" in Source health and the job log. It never retries more than 3 times a day |
| "Too many failed attempts" at the login | Wait for the lockout to end (15 minutes by default) or restart the dashboard |
| Few verified articles | Many outlets are not in the trust registry (`llm_pipeline.py`); unknown outlets stay unverified unless corroborated |
| Cycle costs rising | Check the Pipeline cost panel; most cycles cost only the Apify run (about $0.0002) when no new articles arrive |

## 7. Good to know

- The system is a working draft. Check the brief and the tags against your own judgment before acting on them.
- Nothing here is investment advice.
- System alerts (a source down, a budget overrun) appear in the scheduler window and the job log; add
  `SLACK_WEBHOOK_URL` to `.env` to also get them in Slack.
- Slack alerts and the brief email have only been tested with mocks so far; send yourself a test first.
