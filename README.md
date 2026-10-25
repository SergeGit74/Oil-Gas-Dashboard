# Oil & Gas Morning Intelligence Dashboard

An automated system that collects oil and gas market news and related geopolitical/macro news, scores each
item for source trust and confidence, tags it for market impact and Bull/Bear/Neutral direction, grounds an
LLM-generated morning brief in verified content only, checks the brief before publishing, tracks what each run
costs, and serves everything through a live Streamlit dashboard. It is built for oil and gas market analysts and
physical contract traders who need a reliable pre-market briefing.

This project is designed to be built and extended with **Claude Code**.

**Status:** working draft. The collection cycle, tagging, sentiment and price check, ingestion, brief generation
and evaluation, "ask the desk" and the dashboard have each been run against the live APIs once or a few times.
The price poller, event calendar, story clustering and corroboration fix have run live for short periods. Not yet
exercised: Slack delivery and the brief email against real services (only mocks so far), the cost-budget,
evaluation-trend and sentiment-divergence alerts firing, the backtest (it needs about 30 days of prices), a hosted
PostgreSQL, the Docker image, and long unattended scheduler runs. Treat the "Known gaps" section as a checklist before relying on any output for
trading decisions or cost reporting.

**Disclaimer:** this is not financial, investment or trading advice. Briefs, tags, sentiment and price checks are
generated automatically by language models and data feeds, can be wrong or out of date (for example, EIA price data
lags by days), and have not been independently validated. Check anything you rely on against primary sources.

## What the system does

1. **Collects** news from the Apify actor `topsail/compliant-commodity-intel` (OilPrice, World Oil, Rigzone),
   GDELT's free 15-minute GKG files (energy-related stories from many outlets, titles only), and official data
   from the EIA API.
2. **Retries and falls back**: Apify runs use exponential backoff and are aborted if they exceed a time limit;
   if the actor stays unavailable, the cycle continues on GDELT alone and is logged as `degraded`.
3. **Skips articles already stored**: URLs are normalized and checked against the store, so Claude and Serper
   only run on new articles.
4. **Scores source trust** with a static tiered registry (official data, wire/trade press, aggregators, unknown).
5. **Separates news from commentary** with rule-based signals (byline, section tags, headline phrasing).
6. **Corroborates claims** with Serper (Google Search API), run selectively on new, lower-trust, higher-impact
   articles to conserve quota.
7. **Tags articles** with Claude tool use (schema-validated, in batches of 25): category, direction
   (Bullish/Bearish/Neutral/Mixed), impact magnitude (1-5). Confidence is derived from source tier,
   content type and corroboration.
8. **Computes market sentiment** from stored articles: a daily Bull/Bear/Neutral score over verified,
   direction-tagged articles first seen in the last 24 hours, weighted by magnitude and confidence.
9. **Checks sentiment against price**: compares the sentiment label with the Brent move from EIA and flags
   Confirmed, Partial, or Divergent; alerts after consecutive days of divergence.
10. **Treats scraped content as untrusted data**: injection-pattern flagging with exclusion, delimiters around
    content in prompts, and low-confidence items kept out of the LLM prompt entirely.
11. **Indexes verified articles** (RAG) in a vector store for historical context in briefs and "ask the desk" queries.
12. **Generates the morning brief** from verified articles plus the sentiment reading and EIA context.
13. **Evaluates the brief before publishing**: claim decomposition, entailment against sources, numeric
    fidelity, and coverage of high-impact stories. Failures are flagged for analyst review, not published.
14. **Monitors source health**: consecutive-degradation alerts for Apify, Serper, and EIA, with optional Slack
    notification.
15. **Calculates the cost of every run** (Claude Sonnet, Apify, Serper), stores it per run with a per-step
    breakdown, shows it on the dashboard, and alerts if a day's total exceeds a budget.
16. **Runs all heavy work as scheduled background jobs**; the dashboard only reads from a shared store.

## Architecture

```
jobs/scheduler.py (long-running process, APScheduler)
|
|  every 20 min  [one costed run: collection_cycle]
|    run_collection: Apify (retry/fallback, costed) + GDELT -> normalize URLs -> dedupe
|                    -> skip URLs already stored -> Claude tagging of NEW articles only (costed)
|                    -> selective Serper corroboration of new articles (costed) -> save
|                    -> sentiment from stored last-24h verified articles -> EIA price check
|                    -> weekly EIA stocks / gas storage into eia_weekly (at most every 6 h)
|                    -> Slack alert per high-impact verified story (high_impact_alerts)
|                    -> save sentiment snapshot
|    run_ingestion:  embed verified articles into the Chroma vector store (RAG)
|    send_brief_email: once per day, if enabled and today's brief is unsent (published -> list, flagged -> owner)
|    catch_up_brief: past 06:00 desk time with no brief for today -> claim, then run_evaluation
|    source_health:  consecutive-degradation alerts for Apify, Serper, EIA
|
|  every 5 min  [one zero-cost run: price_poll]
|    (a price tick is due on each indicator's own calendar-aware interval)
|    run_prices: due indicators -> Twelve Data / Yahoo / EIA fallback chain -> price_ticks
|  daily 04:00: load_events (config/events.yaml + EIA release schedules -> events table)
|  daily 03:10: price_retention (roll ticks into price_daily, purge ticks older than 30 days)
|
|  daily 05:30  [one costed run: daily_brief_cycle, takes the same brief claim]
|    run_evaluation -> check_eval_trend -> source_health -> check_sentiment_divergence
|    then check_cost_budget (yesterday's total vs DAILY_COST_BUDGET_USD)
|
v
storage.py (shared store, SQLite or optional PostgreSQL: articles, briefs, sentiment_history, job_runs, run_costs, run_claims,
|           price_ticks, price_daily, api_quota, eia_weekly, events, feedback, alert_log, email_log)
|
v
app.py (Streamlit, separate process, read-only against the store)
  price tiles, sentiment gauge and trend, morning brief with status, verified feed,
  unverified low-confidence panel, source health panel, pipeline cost panel
```

## Prerequisites

### Software

| Tool | Version | Purpose |
|---|---|---|
| Python | 3.10+ | Runtime |
| pip | recent | Package management |
| Git | any | Version control |
| Claude Code | latest | Recommended for building and extending the project |
| SQLite | bundled with Python | Shared store between the scheduler and the dashboard |

Optional for production: Docker or systemd (to keep the scheduler running), PostgreSQL with pgvector (to replace
SQLite and Chroma at higher scale), Apache Airflow (if job complexity outgrows APScheduler).

### Accounts, API keys and tokens

All secrets are read from `.env`. They are never hardcoded and `.env` is gitignored.

| Variable | Service | Required | Used for |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API | Yes | Tagging, brief generation, claim evaluation |
| `VOYAGE_API_KEY` | Voyage AI | Yes | Embeddings for the RAG vector store |
| `EIA_API_KEY` | EIA Open Data (free) | Yes | Brent/WTI spot prices, weekly crude and distillate stocks, gas storage, release schedules |
| `SERPER_API_KEY` | Serper | Yes | Claim corroboration (news search) |
| `APIFY_TOKEN` | Apify | Yes | Running `topsail/compliant-commodity-intel`; also used to read run usage for cost tracking |
| `SLACK_WEBHOOK_URL` | Slack incoming webhook | Optional | High-impact news alerts and system alerts; system alerts fall back to logs and console if unset |
| `TWELVE_DATA_API_KEY` | Twelve Data | Optional | Market price quotes (free Basic plan: 8 credits/min, 800/day) |
| `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD` | Your mail provider | Optional | The brief email (see "Delivery: alerts and email") |

GDELT needs no key.

The Anthropic key must be scoped to a workspace (create it with Workspace set to Default). A key scoped to the
whole organization is rejected with a 400 asking for an `anthropic-workspace-id` header; if you must use one, set
`ANTHROPIC_WORKSPACE_ID` below.

### Other settings

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_WORKSPACE_ID` | unset | Sent as the `anthropic-workspace-id` header; only needed for organization-scoped keys |
| `DESK_TIMEZONE` | `Asia/Dubai` | Timezone for the scheduler's schedule (the daily brief time) |
| `COLLECTION_INTERVAL_MINUTES` | `20` | Minutes between collection cycles. Also scales the "scheduler stale" threshold (twice this plus 5, at least 45) |
| `BRIEF_HOUR`, `BRIEF_MINUTE` | `5`, `30` | Daily brief time in `DESK_TIMEZONE` |
| `DATA_DIR` | project folder | Where the database, vector store and evaluation log live. Docker sets it to `/data` |
| `ANTHROPIC_MODEL` | `claude-sonnet-5-5` | Model used for all Claude calls; prices are looked up by this id |
| `CATCHUP_AFTER_HOUR`, `CATCHUP_AFTER_MINUTE` | brief time + 30 min (`6`, `0`) | Desk time from which a missed brief is generated |
| `TWELVE_DATA_API_KEY` | unset | Twelve Data quotes (optional; see "Market prices") |
| `ENABLE_YAHOO_FALLBACK` | `false` | Enables the unofficial Yahoo Finance endpoint |
| `PRICE_PROVIDERS` | `twelvedata,yahoo,eia` | Order in which price providers are tried |
| `PRICE_POLL_INTERVAL_MIN`, `PRICE_POLL_OFFHOURS_INTERVAL_MIN` | `5`, `60` | Poll interval inside / outside an instrument's trading window (lengthened automatically to fit quotas) |
| `PRICE_TICK_RETENTION_DAYS`, `PRICE_STALE_MINUTES` | `30`, `30` | Days of ticks kept before they are rolled into `price_daily`; age after which an open-market tile is marked stale |
| `TWELVE_DATA_MAX_PER_MIN` / `_PER_DAY`, `YAHOO_MAX_PER_MIN` / `_PER_DAY`, `EIA_MAX_PER_MIN` / `_PER_DAY` | `8`/`800`, `20`/`2000`, `10`/`500` | Provider limits in credits; calls stop at 90% of the daily figure |
| `EIA_WEEKLY_REFRESH_HOURS` | `6` | How often the weekly EIA series are refetched |
| `STALE_COLLECTION_AMBER_MIN` / `_RED_MIN` | 2x / 4x the collection interval | Dashboard header: age of the last collection that turns amber / red |
| `STALE_PRICE_AMBER_MIN` / `_RED_MIN` | 3x / 12x the price interval | Same for the last price fetch (judged only while a market is open) |
| `STALE_BRIEF_RED_AFTER_MIN` | `180` | Minutes after the catch-up time with no brief before the header turns red (amber from the catch-up time) |
| `CLUSTER_SIMILARITY_THRESHOLD` | `0.45` | Headline similarity (TF-IDF cosine) at which an article joins an existing story. Tuned on 2,371 real titles |
| `CORROBORATION_MIN_SIMILARITY` | `0.40` | Title similarity a Serper result needs to count as corroboration. Tuned with 8 real Serper calls |
| `BACKTEST_MIN_OBSERVATIONS` | `30` | Paired observations needed before the backtest shows statistics |
| `ALERT_MIN_MAGNITUDE`, `ALERT_MAX_PER_HOUR` | `4`, `5` | Slack alert threshold (impact 1-5) and hourly cap |
| `EMAIL_ENABLED` | `false` | Master switch for the brief email |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` | unset, `587` | SMTP server. Port 465 uses SSL; any other port must offer STARTTLS |
| `EMAIL_FROM`, `EMAIL_TO`, `EMAIL_OWNER` | unset | Sender; comma-separated recipients of the brief; owner of the "needs review" notice (default: first `EMAIL_TO`) |
| `EMAIL_OUTBOX_DIR` | `outbox/` in the data folder | Where `--dry-run` writes `.eml` files |
| `DATABASE_URL` | unset | PostgreSQL URL (`postgresql://user:password@host:5432/db`). Unset = the SQLite file. Never logged |
| `DASHBOARD_PASSWORD` | unset | If set, the dashboard shows only a login form until this password is entered |
| `DASHBOARD_LOGIN_MAX_ATTEMPTS`, `DASHBOARD_LOGIN_LOCKOUT_MIN` | `5`, `15` | Failed logins before a lockout, and the lockout length (also the window failures are counted in) |
| `PG_TEST_URL` | unset | Tests only: a PostgreSQL server for the PostgreSQL leg of the test suite |
| `DAILY_COST_BUDGET_USD` | `10` | Daily total above which a budget alert fires |
| `SONNET_INPUT_USD_PER_MTOK`, `SONNET_OUTPUT_USD_PER_MTOK` | `2.00`, `10.00` | Price overrides for the default model |
| `ANTHROPIC_PRICE_MULTIPLIER` | `1.0` | For example 1.1 if you use US-only inference pricing |
| `SERPER_USD_PER_CREDIT` | `0.001` | Entry-pack rate; lower in larger packs |
| `APIFY_FALLBACK_USD_PER_RESULT`, `APIFY_FALLBACK_USD_PER_START` | `0.0005`, `0` | Used only when Apify does not report a run's usage |

### .env.example and .gitignore

`.env.example` in the repository lists every setting with its default and a comment, grouped by feature (core keys,
scheduler, prices, delivery, trust and quality, PostgreSQL and password). Copy it to `.env` and fill in what you use;
values are unquoted. `.gitignore` keeps `.env`, `venv/`, caches, the databases, `oil_gas_archive/`,
`eval_history.jsonl`, `reports/` and `outbox/` out of git.

## Installation

Two ways to run it: with **Docker** (below, no Python setup) or directly with **Python** (the rest of this
section).

### Option A: Docker

Requires Docker Desktop (Windows with the WSL2 backend, or macOS) or Docker Engine with the Compose plugin
(Linux). You also need your own keys for every service listed under "Accounts, API keys and tokens".

```bash
git clone https://github.com/SergeGit74/Oil-Gas-Dashboard.git
cd Oil-Gas-Dashboard

cp .env.example .env              # then fill in your keys; leave values unquoted
docker compose up -d --build      # first build downloads dependencies (the image is large, about 1 GB)
```

Open **http://localhost:8501**. Common commands:

| Task | Command |
|---|---|
| Follow scheduler logs | `docker compose logs -f scheduler` |
| Follow dashboard logs | `docker compose logs -f dashboard` |
| Status and health | `docker compose ps` |
| Stop (keeps data) | `docker compose down` |
| Rebuild after changing code | `docker compose up -d --build` |
| Run a job once | `docker compose run --rm scheduler python -m jobs.run_evaluation` |
| Delete all stored data | `docker compose down -v` (removes the volume: database, vector store, evaluation log) |

How it is set up:

- **Three services, one image.** `scheduler` runs the collection and brief jobs, `dashboard` serves the page, and
  `init` is a one-shot that creates the database tables before the other two start.
- **One named volume** (`oilgas_data`, mounted at `/data`) holds the database, vector store and evaluation log, so
  data survives rebuilds. Use a named volume, not a bind mount to a Windows folder: SQLite's WAL mode is unreliable
  on bind mounts under Docker Desktop. To back it up, copy it out with a throwaway container, for example
  `docker run --rm -v oil-gas-dashboard_oilgas_data:/data -v "$PWD":/backup alpine tar czf /backup/data.tgz -C /data .`
  (the volume name is prefixed with the compose project name; check `docker volume ls`).
- **Secrets stay out of the image.** `.env` is excluded by `.dockerignore` and passed to the containers at runtime.
- **The dashboard is published to this PC only** (`127.0.0.1:8501`) because it has no login. Do not expose it
  to a network you do not trust.
- **Every start runs one collection cycle**, which costs a few cents of API use, so avoid restart loops.
- **Healthchecks:** the dashboard must answer on its health endpoint; the scheduler must have finished a
  collection cycle within the staleness window (45 minutes by default). `docker compose ps` shows the result.
- The compose file and image have not been built or run in the development environment (Docker was not available
  there). The first build on a new machine is the real test; please report problems.

Do not run the Docker scheduler and a Python-installed scheduler against the same data at the same time: each
would run every cycle and double the cost. They use different storage (the volume versus the project folder), so
they would not even share data.

### Option B: Python

```bash
git clone https://github.com/SergeGit74/Oil-Gas-Dashboard.git
cd Oil-Gas-Dashboard

python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

pip install -r requirements.txt

cp .env.example .env              # then fill in your keys

python -c "import storage"        # creates dashboard_store.db and its tables (and migrates older ones)
```

### requirements.txt

```text
streamlit>=1.38
anthropic>=0.40
voyageai>=0.3
chromadb>=0.5
python-dotenv>=1.0
requests>=2.32
pandas>=2.2
beautifulsoup4>=4.12
apscheduler>=3.10
exchange_calendars>=4.5
pyyaml>=6.0
psycopg[binary]>=3.1
```

`constraints.txt` pins the exact tested versions (the Docker image and CI install with it); `requirements-dev.txt` adds
`pytest` and `ruff`.

## Running the system

Two long-running processes, each in its own terminal (or as managed services in production):

```bash
# Process 1: scheduler (collection, ingestion, brief, evaluation, health and cost checks)
python -m jobs.scheduler

# Process 2: dashboard (reads from the shared store)
streamlit run app.py
```

The dashboard has no login by default (see "Dashboard password"), and "ask the desk" spends your API credit, so it
must not be reachable by other people. `.streamlit/config.toml` makes `streamlit run app.py` listen on `127.0.0.1` (this PC only); the Docker
setup publishes the port to `127.0.0.1` too. Do not change either to `0.0.0.0` or forward the port unless you add
authentication in front of it.

| Job | Trigger | What it does |
|---|---|---|
| Collection cycle | Every 20 minutes | Collect, skip known articles, tag new ones, corroborate, compute sentiment and price confirmation, ingest verified articles, check source health |
| Price poll | Every 5 minutes (`PRICE_POLL_INTERVAL_MIN`) | Fetch quotes for the indicators that are due (hourly while their market is closed), within provider quotas; also runs at startup |
| Price retention | 03:10 | Roll old ticks into daily closes and purge ticks older than 30 days |
| Event load | 04:00 and at startup | Refresh `events` from `config/events.yaml` and EIA's release schedules |
| Daily brief cycle | 05:30 in the desk timezone | Generate and evaluate the brief, check evaluation trend, source health, sentiment/price divergence, then check yesterday's cost against the budget |

**Missed-brief catch-up.** `catch_up_brief` (in `jobs/scheduler.py`) runs at scheduler startup and after every
collection cycle. Once the local time in `DESK_TIMEZONE` is past `CATCHUP_AFTER_HOUR:CATCHUP_AFTER_MINUTE` (06:00 by
default, 30 minutes after the brief time) and the `briefs` table has no row for today's date (the UTC date
`run_evaluation` saves under), it generates the brief. Any row counts as "the morning run happened", including one
flagged for review, so a flagged brief is never regenerated. Both this and the scheduled 05:30 run first take a
claim with `storage.claim_daily_run("daily_brief", day)`: at most 2 attempts a day, at least 30 minutes apart,
and never two at once, so a failing run cannot loop and spend money. With no verified articles there is nothing to
brief and no attempt is spent. The dashboard never starts the pipeline; it shows "Brief pending" until a brief
exists. The catch-up does not rerun the source-health, divergence or budget checks.

The scheduler's timezone, interval and brief time come from `DESK_TIMEZONE`, `COLLECTION_INTERVAL_MINUTES`,
`BRIEF_HOUR` and `BRIEF_MINUTE` in `.env` (defaults: `Asia/Dubai`, 20, 05:30). The scheduler runs one
collection cycle immediately at startup, then every 20 minutes. A failure in one step is logged and does not stop the rest of
the cycle. Each cycle runs inside `cost_run(...)` so it produces one cost record. Standalone job runs
(`python -m jobs.run_collection`, `python -m jobs.run_evaluation`, `python -m jobs.run_ingestion`) wrap themselves
the same way, and a nested `cost_run` joins the outer one.

Code changes only take effect after the scheduler is restarted. If you start either process from a tool with a
background time limit, it stops when the limit is reached; run both in your own terminals (or as services) for
unattended use.

### Dashboard refresh modes

The dashboard only reads the store; it never collects data. New data appears only while the scheduler is running,
and an open browser tab does not poll on its own. The sidebar **Refresh** control chooses when the open page
re-reads the store:

| Mode | Behavior |
|---|---|
| **With scheduler** (default) | Checks the store every 30 seconds and reloads only when a new run has finished. Cheapest way to stay current |
| **Auto-refresh** | Reloads on a timer you pick: 1, 5 (default), 10 or 30 minutes, whether or not there is new data |
| **Every 20 min** | Reloads every 20 minutes, matching the scheduler's cycle length |
| **Manual** | No automatic reload; use **Refresh now** or reload the browser page |

- The sidebar also shows when the page loaded and when the last collection cycle finished, and warns if no cycle
  has finished for 45 minutes (usually a sign the scheduler is not running).
- With the scheduler stopped, every mode just re-reads the same stored data. Run both processes for live updates.
- Changing the mode, using **Refresh now**, or typing in "ask the desk" restarts the timer. The selected mode is
  kept for the browser session but resets to the default if you reload the browser page.
- Intervals and the staleness threshold are constants at the top of `app.py`. Timed reloads need Streamlit 1.37
  or later (`st.fragment(run_every=...)`).

## Project structure

```
oil_gas_dashboard/
├── .env                      # real keys (gitignored)
├── .env.example
├── .gitignore
├── requirements.txt          # lower-bound dependencies
├── constraints.txt           # exact tested versions, used by the Docker build
├── Dockerfile                # one image for the dashboard and the scheduler
├── docker-compose.yml        # init, scheduler and dashboard services plus the data volume
├── .dockerignore             # keeps .env and local data out of the image
├── healthcheck.py            # container healthchecks (dashboard, scheduler)
├── README.md
├── USER_GUIDE.md             # short guide: running the scheduler and dashboard, reading the panels
├── LICENSE                   # personal-use license
├── .streamlit/config.toml    # dashboard listens on 127.0.0.1 only; usage stats off
├── config.py                 # .env loading, required-key checks, ANTHROPIC_MODEL
├── pricing.py                # unit prices for cost estimates (env-overridable)
├── cost_tracker.py           # per-run cost accounting: Claude, Serper, Apify, counters
├── tracked_llm.py            # single entry point for Claude calls (records token usage)
├── db.py                     # SQLite / PostgreSQL connection wrapper (placeholders, upserts, DDL translation)
├── migrations.py             # versioned schema migrations, recorded in schema_migrations
├── auth.py                   # optional dashboard password (DASHBOARD_PASSWORD)
├── ruff.toml, pytest.ini     # lint and test configuration; .github/workflows/ci.yml runs both
├── storage.py                # SQLite store and accessors
├── alerts.py                 # send_alert: log, console, and Slack if configured
├── data_sources.py           # EIA, GDELT (GKG files), Apify (async run, retry, cost), Serper (credits), normalize_url
├── llm_pipeline.py           # source trust registry, news/commentary split, injection flags, tagging, brief generation
├── sentiment_analysis.py     # weighted Bull/Bear/Neutral aggregation, current_sentiment, divergence check
├── price_confirmation.py     # sentiment vs Brent price move: Confirmed/Partial/Divergent
├── high_impact_alerts.py     # Slack alert for each verified high-impact story (dedupe, hourly cap, escaping)
├── brief_email.py            # builds the brief email / review notice, sends over TLS, writes .eml previews
├── story_cluster.py          # local TF-IDF headline similarity and story clustering (no API cost)
├── key_points.py             # per-brief key points and the deterministic day-over-day diff
├── backtest.py               # descriptive sentiment-vs-Brent-return backtest (no look-ahead)
├── feedback_tools.py         # golden-set export and tagger-agreement report
├── pages/1_Research.py       # dashboard page: the backtest (read-only)
├── market_calendar.py        # open / closed / holiday per indicator (exchange_calendars), market-status line
├── event_calendar.py         # events table: manual YAML + EIA release schedules; Watch-today text
├── freshness.py              # dashboard header: collection / price / brief age with amber and red levels
├── market_data.py            # price providers (Twelve Data, Yahoo, EIA), quota manager, trading windows, polling plan
├── config/market_symbols.json # the 8 indicators: label, unit, currency, calendar code, fallback window, per-provider symbol
├── config/events.yaml        # manual scheduled events (FOMC dates; add OPEC+ meetings by hand)
├── scripts/backtest_sentiment.py, export_golden_set.py, tagger_agreement.py  # CLIs for the three tools above
├── scripts/copy_sqlite_to_postgres.py  # one-off copy of an existing SQLite database into PostgreSQL
├── scripts/check_symbols.py  # symbol discovery -> reports/symbol_check.json (generated, gitignored)
├── tests/                    # pytest suite (mocked HTTP, throwaway database, network blocked)
├── pytest.ini, requirements-dev.txt
├── rag_pipeline.py           # chunking, embeddings, recency-weighted retrieval, RAG answers
├── brief_evaluation.py       # claim decomposition, entailment, numeric and coverage checks
├── app.py                    # Streamlit dashboard (read-only against storage.py)
├── jobs/
│   ├── __init__.py
│   ├── scheduler.py          # APScheduler orchestrator; wraps each cycle in cost_run
│   ├── run_collection.py     # fetch, normalize, skip known, tag new, corroborate, sentiment, price check
│   ├── run_ingestion.py      # embed verified articles into the vector store
│   ├── run_prices.py         # price poll (fetch_prices) and the tick retention job
│   ├── send_brief_email.py   # email today's brief once (also `--dry-run`)
│   ├── load_events.py        # load_events: fill the events table
│   ├── run_evaluation.py     # generate and evaluate the morning brief
│   ├── check_eval_trend.py   # groundedness drift monitoring
│   ├── source_health.py      # generic consecutive-degradation monitor (Apify, Serper, EIA)
│   └── check_cost_budget.py  # yesterday's total cost vs DAILY_COST_BUDGET_USD
├── oil_gas_archive/          # Chroma vector store (generated, gitignored)
├── dashboard_store.db        # SQLite store (generated, gitignored)
└── eval_history.jsonl        # evaluation log (generated, gitignored)
```

## Data model

| Table | Contents |
|---|---|
| `articles` | Tagged articles: category, direction, magnitude, confidence, reason, content type. `collected_at` is the time the article was first seen. `cluster_id` groups near-duplicate headlines (the id of the story's first article); `corroboration_json` lists the trusted-source results that matched |
| `briefs` | One brief per day with status (`published` or `flagged_for_review`), groundedness score, evaluation JSON, `key_points_json` (top stories, sentiment, prices) used for the next day's comparison, and `sources_json` (the numbered source list the brief cites) |
| `sentiment_history` | One row per day: label, score, counts, price benchmark, price change, confirmation status |
| `job_runs` | Status log for every job and source (`success`, `degraded`, `skipped`, `failed`, `alert`, `ok`) |
| `run_claims` | `(job, day)` -> attempts and last attempt; lets a daily job run at most N times a day, spaced by a cooldown |
| `price_ticks` | One row per fetched quote: indicator, quote time `ts`, price, previous close, change %, provider, `delayed_minutes`, `fetched_at`. Kept 30 days |
| `price_daily` | Daily open/high/low/close per indicator, rolled up from ticks before they are purged |
| `eia_weekly` | Weekly EIA series (crude stocks, distillate stocks, Lower 48 gas storage): series, period, value, unit |
| `events` | Scheduled events: date, time, tz, title, category, source_url, source. Unique on (date, time, title) |
| `schema_migrations` | Applied schema versions (`version`, `name`, `applied_at`); see "Optional PostgreSQL" |
| `alert_log` | One row per story ever considered for a Slack alert (cluster key unique): article, status `sent`, `failed`, `suppressed_rate_limit` or `not_configured` |
| `email_log` | `(day, kind)` -> sent time and recipient count; the "already mailed" marker (`brief` or `review_notice`) |
| `feedback` | Analyst feedback: item_type (`article_tag` or `brief_section`), item_id, rating (`up`/`down`), comment, analyst_label (corrected direction), created_at |
| `api_quota` | Credits used per provider per UTC day |
| `run_costs` | One row per scheduled run: Claude, Apify and Serper cost, token and credit totals, and `details_json` with the per-step breakdown, counters and warnings |

## Skipping already-stored articles

Each cycle fetches from Apify and GDELT, normalizes URLs (lowercase host, no fragment, tracking parameters and
trailing slashes removed), dedupes, and drops anything already in `articles`. Only new articles are tagged and
corroborated, so Claude and Serper spend scales with new articles, not with how many the feeds return.

- Articles the model does not tag are not saved and are retried next cycle.
- Sentiment and the price check run every cycle from stored articles (first seen in the last 24 hours), so the
  score does not depend on how many articles happened to be new.
- Rows stored before direction tagging existed have no direction and are ignored by the sentiment score.
- Apify is billed per run and per result whether or not the articles are new. Cycle frequency and the number of
  sources are its cost levers.
- Per-run counters (`articles_fetched`, `articles_new`, `articles_skipped_known`) are stored in `details_json`.

## Cost tracking

A "run" is one scheduler cycle. `cost_run(job_name)` collects everything recorded inside the block and saves one
`run_costs` row, even if a job raises.

| Service | How it is counted |
|---|---|
| Claude | All calls go through `tracked_llm.create_message(step, ...)`, which reads the token counts returned by the API and prices them by model id in `pricing.py`. A model with no price entry is tracked as tokens and flagged as unpriced, not guessed |
| Serper | All calls go through `data_sources.serper_news`, which records credits (the reported value if present; otherwise 1 for up to 10 results, 2 above). Keep `num` at 10 or below |
| Apify | The actor is started asynchronously so the run id is known. After the run ends, the cost is read from the run's reported usage. If that is missing, an estimate from result count is used and flagged. Failed, aborted and retried runs are costed too |

Step labels: `tagging`, `brief_generation`, `eval_claim_decomposition`, `eval_entailment`, `rag_answer`,
`serper_corroboration`, `apify_news`.

- **Observed cost (short test, will vary).** A cycle with no new articles cost about $0.0002 (the Apify run only,
  roughly $0.015 per day at 72 cycles). A cycle that tagged about 40 new articles cost about $0.04, and a brief with
  its evaluation about $0.07. "Ask the desk" costs about $0.006 per question. Real daily cost depends on how many
  new articles arrive, your Apify plan and current vendor prices, so watch the dashboard's cost panel for the first
  few days.
- **There is no hard spending cap.** `DAILY_COST_BUDGET_USD` only sends an alert once a day has already exceeded
  it. Set spending limits with your API providers if you need a ceiling.
- **Prices** are defaults in `pricing.py` and will drift. Verify them against Anthropic's, Serper's and Apify's
  current pricing and override through `.env`.
- **Dashboard:** a cost panel shows the latest day's total and per-service split, a 14-day stacked chart, and recent runs.
- **Budget alert:** `jobs/check_cost_budget.py` runs once a day and posts to Slack (if configured) when
  yesterday's total exceeded `DAILY_COST_BUDGET_USD`.
- **Days** in the cost views and budget check are UTC calendar days, while the scheduler uses the desk timezone.

## Source trust, confidence and verification

`llm_pipeline.TRUST_REGISTRY` assigns each outlet a tier by exact domain (a leading `www.` is ignored):

| Tier | Base confidence | Contents |
|---|---|---|
| `official` | 0.95 | Agencies, regulators, central banks, multilateral bodies (EIA, IEA, OPEC, Treasury/OFAC, Fed, ECB, IMF, ...) |
| `wire_trade` | 0.80 | Wires, major newspapers, established energy trade press (Reuters, Bloomberg, Platts, Argus, Rigzone, ...) |
| `aggregator` | 0.60 | General, regional or state-linked outlets and aggregators (OilPrice, Mint, Al Jazeera, Arab News, ...) |
| `unknown` | 0.35 | Anything not listed |

- **Confidence** = tier base, times 0.7 for commentary, plus 0.10 per corroborating trusted domain (at most two),
  capped at 1.0. Corroboration counts distinct `official` and `wire_trade` domains in Serper news results.
- **Verified** means confidence of at least 0.5 (`VERIFIED_MIN_CONFIDENCE` in `config.py`) and not
  injection-flagged. Only verified articles reach sentiment, the vector store and the brief. Unknown-tier articles
  are verified only if corroborated. Everything else appears in the dashboard's unverified panel.
- `wire_trade` doubles as the corroboration set, so keep it to strong names. Add outlets by editing the registry;
  existing stored articles keep their old tier until you re-tier them.

## Brief generation and evaluation

- The brief is written from verified, non-commentary articles (up to 40, highest impact first), numbered 1 to N
  and cited as `[n]`, plus the sentiment reading, EIA figures and archive headlines from the vector store.
- The title and date line are added by the code from the run date (UTC), not written by the model. The model is
  told to give each EIA figure with its own date, because EIA data lags the calendar by days.
- `brief_evaluation.evaluate_brief` runs: claim decomposition, entailment of each claim against the numbered
  sources and a trusted reference block (EIA figures, sentiment reading), a numeric-fidelity check (every number
  must appear in a source or the reference data), and a coverage check (magnitude 4-5 stories must be cited).
- A brief is published only if groundedness is at least 0.90, no number is unmatched, and at least 70% of
  high-impact stories are cited. Otherwise it is saved as `flagged_for_review` and the dashboard shows the
  reasons instead of the text. Each evaluation is appended to `eval_history.jsonl`.
- Claims the checker does not rule on count as unsupported. The checker is an LLM, so its verdicts need periodic
  human spot-checks.

## Sentiment and price confirmation

- **Direction** comes from the same Claude tagging call as impact, calibrated for oil and gas price direction,
  not general market mood. `Mixed` counts as zero in the score but is shown in the breakdown.
- **Score** = average of direction (+1 / -1 / 0) x (magnitude / 5) x confidence weight, over verified articles.
  Labels: Bull at +0.15 or above, Bear at -0.15 or below, otherwise Neutral.
- **Price confirmation** compares the label with the Brent move. Moves under 0.5% count as Neutral.
  `Divergent` means news and price disagree, `Partial` means one side is Neutral, `Unavailable` means no price data.
- **Divergence alert** fires after 3 consecutive divergent days. It prompts analyst review and never adjusts the
  score or the brief automatically.

## Source health monitoring

`jobs/source_health.py` holds one registry, `SOURCE_HEALTH_CONFIG`, with a threshold per source:

| Source | Job name logged | Alert threshold |
|---|---|---|
| Apify trade-press feed | `fetch_apify_commodity_news` | 3 consecutive degraded cycles |
| Serper corroboration | `serper_corroboration` | 3 consecutive degraded cycles |
| EIA price and inventory data | `fetch_eia_data` | 2 consecutive degraded cycles |

`skipped` runs neither break nor extend a streak. To monitor a new source, log its status to `job_runs` at the
call site and add one entry to the registry.

## GDELT

GDELT's DOC API answered HTTP 429 on every request from the development machine, even with long gaps, so the
code reads GDELT's static 15-minute GKG files instead (`data_sources.fetch_gdelt`). No key, no rate limit. Each
cycle downloads the last two files (a few MB zipped each), keeps rows that have an energy theme and an oil or gas
keyword in the page title, and passes them on as titles only. Tune `GDELT_THEMES`, `GDELT_KEYWORDS` and
`GDELT_FILES` at the top of that section. Most GDELT outlets are not in the trust registry, so many of these
stories stay unverified unless the outlet is added or the story is corroborated.

## Apify actor

The project uses `topsail/compliant-commodity-intel` (Commodity & Energy News Scraper, robots-compliant, no PII).
Its input is `sources`, a list of `{"url": ..., "commodity": ...}` objects, plus `maxItemsPerSource`; plain strings
make the actor crash. The configured sources (`data_sources.APIFY_SOURCE_CONFIG`) are the OilPrice, World Oil and
Rigzone news pages. Retry settings (3 attempts, 5s exponential backoff, retry on 429 and 5xx, fail fast on other
errors) are constants at the top of `data_sources.py`; a run that exceeds its time limit is aborted so it cannot
keep accruing usage. The actor has a small public user base: confirm in the Apify console that its input and
output fields still match `APIFY_SOURCE_CONFIG` and `_normalize_apify_items` (the URL and title mapping works;
snippets are short or missing for some sources).

## Design principles

- **Trust boundaries at every layer**: unverified or low-confidence content never enters an LLM prompt, the vector
  store, or the published brief. It appears only in a separate "unverified" panel.
- **Scraped content is data, never instructions**: instruction-like text is flagged and excluded, and prompts wrap
  content in delimiters with explicit instructions not to obey it.
- **Structured output**: all tagging uses Claude tool use with schemas, not free-text parsing.
- **Evaluate before publishing**: a failing brief goes to analyst review instead of the dashboard.
- **Heavy work stays out of the dashboard**: collection, tagging, ingestion, and evaluation run as scheduled jobs.
- **Spend only on new information**: known articles are not re-tagged or re-corroborated.
- **Degrade visibly, not silently**: source failures and unpriced models are logged and surfaced.
- **No secrets in code**: credentials load from `.env` through `config.py`.

## Market prices

`jobs/run_prices.py` polls 8 indicators (Brent, WTI, Henry Hub, TTF, gold, DXY, S&P 500, ULSD) as its own scheduler
job, separate from the news cycle, and the dashboard shows them as tiles with a 24-hour chart, the quote time, a
"delayed N min" label when the provider reports one, and a stale marker. The tiles read only `price_ticks` and
refresh every 60 seconds; the dashboard never calls a price provider.

- **Providers**, tried in `PRICE_PROVIDERS` order, each indicator served by the first that returns a quote:
  Twelve Data (needs `TWELVE_DATA_API_KEY`), Yahoo Finance (**unofficial** endpoint, off unless
  `ENABLE_YAHOO_FALLBACK=true`; no published terms or limits, may change or stop, review its terms first), and EIA
  daily spot for Brent and WTI (updates once a day and lags the calendar, so it is a last resort).
- **Symbols** live in `config/market_symbols.json`. Run `python scripts/check_symbols.py` to test one call per
  indicator and provider; it writes `reports/symbol_check.json` and never edits the symbol file. Keyed providers
  without a key are skipped. This spends provider credits.
- **Quota.** Credits are counted per symbol (Twelve Data documents one credit per symbol, including in a
  multi-symbol request) in `api_quota`. A call is refused once it would pass 90% of the daily limit, requests are
  split and paced to the per-minute limit, and the polling interval is lengthened automatically so the daily
  quota cannot run out early (8 symbols on one 800/day provider means at least 16 minutes).
- **Rate.** Every `PRICE_POLL_INTERVAL_MIN` (5) minutes while an instrument's market is open, every
  `PRICE_POLL_OFFHOURS_INTERVAL_MIN` (60) while it is closed or on holiday (see "Market context" for how
  that is decided).
- **Health.** Each poll logs `fetch_prices` to `job_runs` (`success`, `degraded` if any due indicator got no
  quote, `skipped` if nothing was due), is registered in `SOURCE_HEALTH_CONFIG` (threshold 3), and records
  per-provider call counters in the `price_poll` run. These runs cost nothing and are hidden from the dashboard's
  run list and refresh marker.
- **Retention.** A daily job at 03:10 rolls every day still held as ticks into `price_daily`, then deletes ticks
  older than `PRICE_TICK_RETENTION_DAYS` (whole days only). It is safe to run repeatedly.
- **Without keys.** The dashboard and the scheduler start with no paid keys. Steps that need a missing key degrade
  or fail visibly in `job_runs`; with no price provider enabled, `fetch_prices` logs `degraded` with the reason.

## Market context

- **Market calendar.** `market_calendar.market_status(indicator, now)` returns `open`, `closed` (weekend or outside
  hours) or `holiday` (a weekday with no session), plus the last close and next open. It uses
  [`exchange_calendars`](https://pypi.org/project/exchange-calendars/): one library with minute-level sessions,
  holidays and early closes for CME Globex (`CMES`, used for WTI, Henry Hub, gold, ULSD), ICE (`IEPA`, used for Brent
  and DXY), Euronext Amsterdam (`XAMS`, used for TTF) and NYSE (`XNYS`, S&P 500). The other candidate,
  `pandas_market_calendars`, has per-product CME calendars but no ICE Europe either, and would add a second
  dependency. **Brent (`IEPA` is ICE US) and TTF (`XAMS` is Euronext, not ICE Endex) use proxy calendars**, because
  the library has no calendar for ICE Futures Europe or ICE Endex; their holiday and hours labels are indicative.
  If a calendar is unavailable the code falls back to the approximate weekly window in `market_symbols.json`.
  Tiles for a closed market show "Closed, last close <date>" and the last session's change instead of a move, and the
  poller uses the slow rate on weekends and holidays. `market_calendar.status_line()` builds the market-status line
  that goes into the brief prompt and its trusted reference text.
- **Freshness header.** The top of the dashboard shows the age of the last collection (newest successful Apify or
  GDELT fetch in `job_runs`), the last price fetch (`price_ticks`, judged only while some market is open) and when
  today's brief was generated (`briefs`). Each turns amber then red past the `STALE_*` thresholds.
- **Spread and ratio.** The Brent-WTI spread and the gold/Brent ratio are computed from the latest ticks at read time.
  They show "n/a" if the two quotes are more than 24 hours apart, and the spread also needs both from the same
  provider. There is no TTF-Henry Hub spread: it needs currency and unit conversion, which is not implemented.
- **EIA weekly data.** Crude stocks (`WCESTUS1`), distillate stocks (`WDISTUS1`, route `petroleum/stoc/wstk`) and Lower 48
  gas storage (`NW2_EPG0_SWO_R48_BCF`, route `natural-gas/stor/wkly`) are fetched into `eia_weekly` by the collection
  cycle at most every `EIA_WEEKLY_REFRESH_HOURS`, shown on the dashboard with the week-on-week change, and added, with
  their own dates, to the brief's EIA context. The two new series ids were found with EIA's live route browser.
- **Event calendar.** `jobs/load_events.py` (daily 04:00 and at scheduler start) fills the `events` table from
  `config/events.yaml` (manual; new rows only, never deleted) and from EIA's published schedule pages: the Weekly
  Petroleum Status Report and Weekly Natural Gas Storage Report standard day and time are read from each page's own
  sentence, and its holiday exceptions move individual releases. If a page layout changes, the loader raises,
  logs `load_events` as `degraded`, and keeps the existing rows. The dashboard lists the next 7 days.
- **Watch today.** The brief's "Watch today" section may list only the events in the table for today and the next 3
  days; the prompt says so, gives "none" when the table has none, and the same text is in the evaluator's trusted
  reference data. Nothing deterministic enforces it after generation, so the evaluator is the check.

## Trust and quality

- **Story clustering.** `story_cluster.py` groups near-duplicate headlines with a local TF-IDF cosine similarity
  (numpy only, no API call, no cost). Each new article joins the cluster of its most similar article from the
  previous 24 hours if the similarity reaches `CLUSTER_SIMILARITY_THRESHOLD` (0.45, chosen by reading pairs from
  your stored titles: at 0.40-0.45 the pairs were still the same event; below 0.40 related but different stories
  start to mix). Clusters are never merged afterwards, and the id is the earliest member's, so it is stable.
  Embeddings were not needed. On the 2,371 stored articles this gave 1,249 stories (the largest, one event, had
  215 outlets). Sentiment counts one article per story (highest confidence, then highest magnitude), so its `n` is
  the number of stories. The verified feed shows one row per story with "reported by N outlets". The brief's own
  source list is **not** deduplicated yet.
- **Corroboration.** A Serper result counts only if its site is in a trusted tier, it is not the article's own
  outlet, and its **title** is similar enough to the article's headline (`CORROBORATION_MIN_SIMILARITY`, 0.40).
  Title and snippet together matched unrelated pages too often in the calibration sample, so only titles are
  compared. Several results from one outlet count once. The matches (site, url, title, similarity) are saved in
  `articles.corroboration_json`. Trusted domains now match by whole-label suffix, so `uk.reuters.com` matches
  `reuters.com` but `notreuters.com` does not; this applies to every tier lookup, not just corroboration, and
  stored articles are not re-tiered.
- **Backtest.** `backtest.py`, `scripts/backtest_sentiment.py` and the dashboard's **Research** page (a Streamlit
  page in the sidebar) compare each completed day's final sentiment with the Brent return from that day's
  `price_daily` close to the next and to the third available close. Weekends and holidays are skipped because
  `price_daily` only has days with quotes. No look-ahead: no price from before the sentiment day is used, today's
  row is excluded, and a return is dropped if its close is too far away or from a different provider than the
  entry close. It reports correlation and hit rate by label, and "insufficient data" under
  `BACKTEST_MIN_OBSERVATIONS` (30) paired observations. It is descriptive, not predictive, and says so on the page.
- **What changed since yesterday.** Each brief stores key points (top verified stories, sentiment, latest prices).
  `key_points.py` compares today with yesterday's stored key points in code: new and dropped top stories (matched by
  title similarity), the sentiment change and the price changes. The model only phrases that block in a "What
  changed since yesterday" section and may not add facts; the block is also in the evaluator's trusted reference
  text. If yesterday's brief is missing, or was stored before key points existed, code writes "Yesterday's brief is
  not available..." instead.
- **Analyst feedback.** The dashboard's "Analyst feedback" panel inserts a rating (agree / disagree), an optional
  corrected direction and a comment (max 1,000 characters) for a verified article's tag or a section of the published
  brief into `feedback`. This is the only write the dashboard makes besides "ask the desk". `python
  scripts/export_golden_set.py` writes `reports/golden_set.csv`; `python scripts/tagger_agreement.py` reports how
  often the model's direction matches the analysts (the latest feedback per article wins; a thumbs-down without a
  correction is counted separately).

## Delivery: alerts and email

- **High-impact alerts.** After each collection cycle, `high_impact_alerts.run_alerts` looks at the articles saved in
  that cycle and posts one Slack message per **story** (cluster) that is verified (confidence 0.5 or more, so High
  0.8+ or Medium 0.5-0.8; not injection-flagged) and has impact at or above `ALERT_MIN_MAGNITUDE` (4). The message
  uses the best verified article of the story (title, outlet, direction, impact, confidence level, "reported by N
  outlets" counting verified articles only, link). Titles are scraped text, so Slack markup in them is escaped
  (`<!channel>`, `<@U..>` and links are defused) and length-capped. A story is alerted **at most once ever**
  (`alert_log`, unique per cluster, claimed before sending). `ALERT_MAX_PER_HOUR` (5) caps bursts; the least
  important are recorded as `suppressed_rate_limit` and are **not replayed** later, because they are no longer new.
  A failed post is recorded as `failed` and not retried. With no `SLACK_WEBHOOK_URL`, nothing is sent and the story
  is recorded as `not_configured`. Real attempts log `slack_alerts` to `job_runs` (registered in
  `SOURCE_HEALTH_CONFIG`).
- **Brief email.** Off unless `EMAIL_ENABLED=true`. After every collection cycle and the daily brief, the scheduler
  calls `jobs/send_brief_email.py`, which does nothing unless today's brief exists and has not been mailed.
  A **published** brief goes to everyone in `EMAIL_TO`; a brief **held for review** sends only a short "needs review"
  notice (groundedness and the evaluator's reasons, not the brief text) to the owner. The message is plain text
  plus HTML with the status, generation time, the brief (including its "What changed since yesterday" section), a
  price snapshot (the latest quotes when the brief was generated) and the numbered sources. It never contains cost
  figures or unverified / low-confidence articles. Briefs generated before `sources_json` existed say "Sources were
  not stored".
- **Sending safely.** TLS is required: port 465 uses implicit TLS, any other port must offer STARTTLS or the job
  refuses to log in or send. Timeout 30 s; the password is never logged or put in `job_runs`. Header values have
  control characters replaced, and recipient addresses are validated, so a scraped or odd value cannot add headers or
  recipients. HTML is escaped before a small markdown subset is applied.
- **Once per day.** `claim_daily_run("brief_email", day)` allows at most 3 attempts a day, 30 minutes apart, and stops
  two runs sending at once; `email_log` records a successful send, because the claim alone would allow a second send.
  A failure logs `send_brief_email` as `failed` (registered in `SOURCE_HEALTH_CONFIG`).
- **Preview.** `python -m jobs.send_brief_email --dry-run [--day YYYY-MM-DD]` writes an `.eml` into `outbox/`
  (gitignored). It needs no SMTP settings, works while email is disabled, and neither claims nor marks anything sent.

## Optional PostgreSQL

SQLite (one file, WAL mode) stays the default and is right for one machine. Set `DATABASE_URL` to use PostgreSQL
instead; nothing else changes. The vector store, evaluation log and outbox stay in `DATA_DIR`.

- **How it works.** All SQL lives in `storage.py`, written once in a dialect both databases accept: `?` placeholders,
  `ON CONFLICT` upserts, portable queries (for example `NULLS LAST`, which the two engines order differently by
  default). `db.py` converts placeholders, returns dict rows and translates the two DDL keywords that differ
  (`AUTOINCREMENT`, `REAL`). Timestamps are stored as ISO text on both, so there are no date-type conversions. The
  driver (`psycopg`) is imported only when `DATABASE_URL` is set. A connection is opened per call, as with SQLite.
- **Versioned migrations.** `migrations.py` holds numbered steps (1 baseline schema, 2 columns added later, 3 an
  index); `init_db()` applies the missing ones in order and records them in `schema_migrations`. Every step is
  idempotent, so a database created before versioning is adopted by re-running them and nothing is dropped or
  rewritten. To change the schema, append a step; never edit an applied one. On PostgreSQL the runner takes an
  advisory lock so the scheduler and the dashboard can start at the same moment.
- **Moving existing data.** Create an empty PostgreSQL database, then:

  ```bash
  python scripts/copy_sqlite_to_postgres.py --pg-url postgresql://user:password@host:5432/oilgas --dry-run
  python scripts/copy_sqlite_to_postgres.py --pg-url postgresql://user:password@host:5432/oilgas
  ```

  The SQLite file is opened read-only and never changed; nothing is deleted anywhere. The target must be empty
  unless you pass `--append` (rows whose key exists are skipped, never overwritten). The whole copy is one
  transaction, so a failure leaves the target exactly as it was, and id sequences are advanced past the copied ids.
  Then set `DATABASE_URL` in `.env` and restart the scheduler and dashboard. Stop writing to the SQLite file first:
  the copy is a snapshot, not a sync.
- **Not built.** Hosting, connection pooling, backups of the PostgreSQL server and `pgvector` (moving the Chroma
  archive into PostgreSQL). They are future work.

## Dashboard password

Setting `DASHBOARD_PASSWORD` makes `app.py` and the Research page show only a login form until it is entered:
nothing is read from the store or rendered before that. The comparison is constant-time (`hmac.compare_digest` on
fixed-length digests); the logged-in flag lives in the browser session; and failed logins are limited by a lockout
that all sessions in the server process share (default: 5 failures lock everyone out for 15 minutes), so reloading or
opening a new tab does not reset it. Each wrong guess is also slowed by a second or more. It is off by default for
local use.

This is one shared password, not user accounts, and it does not encrypt traffic or protect Streamlit's own static
files. **A real deployment needs platform-level authentication** (a reverse proxy or identity-aware gateway with
TLS, or a VPN) in front of the dashboard; keep the localhost binding described above unless you have one.

## Development: tests, lint and CI

```bash
pip install -r requirements-dev.txt
python -m pytest            # unit and integration tests, SQLite and PostgreSQL
ruff check .                # lint (pycodestyle errors and warnings, pyflakes; line length 130)
```

- **No network, no paid calls.** `tests/conftest.py` gives every test a throwaway database, blanks all API keys and
  SMTP settings, and makes any non-loopback socket connection raise, so a test that forgets to mock HTTP fails.
- **Both databases.** The `db` fixture runs a test on SQLite and again on PostgreSQL. The PostgreSQL leg uses the server
  in `PG_TEST_URL` if set; otherwise, if `pgserver` is installed (`pip install pgserver`, an embedded PostgreSQL that
  needs no server or Docker), it starts one for the session; otherwise that leg is skipped. `REQUIRE_PG=1` turns a
  missing PostgreSQL into a failure (CI sets it). Local PostgreSQL tests are slower: the whole suite takes about two
  minutes.
- **CI.** `.github/workflows/ci.yml` runs on every push and pull request: install from `requirements-dev.txt` with
  `constraints.txt`, `ruff check .`, then `pytest` against a PostgreSQL 16 service container. It needs no secrets and
  uses no external network.
- **Lint.** `ruff.toml` selects `E`, `F`, `W` with a 130 character line limit (long literals in `tests/` are exempt). A
  stricter rule set (import sorting, bugbear) would flag a lot of existing code and was not adopted.

## Tests

How to run them is under "Development: tests, lint and CI" above.

The suite uses a throwaway database, blank API keys and blocks non-loopback network connections, so it makes no
real or paid calls. It covers the daily-run claim (concurrent claimers, attempt cap, cooldown), the brief catch-up
(no brief, flagged brief, published brief), the three price providers with mocked responses, the fallback order,
the 90% quota refusal, per-minute pacing, trading windows, tick retention, `market_status` on a weekend, a holiday and a normal day, the spread math, EIA weekly storage and
gating, the EIA schedule parsers, event loading (idempotent; a bad page keeps old rows), the brief prompt containing
only table events, the freshness levels, and dashboard rendering with no keys; story clustering (duplicates merge,
distinct stories and day-apart repeats do not), one-per-story sentiment, subdomain trust matching, corroboration
by content, backtest alignment without look-ahead, the day-over-day diff (including a missing yesterday), and
feedback validation, export, agreement and the dashboard form; Slack alert qualification, dedupe by story, the
hourly cap, escaping and failure handling; and the email job (published sends once, flagged sends the owner
notice, disabled sends nothing, STARTTLS before login, retry cooldown, header injection, dry run, no cost text).

## Known gaps (verify before relying on the output)

- **Working draft.** Expect further integration issues over longer runs. `requirements.txt` has lower bounds only;
  `constraints.txt` pins the exact versions tested together (Python 3.11 on Windows), and the Docker build uses
  it. The Linux image has not been built yet, so a pinned version without a Linux wheel is possible.
- **Model id and prices.** Check current Anthropic model names and prices; prices differ between model versions.
  The configured model rejects forced tool choice (`tool_choice` of type `tool`), so tagging and evaluation offer
  the tool and instruct the model to call it. If a batch comes back without a tool call it is skipped (tagging
  retries next cycle; the evaluator marks unruled claims unsupported).
- **Price feeds.** `scripts/check_symbols.py` resolved all 8 Yahoo candidates and the EIA Brent and WTI series on
  2026-10-25 (a Sunday, so quotes were Friday's closes and no delay could be judged). Twelve Data was not tested (no
  key); its Basic plan lists commodities data as excluded, so only gold is a candidate there and it is unconfirmed.
  Twelve Data's limits (8 credits/min, 800/day) come from its pricing page; Yahoo's and EIA's limits in
  `config.py` are conservative guesses, not published figures. Trading windows are approximate until a market
  calendar is added. Yahoo's chart data used here carries no delay figure, so "delayed" appears only if one is reported.
- **Calendars and event sources.** Brent and TTF use proxy calendars (see "Market context"); `exchange_calendars`
  holiday accuracy for futures venues is unverified. FOMC dates in `events.yaml` were copied from the Federal Reserve
  calendar page on 2026-10-25 and carry no time (the page gives none). No OPEC+ date could be confirmed on an
  official OPEC page (the site refused the fetcher), so the OPEC+ list is empty until you add dates by hand. The EIA
  schedules are parsed from HTML text and can break when EIA changes a page.
- **Phase 3 limits.** The clustering and corroboration thresholds were tuned on small samples (one day's stories, 8
  Serper queries) and will need rechecking. Corroboration is conservative: it missed some genuine rewrites
  (title similarity 0.2-0.3), which only weakens verification. Serper charged 1 credit per call in the 8 test
  calls, matching the code's assumption. The backtest shows no statistics yet: `price_daily` fills only from the
  price poller, so about 30 paired days are needed. Key points use the latest quote at brief time, which on a
  weekend is Friday's close (the diff says 'unchanged' when the quote time is the same). Cluster ids exist only
  for articles collected in the last 48 hours when the feature first ran.
- **Delivery not exercised live.** No real Slack message or email has been sent: the alert and email paths are
  tested with mocked HTTP and a fake SMTP server only. SMTP provider sign-in rules (for example app passwords or
  OAuth-only accounts at Gmail or Microsoft) are unverified and depend on your provider. Slack alerts cover only
  articles saved in the cycle, so after a long outage the hourly cap decides which stories are announced.
- **Phase 5 limits.** The PostgreSQL leg was run against an embedded PostgreSQL 16 on Windows (all tests, plus a copy
  of the real SQLite database and a render of the dashboard on it), not against a hosted server or a different
  major version. The GitHub Actions workflow has not run yet: its first run may expose a pin in `constraints.txt`
  (frozen on Windows) that has no Linux wheel. The Docker image has still not been built (the Docker daemon was not
  running); it now also installs `psycopg`. The dashboard password is a shared-secret gate for local use, not a
  substitute for real authentication. The SQLite and PostgreSQL schemas are kept equal by the shared migrations,
  but SQLite's looser typing can still hide a bad value until it is copied or run on PostgreSQL.
- **Price check uses lagging data.** EIA spot prices arrived about nine days behind the calendar in testing, so
  the Confirmed/Partial/Divergent status compares today's news sentiment with the move between the last two EIA
  observations, which may be from a different week. Treat it as indicative until a fresher price source is added.
- **Cost coverage.** Not counted: Voyage embeddings, hosting, and Claude calls made outside a `cost_run`.
  "Ask the desk" is wrapped (about $0.006 per question, recorded as an `ask_desk` run). Prompt-cache tokens are
  not modeled; the tracker warns if they appear.
- **Apify cost.** Runs reported usage of about $0.0002 each. Compare several runs against the Apify billing page
  before trusting totals; it is not confirmed that the reported usage includes pay-per-event charges for this actor.
- **Listing pages change slowly.** Consecutive cycles often return the same articles, which are skipped as known,
  so most cycles cost only the Apify run.
- **Serper cost.** Failed requests are assumed to be unbilled, and the reported credit count is used if the
  response includes it. Check both against your Serper dashboard.
- **EIA series IDs** (`RBRTE`, `RWTC`, `WCESTUS1`) returned data in testing; keep checking them against the live
  API. Henry Hub is not fetched by the code.
- **Corroboration is simple**: it counts distinct trusted domains in Serper news results and does not confirm the
  results report the same claim. The trust registry matches exact domains only (for example `uk.reuters.com` would
  not match `reuters.com` unless listed).
- **GDELT coverage is titles only and keyword-filtered**, so it brings some loosely related stories and the same
  story from several outlets, tagged separately.
- **Commentary detection is heuristic** and will miss some opinion pieces.
- **Sentiment depends on LLM tags.** Validate the tagger against analyst judgment on a sample, and watch the
  share of magnitude 4-5 tags for score inflation. The score is thin for the first day after the direction
  migration, and its window is by first-seen time, not publication time.
- **URL normalization is basic.** The same story under different URLs, or from different outlets, is still tagged
  separately.
- **RAG context is shallow.** The brief job retrieves archive headlines for context, and "ask the desk" answers
  from the archive, but ingestion embeds only titles and short snippets, so answers cannot go beyond headlines.
- **Brief evaluation is LLM-based** and needs periodic human validation of its verdicts.
- **Data licensing**: confirm terms of use for each source, especially if you add paid feeds (Argus, S&P Global).
- **SQLite and Chroma** suit a single-machine deployment. Move to Postgres/pgvector for multi-user production use.

## License

You may download and use this project for personal use. It is not licensed for redistribution or commercial use.

You must have your own access to every data source and service it uses (Anthropic, Voyage AI, EIA, Serper, Apify
and its actor, and GDELT), including your own accounts and API keys, and you are responsible for complying with
each provider's terms of use and for the costs your usage incurs. No keys, accounts or data are provided with
this project. The full terms are in the `LICENSE` file.

The project is provided as is, without a support commitment; issues and questions may not be answered.