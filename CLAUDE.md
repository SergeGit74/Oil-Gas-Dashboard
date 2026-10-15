# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An oil and gas morning-intelligence system: a scheduler collects and tags news, a SQLite store holds the results,
and a Streamlit dashboard reads them. `README.md` has the full design and known gaps; `USER_GUIDE.md` is the
operator guide. The project runs on Windows with a project-local `venv/`; there is no git repo, no test suite and
no linter configured.

## Commands

Call the venv's programs directly (no activation needed):

```
.\venv\Scripts\python.exe -m jobs.scheduler          # long-running: collection every 20 min, brief at 05:30 Asia/Dubai
.\venv\Scripts\streamlit.exe run app.py              # dashboard (add --server.headless true to skip the email prompt)
.\venv\Scripts\python.exe -m jobs.run_collection     # one collection cycle, standalone
.\venv\Scripts\python.exe -m jobs.run_ingestion      # embed verified articles into Chroma
.\venv\Scripts\python.exe -m jobs.run_evaluation     # generate and evaluate today's brief (replaces today's row)
.\venv\Scripts\python.exe -c "import storage"        # creates/migrates dashboard_store.db
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
- **Offline checks:** there are no tests, so verify with ad-hoc scripts. Point the store at a throwaway database
  by setting `config.DB_PATH` and `config.EVAL_HISTORY_PATH` *before* `import storage` (it runs `init_db()` at
  import). Render the dashboard without a server using `streamlit.testing.v1.AppTest.from_file(<absolute path>)`
  and check `at.exception`; the path must be absolute.
- **Restart after edits.** The scheduler and Streamlit keep imported modules loaded, so code changes do not take
  effect until the process is restarted (a stale `storage` import produced an `AttributeError` in the dashboard).
- Processes started from Claude Code's background mode die at their timeout (max 2 hours); the user runs the
  long-lived ones in their own terminals. Do not leave a second scheduler running: it doubles every cycle's cost.

## Architecture

Two processes communicate only through `dashboard_store.db` (SQLite, WAL):

- **Scheduler** (`jobs/scheduler.py`): `collection_cycle` every 20 minutes (`run_collection`, `run_ingestion`,
  `check_source_health`) and `daily_brief_cycle` at 05:30 (`run_evaluation`, `check_eval_trend`, source health,
  `check_sentiment_divergence`, then `check_cost_budget`). Each cycle runs inside `cost_tracker.cost_run(...)`, and
  each step is wrapped so one failure does not stop the cycle. One startup cycle runs immediately.
- **Dashboard** (`app.py`): read-only against `storage`, except "ask the desk", which is wrapped in its own
  `cost_run`. A sidebar fragment (`st.fragment(run_every=...)`) drives the refresh modes; "With scheduler" polls
  `storage.latest_marker()` and reruns only when a run finished.

### Collection pipeline (`jobs/run_collection.py`)

Fetch (Apify + GDELT) -> normalize URLs and dedupe -> drop URLs already in `articles` -> source tier, commentary
rule, injection flag -> Claude tagging of new, unflagged articles -> selective Serper corroboration -> confidence
-> save -> sentiment from *stored* last-24h articles -> EIA price check -> save daily snapshot. Articles the model
does not tag are not saved and retry next cycle. Sentiment is computed from the store, not from this cycle's new
articles.

### Trust model (spans `llm_pipeline.py`, `storage.py`, `config.py`)

- "Verified" = `confidence >= config.VERIFIED_MIN_CONFIDENCE` (0.5) and not injection-flagged. Only verified
  articles may reach sentiment, the vector store or the brief; everything else is shown only in the dashboard's
  unverified panel.
- Confidence = tier base (`TRUST_REGISTRY`: official 0.95, wire_trade 0.80, aggregator 0.60, unknown 0.35),
  x0.7 for commentary, +0.10 per corroborating trusted domain (max 2).
- `TRUST_REGISTRY` matches exact domains only (`uk.reuters.com` != `reuters.com`). The `wire_trade` tier also
  serves as the Serper corroboration set, so adding a weak outlet there weakens verification. Changing the
  registry does not re-tier stored rows; that needs a one-off update of `articles.source_tier` and `confidence`.
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
- `.env` holds live secrets. Do not print or read it back; check which keys are set with
  `config.get(name)` truthiness only. `dashboard_store.backup.db` may exist from a manual re-tiering backup.
