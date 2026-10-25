# Oil & Gas Morning Intelligence Dashboard

An automated system that collects oil and gas market news and related geopolitical/macro news, scores each
item for source trust and confidence, tags it for market impact and Bull/Bear/Neutral direction, grounds an
LLM-generated morning brief in verified content only, checks the brief before publishing, tracks what each run
costs, and serves everything through a live Streamlit dashboard. It is built for oil and gas market analysts and
physical contract traders who need a reliable pre-market briefing.

This project is designed to be built and extended with **Claude Code**.

**Status:** working draft. The collection cycle, tagging, sentiment and price check, ingestion, brief generation
and evaluation, "ask the desk" and the dashboard have each been run against the live APIs once or a few times.
Not yet exercised: Slack delivery, the cost-budget, evaluation-trend and sentiment-divergence alerts firing, and
long unattended scheduler runs. Treat the "Known gaps" section as a checklist before relying on any output for
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
|                    -> save sentiment snapshot
|    run_ingestion:  embed verified articles into the Chroma vector store (RAG)
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
storage.py (SQLite shared store: articles, briefs, sentiment_history, job_runs, run_costs, run_claims,
|           price_ticks, price_daily, api_quota, eia_weekly, events)
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
| `EIA_API_KEY` | EIA Open Data (free) | Yes | Brent/WTI spot prices, weekly crude stocks |
| `SERPER_API_KEY` | Serper | Yes | Claim corroboration (news search) |
| `APIFY_TOKEN` | Apify | Yes | Running `topsail/compliant-commodity-intel`; also used to read run usage for cost tracking |
| `SLACK_WEBHOOK_URL` | Slack incoming webhook | Optional | Alerts; falls back to logs and console if unset |

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
| `DAILY_COST_BUDGET_USD` | `10` | Daily total above which a budget alert fires |
| `SONNET_INPUT_USD_PER_MTOK`, `SONNET_OUTPUT_USD_PER_MTOK` | `2.00`, `10.00` | Price overrides for the default model |
| `ANTHROPIC_PRICE_MULTIPLIER` | `1.0` | For example 1.1 if you use US-only inference pricing |
| `SERPER_USD_PER_CREDIT` | `0.001` | Entry-pack rate; lower in larger packs |
| `APIFY_FALLBACK_USD_PER_RESULT`, `APIFY_FALLBACK_USD_PER_START` | `0.0005`, `0` | Used only when Apify does not report a run's usage |

### .env.example

```bash
ANTHROPIC_API_KEY=
VOYAGE_API_KEY=
EIA_API_KEY=
SERPER_API_KEY=
APIFY_TOKEN=

ANTHROPIC_MODEL=claude-sonnet-5-5
DAILY_COST_BUDGET_USD=10

# Optional: only for organization-scoped API keys (prefer a workspace-scoped key)
# ANTHROPIC_WORKSPACE_ID=

# Optional: alerts fall back to console/log only if not set
SLACK_WEBHOOK_URL=

# Optional price overrides (defaults live in pricing.py; verify against vendor pricing pages)
# SONNET_INPUT_USD_PER_MTOK=2.00
# SONNET_OUTPUT_USD_PER_MTOK=10.00
# ANTHROPIC_PRICE_MULTIPLIER=1.0
# SERPER_USD_PER_CREDIT=0.001
# APIFY_FALLBACK_USD_PER_RESULT=0.0005
# APIFY_FALLBACK_USD_PER_START=0
```

### .gitignore

```bash
.env
venv/
__pycache__/
*.pyc
oil_gas_archive/
dashboard_store.db
dashboard_store.backup.db
eval_history.jsonl
```

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
```

## Running the system

Two long-running processes, each in its own terminal (or as managed services in production):

```bash
# Process 1: scheduler (collection, ingestion, brief, evaluation, health and cost checks)
python -m jobs.scheduler

# Process 2: dashboard (reads from the shared store)
streamlit run app.py
```

The dashboard has no login, and "ask the desk" spends your API credit, so it must not be reachable by other
people. `.streamlit/config.toml` makes `streamlit run app.py` listen on `127.0.0.1` (this PC only); the Docker
setup publishes the port to `127.0.0.1` too. Do not change either to `0.0.0.0` or forward the port unless you add
authentication in front of it.

| Job | Trigger | What it does |
|---|---|---|
| Collection cycle | Every 20 minutes | Collect, skip known articles, tag new ones, corroborate, compute sentiment and price confirmation, ingest verified articles, check source health |
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
├── storage.py                # SQLite store and accessors
├── alerts.py                 # send_alert: log, console, and Slack if configured
├── data_sources.py           # EIA, GDELT (GKG files), Apify (async run, retry, cost), Serper (credits), normalize_url
├── llm_pipeline.py           # source trust registry, news/commentary split, injection flags, tagging, brief generation
├── sentiment_analysis.py     # weighted Bull/Bear/Neutral aggregation, current_sentiment, divergence check
├── price_confirmation.py     # sentiment vs Brent price move: Confirmed/Partial/Divergent
├── market_calendar.py        # open / closed / holiday per indicator (exchange_calendars), market-status line
├── event_calendar.py         # events table: manual YAML + EIA release schedules; Watch-today text
├── freshness.py              # dashboard header: collection / price / brief age with amber and red levels
├── market_data.py            # price providers (Twelve Data, Yahoo, EIA), quota manager, trading windows, polling plan
├── config/market_symbols.json # the 8 indicators: label, unit, currency, calendar code, fallback window, per-provider symbol
├── config/events.yaml        # manual scheduled events (FOMC dates; add OPEC+ meetings by hand)
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
│   ├── load_events.py        # load_events: fill the events table
│   ├── run_evaluation.py     # generate and evaluate the morning brief
│   ├── check_eval_trend.py   # groundedness drift monitoring
│   ├── source_health.py      # generic consecutive-degradation monitor (Apify, Serper, EIA)
│   └── check_cost_budget.py  # yesterday's total cost vs DAILY_COST_BUDGET_USD
├── oil_gas_archive/          # Chroma vector store (generated, gitignored)
├── dashboard_store.db        # SQLite store (generated, gitignored)
└── eval_history.jsonl        # evaluation log (generated, gitignored)
```

## Data model (SQLite)

| Table | Contents |
|---|---|
| `articles` | Tagged articles: category, direction, magnitude, confidence, reason, content type. `collected_at` is the time the article was first seen |
| `briefs` | One brief per day with status (`published` or `flagged_for_review`), groundedness score, evaluation JSON |
| `sentiment_history` | One row per day: label, score, counts, price benchmark, price change, confirmation status |
| `job_runs` | Status log for every job and source (`success`, `degraded`, `skipped`, `failed`, `alert`, `ok`) |
| `run_claims` | `(job, day)` -> attempts and last attempt; lets a daily job run at most N times a day, spaced by a cooldown |
| `price_ticks` | One row per fetched quote: indicator, quote time `ts`, price, previous close, change %, provider, `delayed_minutes`, `fetched_at`. Kept 30 days |
| `price_daily` | Daily open/high/low/close per indicator, rolled up from ticks before they are purged |
| `eia_weekly` | Weekly EIA series (crude stocks, distillate stocks, Lower 48 gas storage): series, period, value, unit |
| `events` | Scheduled events: date, time, tz, title, category, source_url, source. Unique on (date, time, title) |
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

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The suite uses a throwaway database, blank API keys and blocks non-loopback network connections, so it makes no
real or paid calls. It covers the daily-run claim (concurrent claimers, attempt cap, cooldown), the brief catch-up
(no brief, flagged brief, published brief), the three price providers with mocked responses, the fallback order,
the 90% quota refusal, per-minute pacing, trading windows, tick retention, `market_status` on a weekend, a holiday and a normal day, the spread math, EIA weekly storage and
gating, the EIA schedule parsers, event loading (idempotent; a bad page keeps old rows), the brief prompt containing
only table events, the freshness levels, and dashboard rendering with no keys.

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