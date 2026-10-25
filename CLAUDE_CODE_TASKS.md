# Task brief for Claude Code: market prices, delivery, and quality upgrades

Project: `oil_gas_dashboard` (Python, Windows, local single-user). Stack: Streamlit dashboard, APScheduler jobs, SQLite store, Chroma vector store, Anthropic API for tagging and briefs, Serper, Apify, EIA, GDELT.

## How to work (read first)

1. Read `README.md`, `config.py`, `storage.py`, `data_sources.py`, `llm_pipeline.py`, `app.py`, and everything in `jobs/` before changing anything.
2. Work on ONE phase per session. Start by presenting a short plan (files to change or create, new tables, new env vars, assumptions) and wait for approval before editing.
3. Keep the existing conventions:
   - Secrets only in `.env`, loaded through `config.py`. Never put keys in code, tests, logs, or the README. Add new variables to `.env.example`.
   - Every external source logs its status to `job_runs` and is registered in `SOURCE_HEALTH_CONFIG`.
   - Every scheduler cycle runs inside `cost_run(...)`.
   - Unverified or low-confidence content never enters an LLM prompt, the vector store, or an email or alert.
   - The dashboard is read-only against the store. It must not call Anthropic, Serper, Apify, a market-data provider, or send email. The only allowed write from the dashboard is a plain database insert of analyst feedback (Phase 3).
4. Database changes must be additive and idempotent (`CREATE TABLE IF NOT EXISTS`, guarded `ALTER TABLE ADD COLUMN`). Never delete or rewrite existing user data.
5. Tests: use `pytest`. No real network or paid-API calls in tests. Mock HTTP, use fixtures. Run the tests and fix failures before reporting.
6. Use `pathlib`, avoid shell-specific commands inside Python code (the machine is Windows). Scheduler timezone is `Asia/Dubai`.
7. Do not claim something is verified unless you ran it. If an endpoint, symbol, series ID, or provider limit cannot be confirmed from the official docs or a live test, make it configurable, say so in your report, and do not guess silently.
8. At the end of each phase: update `README.md` (new files, env vars, tables, "Known gaps"), then report in this format: what changed, how to run it, test results, what is unverified or assumed, suggested next step.
9. If a git repository exists, commit once per phase with a clear message. Do not push.

## Out of scope (do not do)

Deploying anywhere, paid data feeds, trading or execution features, investment-advice wording, changing the LLM model, adding LLM calls where deterministic code is enough.

## The 8 market indicators

Configuration lives in `config/market_symbols.json` (indicator key, label, unit, currency, trading window, and per-provider symbol). The symbols below are CANDIDATES only: verify each one with the discovery script in Phase 1 and report which resolve on each provider's free plan.

| # | Key | Indicator | Why it matters | Candidate Yahoo symbol | Notes |
|---|---|---|---|---|---|
| 1 | BRENT | Brent crude, front month | Global crude benchmark; physical grades price off it | `BZ=F` | USD/bbl |
| 2 | WTI | WTI crude, front month | US benchmark; Brent-WTI spread input | `CL=F` | USD/bbl |
| 3 | HENRY_HUB | Henry Hub natural gas | US gas benchmark | `NG=F` | USD/MMBtu |
| 4 | TTF | Dutch TTF gas, front month | European gas and LNG pricing driver | `TTF=F` | EUR/MWh; do not mix units with Henry Hub |
| 5 | GOLD | Gold (spot XAU/USD or futures) | Safe-haven and geopolitical-risk gauge | `GC=F` | Spot may be on forex-style feeds |
| 6 | DXY | US dollar index | Oil is priced in USD; strong dollar pressures prices | `DX-Y.NYB` | Fallback: EUR/USD |
| 7 | SPX | S&P 500 | Risk appetite and demand proxy | `^GSPC` | Fallback: SPY as proxy |
| 8 | ULSD | ULSD / heating oil futures (diesel) | Product market; distillates are a recurring storyline; crack-spread input | `HO=F` | USD/gal |

Swap candidates if you prefer: VIX, US 10-year yield, energy-sector ETF (XLE), RBOB gasoline, a China equity index. There is likely no free feed for Dubai, Oman, or Murban benchmarks; do not pretend otherwise.

Derived values computed at read time, with no extra API calls: Brent-WTI spread, gold/Brent ratio. Do NOT compute a TTF-Henry Hub spread unless currency and unit conversion is explicitly implemented and tested.

---

## Phase 0: Safety foundations (do first)

Goal: the dashboard never starts a paid run, and the daily brief catches up safely.

- Add `claim_daily_run(job, day, max_attempts=2, cooldown_min=30)` to `storage.py` (table `run_claims`, atomic `UPDATE ... WHERE attempts < ? AND (last_attempt IS NULL OR last_attempt < ?)`, success means `rowcount == 1`).
- Move the "did the morning run happen, and if not run it" logic out of `app.py` into the scheduler: a `catch_up_brief()` job that runs at scheduler startup and after every collection cycle once the local time is past 06:00 Asia/Dubai. Any `briefs` row for today (including `flagged_for_review`) counts as "happened". Use the claim so a failing run is retried at most twice a day, with a cooldown.
- The dashboard shows "Brief pending" when no brief exists. Remove any dashboard code that triggers the pipeline. A manual "Run now" button is allowed only if it is protected (see Phase 5 auth), uses the same claim, and is wrapped in `cost_run`.
- `check_source_health(..., notify=False)` for dashboard use so page loads never send alerts or write to `job_runs`.
- Make `config.py` stop raising at import time for missing paid-API keys. Use `require_keys(...)` at each call site instead, so the dashboard can start with no paid keys.

Acceptance: tests for the claim (two concurrent claimers, only one wins; cap and cooldown respected) and for catch-up (no brief, flagged brief, published brief).

## Phase 1: Price poller

Goal: near-real-time prices for the 8 indicators within free-tier limits, stored locally and shown on the dashboard.

- `market_data.py` with a provider interface `fetch_quotes(indicators) -> list[Quote]`. `Quote`: indicator_key, price, prev_close, change_pct, as_of (UTC ISO), provider, delayed_minutes (nullable), currency.
- Providers, tried in order by config: (1) Twelve Data (key `TWELVE_DATA_API_KEY`), (2) Yahoo Finance unofficial endpoint (no key; enabled by `ENABLE_YAHOO_FALLBACK=true`; mark clearly as unofficial and review its terms before relying on it), (3) EIA daily spot for Brent and WTI (existing). Consult each provider's current official documentation for endpoints, symbol names, and free-plan limits instead of relying on memory.
- `scripts/check_symbols.py`: for each indicator and provider, test one call and print whether the symbol resolves, the latest price, the as-of time, and whether the data appears delayed. Works without keys by skipping keyed providers with a clear message. Save the result to `reports/symbol_check.json`. Do not silently pick symbols.
- Quota manager: table `api_quota(provider, day_utc, calls)`; limits per minute and per day from config; refuse calls beyond 90% of the daily limit; per-minute pacing. Count credits as the provider documents (verify whether batch requests count per symbol).
- `jobs/run_prices.py`, registered in the scheduler separately from the news cycle. Default every 5 minutes inside each instrument's trading window, every 30 to 60 minutes outside it, and automatically lengthen the interval so the daily quota is not exceeded. All intervals configurable.
- Storage: `price_ticks(indicator_key, ts, price, prev_close, change_pct, provider, delayed_minutes)` with an index on (indicator_key, ts); `price_daily` for daily closes; keep ticks 30 days then downsample and purge.
- Log `fetch_prices` to `job_runs` (`success`, `degraded`, `skipped`) and register it in `SOURCE_HEALTH_CONFIG` (threshold 3). Record per-provider call counters with `record_counter`.
- Dashboard: tiles for the 8 indicators showing price, change, as-of time, and a "delayed N min" label; a small 24-hour chart per tile; refresh with `st.fragment(run_every="60s")` reading only the store. Tiles with old data are visibly marked stale.

Acceptance: unit tests with mocked responses for each provider, the fallback order, the quota refusal at 90%, and the tick retention job.

## Phase 2: Market context (improvements 1, 3, 4, 5)

1. **Market calendar awareness.** Per-instrument trading-session config and `market_status(indicator, now)` returning open, closed, or holiday, using a maintained calendar library (choose one, document why). Tiles show "Closed, last close <date>" instead of presenting stale moves as overnight moves. The brief prompt receives a market-status line (for example "futures closed Sunday").
3. **Staleness indicators.** Dashboard header shows "last collection N min ago", "last price tick N min ago", "brief generated at HH:MM", turning amber or red past configurable thresholds, computed from `job_runs` and `price_ticks`.
4. **Gas and spread coverage.** Henry Hub and TTF appear through the indicators; add the Brent-WTI spread tile; add EIA weekly distillate stocks and natural gas storage. Find the correct EIA routes and series through the live API route browser; do not guess series IDs.
5. **Event calendar.** Table `events(date, time, tz, title, category, source_url)` filled from `config/events.yaml` (manual entries: OPEC+ meetings, FOMC dates) plus recurring EIA release dates taken from EIA's published schedule (do not hardcode from memory). The brief's "Watch today" section is built only from this table; the model must not invent dates. Dashboard shows the next 7 days.

Acceptance: tests for `market_status` across a weekend, a holiday, and a normal day; a test that the brief prompt contains only events from the table.

## Phase 3: Trust and quality (improvements 2, 10, 8, 9, 7)

2. **Story clustering.** Cluster near-duplicate headlines within a 24-hour window (try a local method first, such as TF-IDF or MinHash, to avoid API cost; use embeddings only if the local method is clearly worse on a sample). Store `cluster_id`; the sentiment score counts one item per cluster (highest confidence, then highest magnitude); the dashboard shows "reported by N outlets".
10. **Corroboration fix.** A Serper result counts as corroboration only if its title or snippet matches the claim (similarity above a configurable threshold). Match trusted sources by registrable domain or domain suffix, so `uk.reuters.com` matches `reuters.com`. Save matched URLs in `articles.corroboration_json`.
8. **Backtest.** `scripts/backtest_sentiment.py` and a dashboard "Research" tab: sentiment score versus subsequent Brent returns (next day and 3 days) from `sentiment_history` and `price_daily`. No look-ahead. Show correlation, hit rate by label, and sample size; show "insufficient data" below 30 observations. State on the page that it is descriptive, not predictive.
9. **What changed since yesterday.** Store structured key points per brief (JSON: top stories, sentiment, prices). Compute the day-over-day diff deterministically (new top stories, sentiment change, price changes). The model may only phrase the diff, not add facts. If yesterday's brief is missing, say so.
7. **Analyst feedback.** Table `feedback(item_type, item_id, rating, comment, created_at, analyst_label)`. Thumbs up or down with an optional comment on article tags and brief sections. Script to export a golden-set CSV, and a script reporting tagger agreement against analyst corrections.

Acceptance: tests for clustering (duplicates merge, distinct stories do not), subdomain trust matching, backtest alignment with no look-ahead, and the diff logic.

## Phase 4: Delivery (improvements 6, 14)

6. **High-impact alerts.** After tagging, a verified (High or Medium confidence) article with magnitude at or above `ALERT_MIN_MAGNITUDE` (default 4) sends one Slack message per cluster. Table `alert_log` for dedupe, a maximum number of alerts per hour, and never for unverified items.
14. **Brief email.** `jobs/send_brief_email.py`: SMTP settings from `.env` (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_FROM`, `EMAIL_TO`), `EMAIL_ENABLED=false` by default. Send once per day using `claim_daily_run("brief_email", day)`. Send only published briefs; for a flagged brief send a "needs review" notice to the owner only. Plain text plus HTML with: status, sources, generated time, price snapshot, and "what changed since yesterday". TLS, timeouts, header sanitization. A dry-run mode that writes an `.eml` file. Test with mocks or a local debugging SMTP server. Never include low-confidence items or cost details.

Acceptance: tests for alert dedupe and rate limit, and for the email job (published sends once, flagged sends the owner notice, disabled sends nothing).

## Phase 5: Engineering (improvements 11, 12, 13)

11. **Tests and CI.** Fill gaps in the pytest suite (URL normalization, sentiment weighting, price confirmation, cost math, source trust, quota manager, market status). Add `requirements-dev.txt`, `ruff`, and a GitHub Actions workflow that runs lint and tests with no secrets and no network (fail a test that tries a real network call).
12. **Optional dashboard password.** If `DASHBOARD_PASSWORD` is set, require it before showing anything (constant-time comparison, attempt limiting, session state). Off by default for local use. Document that real deployment needs platform-level authentication.
13. **Optional Postgres.** Keep SQLite as the default. Add `DATABASE_URL` support behind the existing storage functions, with versioned migrations and a script to copy existing data. Run the same tests against both backends. Do not build hosting or pgvector; document them as future work.

## Known uncertainties to verify and report (not assume)

Twelve Data free-plan symbol coverage and credit accounting; Yahoo terms of use and delay; EIA series IDs and release schedule; Serper credit counting; SMTP provider sign-in rules; calendar library holiday accuracy for futures exchanges.

## New environment variables (add to `.env.example`)

`TWELVE_DATA_API_KEY`, `ENABLE_YAHOO_FALLBACK`, `PRICE_POLL_INTERVAL_MIN`, per-provider quota limits, `ALERT_MIN_MAGNITUDE`, `EMAIL_ENABLED`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_FROM`, `EMAIL_TO`, `DASHBOARD_PASSWORD` (optional), `DATABASE_URL` (optional).
