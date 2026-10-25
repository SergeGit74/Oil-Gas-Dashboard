# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An oil and gas morning-intelligence system: a scheduler collects and tags news, a SQLite store holds the results,
and a Streamlit dashboard reads them. `README.md` has the full design and known gaps; `USER_GUIDE.md` is the
operator guide. The project runs on Windows with a project-local `venv/`; it has a git repo and a pytest suite
(`tests/`), and no linter configured. `CLAUDE_CODE_TASKS.md` is the phased task brief (Phases 0-4 are done).

## Commands

Call the venv's programs directly (no activation needed):

```
.\venv\Scripts\python.exe -m jobs.scheduler          # long-running: collection every 20 min, brief at 05:30 Asia/Dubai
.\venv\Scripts\streamlit.exe run app.py              # dashboard (add --server.headless true to skip the email prompt)
.\venv\Scripts\python.exe -m jobs.run_collection     # one collection cycle, standalone
.\venv\Scripts\python.exe -m jobs.run_ingestion      # embed verified articles into Chroma
.\venv\Scripts\python.exe -m jobs.run_evaluation     # generate and evaluate today's brief (replaces today's row)
.\venv\Scripts\python.exe -c "import storage"        # creates/migrates dashboard_store.db
.\venv\Scripts\python.exe -m pytest                  # offline test suite (no network, no paid calls)
.\venv\Scripts\python.exe -m jobs.run_prices         # one price poll (free providers; writes to the real store)
.\venv\Scripts\python.exe scripts/check_symbols.py   # one call per indicator and provider -> reports/symbol_check.json
```

Docker (`Dockerfile`, `docker-compose.yml`, `healthcheck.py`) is an alternative way to run the same code:
`docker compose up -d --build` starts `init` (creates tables), `scheduler` and `dashboard`, sharing one named
volume mounted at `/data`. It has **never been built or run here** (Docker is not installed on this machine), so
treat the Docker files as untested. Keep the non-Docker workflow working: `config.py` defaults (`DATA_DIR` = project
folder, `DESK_TIMEZONE=Asia/Dubai`, 20-minute interval, brief 05:30) must stay equal to the original behavior, and
all three data paths derive from `config.DATA_DIR`. `constraints.txt` holds the exact tested versions the image
build uses; regenerate it from the venv with `pip freeze` after dependency changes.

- **The job commands make real, billed API calls** (Apify, Claude, Serper, Voyage). A collection cycle with new
  articles costs about $0.04; a brief plus evaluation about $0.07. Prefer the offline checks below when you can.
- **Offline checks:** run `python -m pytest`. `tests/conftest.py` points the store at a throwaway database
  (`config.DB_PATH` and `config.EVAL_HISTORY_PATH` must be set *before* `import storage`, which runs `init_db()`
  at import), blanks every API key, and refuses non-loopback sockets, so a test that forgets to mock HTTP fails.
  Use the `db` fixture for a fresh database per test. Render the dashboard without a server with
  `streamlit.testing.v1.AppTest.from_file(<absolute path>)` and check `at.exception`; the path must be absolute.
  `scripts/check_symbols.py` and `jobs.run_prices` make real (free-tier) provider calls; the symbol check spends
  Twelve Data credits.
- **Restart after edits.** The scheduler and Streamlit keep imported modules loaded, so code changes do not take
  effect until the process is restarted (a stale `storage` import produced an `AttributeError` in the dashboard).
- Processes started from Claude Code's background mode die at their timeout (max 2 hours); the user runs the
  long-lived ones in their own terminals. Do not leave a second scheduler running: it doubles every cycle's cost.

## Architecture

Two processes communicate only through `dashboard_store.db` (SQLite, WAL):

- **Scheduler** (`jobs/scheduler.py`): `collection_cycle` every 20 minutes (`run_collection`, `run_ingestion`,
  `check_source_health`) and `daily_brief_cycle` at 05:30 (`run_evaluation`, `check_eval_trend`, source health,
  `check_sentiment_divergence`, then `check_cost_budget`). Each cycle runs inside `cost_tracker.cost_run(...)`, and
  each step is wrapped so one failure does not stop the cycle. One startup price poll and one startup collection
  cycle run immediately. `price_cycle` (every `PRICE_POLL_INTERVAL_MIN`, inside `cost_run("price_poll")`, hidden from
  `latest_marker()` and the dashboard run list) calls `jobs/run_prices.py`; a 03:10 job rolls ticks into
  `price_daily` and purges old ticks.
- **Daily-run claims:** `storage.claim_daily_run(job, day)` (table `run_claims`) is how a once-a-day job avoids
  running twice or retrying forever (2 attempts, 30 minute cooldown). The scheduled brief and `catch_up_brief`
  (runs at startup and after each collection cycle, from 06:00 desk time, when no `briefs` row exists for today)
  share the `daily_brief` claim. Use the same pattern for any new daily job.
- **Missing keys:** `config` never raises at import. Call `config.require_keys(...)` where a key is used; the
  scheduler only warns at startup. The dashboard must start with no paid keys.
- **Dashboard** (`app.py`): read-only against `storage`, except "ask the desk", which is wrapped in its own
  `cost_run`. It must never start the pipeline, call a market-data provider, or send alerts: use
  `check_source_health(notify=False)`. Price tiles read `price_ticks` in a 60 s fragment. A sidebar fragment (`st.fragment(run_every=...)`) drives the refresh modes; "With scheduler" polls
  `storage.latest_marker()` and reruns only when a run finished.

### Collection pipeline (`jobs/run_collection.py`)

Fetch (Apify + GDELT) -> normalize URLs and dedupe -> drop URLs already in `articles` -> source tier, commentary
rule, injection flag -> Claude tagging of new, unflagged articles -> selective Serper corroboration -> confidence
-> save -> sentiment from *stored* last-24h articles -> EIA price check -> save daily snapshot. Articles the model
does not tag are not saved and retry next cycle. Sentiment is computed from the store, not from this cycle's new
articles.

### Price poller (`market_data.py`, `jobs/run_prices.py`, `config/market_symbols.json`)

Providers (Twelve Data, Yahoo, EIA) are tried in `PRICE_PROVIDERS` order and each indicator is served by the first
that returns a quote. Yahoo is an unofficial endpoint, off unless `ENABLE_YAHOO_FALLBACK=true`. All calls go through
`QuotaManager` (table `api_quota`; refuses past 90% of the daily limit; paces per minute; the poll interval is
raised automatically to fit the quota). The symbols and trading windows in the JSON file are candidates and
approximations, not verified facts; `config.py` limits for Yahoo and EIA are guesses. `config/` is a data folder next
to `config.py`: the module wins on import, and a test guards that.

### Market context (`market_calendar.py`, `event_calendar.py`, `freshness.py`)

- `market_calendar.market_status(indicator, now)` -> open / closed / holiday from `exchange_calendars`, with the code
  per indicator in `config/market_symbols.json`. **Brent (`IEPA` = ICE US) and TTF (`XAMS` = Euronext) are proxy
  calendars**; do not describe their holiday labels as exact. The poller's interval, the tiles and the brief's
  market-status line all use it, falling back to the approximate `window` if a calendar fails.
- The `events` table is the only source for the brief's "Watch today" and the dashboard's 7-day list. It is filled by
  `jobs/load_events.py` from `config/events.yaml` (manual, insert-only; copy dates only from an official page and give
  a `source_url`) and from EIA's schedule pages (parsed from page text; a parse failure logs `load_events` degraded and
  keeps old rows). Never add a date from memory. The unique key is `(date, COALESCE(time,''), title)` because SQLite
  treats NULLs in a plain UNIQUE as distinct.
- Weekly EIA series live in `eia_weekly` (written by `data_sources.refresh_eia_weekly`, gated to
  `EIA_WEEKLY_REFRESH_HOURS`); the dashboard reads the table and must not call EIA. `freshness.header()` feeds the
  dashboard header from `job_runs`, `price_ticks` and `briefs`.
- Anything added to the brief prompt must also go into the `context` passed to `evaluate_brief`, or its numbers fail
  the numeric-fidelity check.

### Delivery (`high_impact_alerts.py`, `brief_email.py`, `jobs/send_brief_email.py`)

- Slack alerts run at the end of `run_collection` on that cycle's saved articles. Only verified (confidence >= 0.5,
  not injection-flagged), direction-tagged articles with magnitude >= `ALERT_MIN_MAGNITUDE` qualify; one message per
  cluster ever (`alert_log`, claimed with INSERT OR IGNORE *before* sending); `ALERT_MAX_PER_HOUR` caps bursts and
  suppressed ones are not replayed. Scraped titles go through `slack_escape`. Use `alerts.send_slack` (plain post)
  for news alerts and `alerts.send_alert` for system warnings.
- The brief email is sent by `send_brief_email()` from the scheduler (after each collection cycle and the daily
  brief). Published -> `EMAIL_TO`; flagged -> a short notice to the owner only, never the brief text. Exclusivity and
  retry cap come from `claim_daily_run("brief_email", day)` (3 attempts, 30 min cooldown); the "sent" marker is
  `email_log`, because a claim alone would allow a second send after a success. TLS is mandatory (STARTTLS or port
  465); keep `clean_header`/`parse_addresses` on every header value; never log the password or put cost figures or
  low-confidence articles in a message. `briefs.sources_json` holds the numbered sources the email lists.
- `python -m jobs.send_brief_email --dry-run` writes an `.eml` to `outbox/` and changes no state. No live Slack
  message or email has been sent from this repo: tests use mocked HTTP and a fake SMTP class.

### Trust model (spans `llm_pipeline.py`, `storage.py`, `config.py`)

- "Verified" = `confidence >= config.VERIFIED_MIN_CONFIDENCE` (0.5) and not injection-flagged. Only verified
  articles may reach sentiment, the vector store or the brief; everything else is shown only in the dashboard's
  unverified panel.
- Confidence = tier base (`TRUST_REGISTRY`: official 0.95, wire_trade 0.80, aggregator 0.60, unknown 0.35),
  x0.7 for commentary, +0.10 per corroborating trusted domain (max 2).
- `TRUST_REGISTRY` matches by whole-label domain suffix (`uk.reuters.com` matches `reuters.com`; `notreuters.com`
  does not): use `llm_pipeline.source_tier` / `site_key`, never compare domains by hand. The `wire_trade` tier also
  serves as the Serper corroboration set, so adding a weak outlet there weakens verification. Changing the
  registry or the matching does not re-tier stored rows; that needs a one-off update of `articles.source_tier` and
  `confidence` (not done for the existing rows).
- Corroboration counts a Serper result only if its site is trusted, is not the article's own outlet, and its title
  is similar to the article's headline (`CORROBORATION_MIN_SIMILARITY`, title-to-title only; titles plus snippets
  matched unrelated pages in calibration). Matches are kept in `articles.corroboration_json`.
- Story clustering (`story_cluster.py`, local TF-IDF, free): `articles.cluster_id` groups near-duplicate headlines
  within 24 h; `cluster_new_articles()` runs after each save and only fills empty ids. Sentiment must go through
  `story_cluster.one_per_cluster` so each story counts once. The brief's source list is not deduplicated (its
  numbering is shared with the evaluator).
- Key points and "what changed": `briefs.key_points_json` is written by `run_evaluation`; the diff is computed in
  code (`key_points.py`) and the model only phrases it. Anything new in the prompt also goes in the evaluator
  `context`. The backtest (`backtest.py`) must stay free of look-ahead and must say "insufficient data" under 30
  observations. The dashboard's only writes are "ask the desk" and `storage.add_feedback` (validated plain insert).
- Scraped text is data: it is wrapped in `<article>`/`<source>` tags in prompts with instructions not to obey it.

### Cost accounting

- All Claude calls must go through `tracked_llm.create_message(step, ...)`, Serper through
  `data_sources.serper_news`, Apify through `data_sources.fetch_apify_commodity_news`; these record cost into the
  active `RunCost`. A call made outside a `cost_run` is silently uncounted. Nested `cost_run` calls join the outer one.
- Unknown model ids are tracked as unpriced tokens, not guessed. Prices live in `pricing.py` (env-overridable).
- "Days" in cost views and the budget check are UTC; the scheduler's timezone is `config.DESK_TIMEZONE`.

### Brief generation and evaluation

- `generate_brief` numbers sources 1..N by list order; `brief_evaluation` relies on the same list and numbering
  (citations `[n]`, coverage of magnitude >= 4 stories). Pass the same list to both.
- The brief's title and date are prepended by code from the run date; the model is told not to write one (it
  previously used the latest EIA date). The evaluator receives EIA figures and the sentiment reading as trusted
  reference data, and every number in the brief must appear in a source or that reference text.
- Publish gate: groundedness >= 0.90, no unmatched numbers, coverage >= 0.70; otherwise `flagged_for_review`.

## Gotchas learned from live runs

- The configured Claude model rejects forced tool use (`tool_choice` of type `tool`/`any`). Tools are offered
  with the default choice and the prompt says to call them; code must tolerate a missing tool call.
- The Anthropic key in `.env` must be scoped to a workspace; an organization-scoped key returns a 400 unless
  `ANTHROPIC_WORKSPACE_ID` is set (read in `tracked_llm.py`).
- The Apify actor `topsail/compliant-commodity-intel` needs `sources` as a list of `{url, commodity}` objects;
  strings crash it. A failed run is still billed, so attempts are costed individually.
- GDELT's DOC API returned 429 persistently from this machine, so `fetch_gdelt` reads the static 15-minute GKG
  files (`data.gdeltproject.org/gdeltv2`) and filters by theme plus title keyword. It yields titles only.
- EIA spot data lags the calendar by about nine days, so the price-confirmation status compares current news
  sentiment with an older price move.
- The dashboard has no auth and "ask the desk" spends API money, so `.streamlit/config.toml` binds it to
  `127.0.0.1`. Keep it that way (the Docker image overrides it to `0.0.0.0` inside the container, and compose
  publishes only to the host's `127.0.0.1`).
- Commits should use the GitHub noreply email (set in this repo's local git config), not a personal address.
- `.env` holds live secrets. Do not print or read it back; check which keys are set with
  `config.get(name)` truthiness only. `dashboard_store.backup.db` may exist from a manual re-tiering backup.
