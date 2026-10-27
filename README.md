# Oil & Gas Morning Intelligence Dashboard

A scheduled pipeline that collects oil and gas market news and prices, scores each item for source trust and market impact, writes a grounded morning brief, **checks that brief against its sources before publishing it**, and records what every run costs. A Streamlit dashboard shows the result.

**Status: working draft.** Core parts have run against live APIs a few times; several delivery and deployment paths have only been tested with mocks. See [What has actually been run](#what-has-actually-been-run) and [Known limitations](#known-limitations).

- Demo: [TODO: demo URL]
- Walkthrough video (2-3 min): [TODO: video URL]
- Operator guide: [`USER_GUIDE.md`](USER_GUIDE.md)
- Full design and settings reference: [`docs/REFERENCE.md`](docs/REFERENCE.md)

> **Not financial, investment or trading advice.** Briefs, tags, sentiment and price checks are generated automatically by language models and data feeds. They can be wrong or out of date (for example, EIA spot prices lag by days) and have not been independently validated.

[TODO: add 2-3 screenshots or a GIF: dashboard overview, the brief with its groundedness score, the cost panel]

## What it does

For analysts and physical-contract traders who need a pre-market briefing, the system:

1. **Collects** news from an Apify actor (OilPrice, World Oil, Rigzone) and GDELT's free GKG files, plus official data from the EIA API and quotes for 8 market indicators (Brent, WTI, Henry Hub, TTF, gold, DXY, S&P 500, ULSD).
2. **Scores trust.** Each outlet gets a static tier, commentary is separated from news by rules, and a story's confidence rises when independent trusted outlets report it (checked through Serper).
3. **Tags** each new article with Claude (category, direction, impact 1-5) using schema-validated tool calls, and skips articles it has already stored.
4. **Measures sentiment** as a daily Bull/Bear/Neutral score and compares it with the Brent move.
5. **Writes a morning brief** only from verified articles, then decomposes it into claims and checks every claim and number against the sources. Briefs that fail go to review instead of the dashboard.
6. **Tracks cost** per run (Claude, Apify, Serper) and monitors the health of each data source.
7. **Delivers** high-impact Slack alerts and an optional once-a-day email.

## Why it is built this way

| Problem | Design response |
|---|---|
| Rumors and low-quality sources reaching the model | Verified means confidence of at least 0.5 and not injection-flagged. Only verified items reach sentiment, the vector store, or the brief; the rest appear in a separate "unverified" panel |
| Prompt injection through scraped pages | Scraped text is treated as data: instruction-like text is flagged and excluded, and prompts wrap content in delimiters |
| Invented facts and numbers in the brief | Every claim is checked against the numbered sources, every number must appear in a source or trusted reference data, and magnitude 4-5 stories must be cited. Publish gate: groundedness at least 0.90, no unmatched numbers, coverage at least 0.70 |
| The same story counted many times | Local TF-IDF clustering groups near-duplicate headlines; sentiment counts one article per story |
| Weak corroboration | A Serper result counts only if its site is trusted, is not the article's own outlet, and its title is similar to the headline |
| Jobs that retry or run twice | An atomic once-a-day claim (`run_claims`) caps attempts and spaces retries, so a failing run cannot loop and spend money |
| Unreliable inputs | Retry with backoff, an Apify run abort on timeout, GDELT as fallback, and per-source health alerts |
| Hidden spend | Every Claude, Serper and Apify call is recorded per run; known URLs are skipped before any model call; a daily budget alert fires when exceeded |
| Dashboard exposure | The dashboard is read-only against the store, binds to `127.0.0.1`, and has an optional password gate. It never starts the pipeline or calls a market-data provider |

## Architecture

```
scheduler process (APScheduler)                                         dashboard process (Streamlit)
  every 20 min   collect (Apify, GDELT, EIA) -> skip stored URLs                     |
                 -> trust tier + commentary rule + injection flag                    |  read-only
                 -> Claude tagging (new articles only) -> Serper corroboration       v
                 -> cluster -> save -> sentiment + Brent check ---------->  shared store  <------ feedback
                 -> Slack alert per high-impact story                    (SQLite, or PostgreSQL)
                 -> embed verified articles (Chroma) for archive context              ^
  every 5 min    price poll: Twelve Data / Yahoo / EIA fallback chain -----------------+
  daily 05:30    brief -> claim-level evaluation -> publish or flag -> optional email
                 (catch-up if missed; deployment mode runs a few times a day instead)
```

All spend happens in the scheduler process. Details are in [`docs/REFERENCE.md`](docs/REFERENCE.md).

## What has actually been run

| Evidence level | Capabilities |
|---|---|
| **Run against live APIs** (once or a few times) | Collection, tagging, sentiment and price check, ingestion, brief generation and evaluation, "ask the desk", the dashboard. The price poller, event calendar, story clustering and the corroboration change ran live for short periods |
| **Tested with mocks only** | Slack delivery, the brief email, deployment mode (`SCHEDULER_MODE=deployment`), the brief catch-up under real scheduling |
| **Not yet exercised** | The cost-budget, evaluation-trend and sentiment-divergence alerts firing; the backtest (needs about 30 days of prices); a hosted PostgreSQL server; the Docker image (never built); long unattended scheduler runs |

## Engineering practices

- **Tests:** a pytest suite that blocks all non-loopback network access, blanks API keys and uses a throwaway database, so no test can make a paid call. Database tests run on SQLite and on PostgreSQL.
- **Lint and CI:** `ruff` and a GitHub Actions workflow that runs both against a PostgreSQL 16 service container. [TODO: add the CI badge after the first green run; the workflow had not run when this was written]
- **Schema:** versioned, idempotent migrations (`migrations.py`), one SQL layer that runs unchanged on SQLite and PostgreSQL, and a read-only copy script from SQLite to PostgreSQL.
- **Reproducibility:** `constraints.txt` pins the versions tested together.
- **Secrets:** read only from `.env` through `config.py`; `.env` is gitignored; the dashboard starts with no paid keys; passwords and `DATABASE_URL` are never logged.
- **Reviewable by an AI assistant:** [`CLAUDE.md`](CLAUDE.md) records the architecture rules and the lessons from live runs.

## Cost

Observed in short tests; real cost depends on how many new articles arrive, your Apify plan and current vendor prices:

| Run | Observed cost |
|---|---|
| Collection cycle with no new articles (the Apify run only) | about $0.0002 |
| Collection cycle that tagged about 40 new articles | about $0.04 |
| Brief plus evaluation | about $0.07 |
| One "ask the desk" question | about $0.006 |

There is **no hard spending cap**: `DAILY_COST_BUDGET_USD` only alerts after a day has exceeded it. Set limits with your API providers if you need a ceiling. The dashboard's cost panel shows the actual figures per run.

## Try it

**Without any keys:** watch the walkthrough video, or open the live demo [TODO: link]. Demo mode serves a frozen, sanitized snapshot of the data (`demo/demo_store.db`) read-only, so it needs no keys, makes no API calls, runs no scheduler and spends no credits. To run it locally:

```powershell
pip install -r demo_app/requirements.txt     # the small dependency set; the full list is requirements.txt
$env:DEMO_MODE = "true"; streamlit run app.py
```

(`streamlit run demo_app/streamlit_app.py` does the same without setting the variable; that is the entry point for Streamlit Community Cloud.)

**Run it yourself (you need your own keys and pay for your own usage):**

```powershell
git clone https://github.com/SergeGit74/Oil-Gas-Dashboard.git
cd Oil-Gas-Dashboard
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env          # fill in your keys; values are unquoted
python -c "import storage"      # creates and migrates the database
python -m jobs.scheduler        # terminal 1: collection, prices, brief
streamlit run app.py            # terminal 2: dashboard at http://localhost:8501
```

macOS and Linux: use `cp` and `source venv/bin/activate`. Required keys: Anthropic, Voyage AI, EIA (free), Serper and Apify. Optional: Twelve Data, Slack webhook, SMTP. The full settings table is in [`docs/REFERENCE.md`](docs/REFERENCE.md).

**Docker** (`docker compose up -d --build`) is provided but has **never been built or run**, so treat it as untested.

Two cautions: run only one scheduler per data folder (two double every cycle's cost), and keep the dashboard on `127.0.0.1` unless you put real authentication in front of it, because "ask the desk" spends API credit.

## Known limitations

- Working draft. Over longer runs, expect further integration issues.
- **Price data:** EIA spot prices lag the calendar by about nine days, so the price check compares current news with an older move. Yahoo is an unofficial endpoint. Twelve Data was not tested (no key). Brent and TTF use proxy exchange calendars, so their open/closed labels are indicative.
- **LLM judgments:** tagging, sentiment and the brief evaluator are model-based and have not been validated against analyst judgment. Thresholds for clustering and corroboration were tuned on small samples.
- **Evidence depth:** GDELT contributes titles only, and the archive ("ask the desk" and brief context) embeds titles and short snippets, so answers cannot go beyond headlines.
- **Delivery:** no real Slack message or email has been sent from this repo.
- **Security:** the dashboard password is a shared secret for local use, not real authentication.
- **Scale:** SQLite and a local vector store suit one machine; a hosted PostgreSQL is untested and `pgvector` is not built.
- **Data terms:** each source has its own terms. Check them before reusing any data.

## Repository map

```
app.py, pages/            Streamlit dashboard (read-only) and the Research page
jobs/                     scheduler, collection, prices, ingestion, evaluation, email, health and budget checks
llm_pipeline.py           trust tiers, commentary rule, injection flags, tagging, brief generation
brief_evaluation.py       claim decomposition, entailment, numeric and coverage checks
market_data.py            price providers, quota manager, polling plan
storage.py, db.py,        one SQL layer for SQLite and PostgreSQL, versioned migrations
migrations.py
cost_tracker.py, ...      per-run cost accounting
tests/                    offline pytest suite
docs/REFERENCE.md         full design notes, settings and known gaps
```

## Data sources and terms

EIA (free API), GDELT (free), headlines gathered by the Apify actor `topsail/compliant-commodity-intel`, search results from Serper, models from Anthropic, embeddings from Voyage AI, and quotes from Twelve Data or Yahoo Finance if enabled. You must have your own access to each service and comply with its terms. No keys, accounts or data are provided with this project.

## How this was built

[TODO: confirm the wording] Built with AI-assisted development (Claude Code). Design decisions, review and testing are mine.

## License

Personal use only; not licensed for redistribution or commercial use. See [`LICENSE`](LICENSE). The project is provided as is, without a support commitment.

## Author

[TODO: your name, and a link to LinkedIn or your portfolio]
