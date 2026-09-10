# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A personal Flask web app for tracking an Indian equity (NSE/BSE) portfolio: ATH (all-time-high) breakout scanning, a "Turtle" 4-pillar fundamental/technical scoring model, sector analytics, a PMS fund-review dashboard, alerts, news, and daily archiving. All prices/fundamentals/news come from `yfinance` live — there is no separate market-data ingestion service.

For a deep, route-by-route trace of every feature workflow (which routes/modules/tables implement it, the exact GO/WAIT and RS-outperformance formulas, the three-way SQLite database split, and known duplicated/dead logic paths), see **[`WORKFLOWS.md`](./WORKFLOWS.md)**. This file stays intentionally terse; that one has the holistic picture.

## Running the app

```bash
pip install -r requirements.txt
python init_base_tables.py      # create/ensure core tables (users, stock_data, scoring_history, scanner_state, fund_snapshots)
python init_alerts_db.py        # create alerts table
python init_sector_db.py        # create sector_scores / sector_risk_metrics / sector_alerts tables
python init_profit_history.py   # create profit_history table (ATH profit scan)
#                                 (init_base_tables also creates scan_runs, the scan log)
python app.py                   # runs Flask dev server on :5000 (debug=True, use_reloader=False)
```

Load reported profit history for the Dual/Growth profit scan. The primary path is **`profit_feed.py`**, which fetches the two Google Apps Script endpoints directly (48 quarters and 15 years for ~5500 companies) and writes `profit_history` — no `financial_data.duckdb` round-trip, and no dependency on the colleague's `fetch_classifications` module, which this app never reads:

```bash
python profit_feed.py                        # refresh profit_history from the APIs
python profit_feed.py --preview              # refresh, then report flag changes
python profit_feed.py --apply                # refresh, then write the flags
python import_profit_duckdb.py <file>.duckdb # alternative: load from a duckdb file
python import_profit_history.py <file>.xlsx --type Q   # fallback: CSV/Excel
```

Endpoint URLs default to the current deployments and are overridable via `PROFIT_API_QUARTERLY` / `PROFIT_API_YEARLY` (Apps Script URLs change on every redeploy). `import_profit_duckdb.py` shares `profit_feed`'s reverse/trim/label transform, so the two paths cannot drift.

The same refresh is on the scanner page as **Refresh Profit Data**, which then previews what it would change in `profit_tracker.ath_profit` and lets you apply or cancel. **It shares `scanner_state` with the scan, so the two can never overlap** — a scan reading `profit_history` mid-rewrite would judge some stocks on old data and some on new, silently. Three non-obvious things it handles, all verified against the real feed — do not "simplify" them away:
- **`QL1`/`FYL1` are the NEWEST periods**, not the oldest (confirmed: `TTM == QL1+QL2+QL3+QL4` for 97.9% of full-history companies). The series is reversed on import, and the importer re-runs that check each time and warns if the feed's ordering ever flips.
- **Oldest-end zeros are pre-listing padding**, not reported profits, and are trimmed. Left in, a rolling TTM straddling the boundary mixes real quarters with fake zeros, and for a loss-making company `0` becomes the all-time peak.
- **`period_end` is a positional sequence** (`Q001` oldest … `Q048` newest), because the feed carries no dates and its columns shift every quarter. Each symbol's series is replaced wholesale on import. Don't mix these with date-labelled rows for the same symbol.

`app.py` also calls `create_tables()` on startup, which creates the remaining tables (`stocks`, `profit_tracker`, `historical_log`, `watchlist`, `portfolios`, `holdings`, `fundamental_checklist`, `upload_previews`, `scoring_history`, `market_stats_history`, `stock_analytics_snapshot`) if missing — so a fresh `tracker.db` is bootstrapped just by running `app.py`.

In production (PythonAnywhere / gunicorn per the `Procfile`): `gunicorn app:app`.

Daily maintenance job (result-date refresh, EOD archiving) is `daily_tasks.py`, invoked via `run_pa.sh` — a cron-style helper with a hardcoded PythonAnywhere venv path, not meant to run as-is in other environments.

There is no lint or test-suite command configured (no pytest/unittest in the repo). `test_logic_live.py` is a standalone manual script that hits live `yfinance` data to sanity-check ATH/RS logic — run it directly with `python test_logic_live.py`, not via a test runner.

## Database

SQLite, default file `tracker.db` in the repo root. `DATABASE_PATH` is honoured by only **four** modules — `app.py`, `daily_tasks.py`, `ath_scanner.py`, `scanner_status.py`. `scanner_engine.py`, `market_stats.py`, `alerts_manager.py`, and `fundamental_analysis.py` hardcode a path next to their own file, and `data_manager.py`/`sector_analytics.py` take `db_path` from the caller. **Setting `DATABASE_PATH` therefore splits the app across two databases** rather than relocating it — treat it as effectively unsupported unless you fix the stragglers first. All connections open with `PRAGMA journal_mode=WAL` to allow concurrent reads/writes. There is no ORM — every module runs raw SQL via `sqlite3` (`conn.row_factory = sqlite3.Row`).

Because table creation is scattered (`init_base_tables.py`, `init_alerts_db.py`, `init_sector_db.py`, `app.py:create_tables()`, ad-hoc `CREATE TABLE IF NOT EXISTS` inside `data_manager.py`/`scanner_engine.py`, plus one-off `migrate_*.py` scripts for ALTER TABLE changes), when adding a new persisted field prefer following the existing pattern for that table's owning module rather than centralizing — there is no single schema file. Key tables: `users`, `portfolios`/`holdings` (portfolio module), `profit_tracker`/`stocks`/`historical_log` (daily ATH tracker + archive), `watchlist`, `fundamental_checklist`, `scoring_history`, `sector_scores`/`sector_risk_metrics`/`sector_alerts`, `alerts`, `fund_snapshots` (PMS review), `ath_scanning_results`/`ath_scanning_results_custom`/`scanner_state`/`scan_runs` (scanner engine), `stock_price_cache`/`stock_fundamental_cache`/`stock_history_cache` (DataManager caching layer).

The two scanner result tables are deliberate: the tracked-universe scan (`scope='tracked'`) and the custom-universe scan (`scope='custom'`) each own one, so neither run destroys the other's output. `RESULTS_TABLES`/`_results_table()` in `scanner_engine.py` map scope → table and fall back to `tracked` for anything unrecognised. Anything that reads or writes results takes `scope=` — including `/api/profit/apply-flag`, so applying a flag from the custom tab uses the custom scan's verdicts. `scan_runs.universe` doubles as the scope key, which is why the split needed no migration.

## Architecture

`app.py` is a single large monolithic Flask app (~90 routes, no blueprints) that owns auth (Flask-Login, session cookie, `werkzeug.security` password hashing) and most view/route logic directly. Business logic that's reused or non-trivial is factored into top-level modules that `app.py` imports functions from:

- **`data_manager.py`** (`DataManager`) — central live-price/fundamentals fetch + SQLite caching layer (`stock_price_cache`, `stock_fundamental_cache`, `stock_history_cache`) wrapping `yfinance` batch calls. Most other modules that need a price go through this instead of calling `yfinance` directly.
- **`scanner_engine.py`** / **`ath_scanner.py`** — the ATH breakout scanner. `scanner_engine.py` is the current 3-phase implementation (sync tickers from `profit_tracker` → batch-detect ATH hits → in-depth RS-outperformance/green-candle analysis), driven from `app.py`'s `/api/ath/*` and `/api/run-scanner` routes. `ath_scanner.py` (`ATHScanner` class) is an alternate/earlier scanner implementation — check which one a route actually calls before assuming both are live.
- **`scanner_status.py`** (`ScannerStatusManager`) — persists scan progress to the `scanner_state` table so long-running scans survive across gunicorn worker processes (progress can't live in memory).
- **`profit_scanner.py`** — Phase 4 of the scan: classifies each ATH hit as **Dual** (latest quarter *and* TTM at an all-time high) or **Growth** (TTM at ATH *and* latest quarter beats the year-ago quarter). **TTM is computed once from the latest four quarters and compared against the reported financial-year series (`[TTM, FY1..FY15]`), never as a rolling 4-quarter maximum** — a rolling window spans two part-years, so a "record" inside one is an artifact of window placement, not a result the company reported. The peak FY must also be positive. There is **no minimum-history gate** — a recent listing with two reported FYs still has an all-time high over its whole existence, so it is judged like any other; depth is disclosed via `profit_points` rather than withheld as a verdict.

  **Req. Profit** is chosen *before* the scan (default Dual) and decides what the single Profit column reports as "At ATH". Only two settings are meaningful, because Dual is a strict subset of Growth (verified: 0 of 144 Dual companies fail the Growth leg) — so "either D or G" is just Growth, and "G but not D" would exclude the strongest names.

  **`profit_tracker.ath_profit` tracks Dual only.** Apply Profit Flag sets `Y` for Dual and `N` for everything else, regardless of the scan's criterion — that column gates the FUND category, which is reserved for the strict condition. Selecting Growth widens what the scan *reports*, not what qualifies for portfolio classification. Reads only the local `profit_history` table, so it costs no network calls. Populate that table with `init_profit_history.py` + `import_profit_history.py` (yfinance exposes only ~4–6 quarters, far too shallow for a real all-time high).
- **`scoring_engine.py`** (`ScoringEngine`) — the "Turtle Wealth 4-Pillar" scoring model (Growth 30% / Quality 25% / Value 20% / Technical 25%), used by the fundamental analysis and Turtle Dashboard/Research features.
- **`fundamental_analysis.py`** — per-symbol fundamentals + technicals (RSI, CAGR, std dev) built on `data_manager` and `sector_manager`.
- **`sector_manager.py`** (`SectorManager`) / **`sector_analytics.py`** (`SectorAnalytics`) — sector-level PE/PB/EV-EBITDA baselines and sector index (`^CNXFIN` etc.) scoring/risk aggregation, backing the `/sectors` pages.
- **`analytics_engine.py`** — portfolio-level aggregation: allocation by sector, portfolio metrics, ATH/outperformance checks against `^CRSLDX` (Nifty 500) benchmark.
- **`market_stats.py`** (`MarketStats`) — market-breadth stats (% above 200DMA, ATH-price/ATH-profit breadth) feeding `market_stats_history`.
- **`alerts_manager.py`** — CRUD + trigger-checking for price alerts (`alerts` table).
- **`news_feed.py`** — pulls per-symbol/general market news via `yfinance`.
- **`daily_tasks.py`** — screener.in scraping (`BeautifulSoup`) for result dates, plus the daily EOD archive job (`perform_archive_for_user`) that snapshots `stocks`/`profit_tracker` into `historical_log`.
- **`master_ath_manager.py`** — standalone/offline ATH scan against `ATH_Results.xlsx` (used by the legacy `ATH Frontend/` sub-projects, not by the main Flask app's live routes).

Templates are server-rendered Jinja2 under `templates/`, extending `base.html`; most pages use plain CSS variables (`--primary-color` etc., see `DASHBOARD_WIDGETS_README.md` for the dashboard-widget pattern and dark-mode variables) rather than a JS framework. `static/js/` and `static/css/` currently only hold assets for the `fund_review` (PMS dashboard) page — other pages inline their JS/CSS in the template.

`ATH Frontend/HTML Project/` and `ATH Frontend/Streamlit Project/` are separate, older standalone prototypes (their own `server.py` / Streamlit `app.py` reading `ATH_Results.xlsx`) — not part of the main app's request path; don't assume changes to `app.py` need to be mirrored there.

## Conventions worth knowing

- All DB helpers follow the same shape: module-level `get_db()`/`_get_conn()` opening `sqlite3.connect(..., timeout=30)` with `PRAGMA journal_mode=WAL`, `db_path` defaulting from `DATABASE_PATH` env var or an absolute path derived from `os.path.dirname(os.path.abspath(__file__))`. Match this pattern for new modules rather than hardcoding a relative `tracker.db` path (a few older scripts like `migrate_result_dates.py` still do the latter and only work when run from the repo root).
- Symbols are NSE tickers without the `.NS` suffix in the DB; suffix is appended (`f"{symbol}.NS"`) at the `yfinance` call site. BSE fallback exists in a couple of places (`master_ath_manager.get_exchange_suffix`) but the live app is NSE-first.
- **Benchmark for relative strength is Nifty 500 (`^CRSLDX`) and nothing else — there is deliberately NO fallback index.** RS is the stock/benchmark ratio, so quietly substituting Nifty 50 would move every number on the page by the ratio of the two indices while looking entirely plausible. `fetch_nifty_live()` retries `^CRSLDX` (`INDEX_FETCH_ATTEMPTS`, currently 3) and then raises; `run_full_scan` catches that, reports it on the status line and returns no results. A missing benchmark is a failed scan, not a scan against something else. Do not re-add a fallback. (`analytics_engine.calculate_outperformance` is a separate 12-month portfolio comparison that takes `^CRSLDX` as a default argument — it has no fallback either, despite what its docstring implies.)
- **RS history is fetched UNADJUSTED (`auto_adjust=False`) by deliberate choice**, even though the index series is adjusted. A stock that splits within the window therefore shows a cliff in the ratio and a false `N` on ATH O.P. (a 1:4 split puts `current_rs` at ~a quarter of `ath_rs`). This is a known, accepted limitation — it hits only the few stocks that split, and the user corrects those by hand. **Do not switch this to `auto_adjust=True` without asking**: adjusted prices also fold in dividends and would shift RS for every stock, and raw prices are what the manually maintained `previous_ath` baselines are expressed in. Every price fetch in the scanner is now unadjusted except the index.
- **RS anchoring mirrors the TradingView indicator and must stay in step with it.** The Pine source anchors at `last_bar_index - barsBackInput` with `barsBackInput = 212`, i.e. **212 bars back from the latest bar**, giving a 213-bar window. Pandas slices by row count, so `RS_BARS_BACK = 212` and `LOOKBACK = RS_BARS_BACK + 1 = 213`. Setting the row count directly is how this silently drifted two bars (and a few percent on every number) away from the indicator. `test_rs_matches_pine.py` transcribes the Pine formula and asserts the two agree — run it after touching anything in the RS path. Keep `RS_FULL_WINDOW` in `templates/ath_scanner.html` equal to `LOOKBACK`; it drives the short-history `*`.
- A recent listing with fewer aligned sessions uses the whole series instead, down to `MIN_RS_SESSIONS` (20). The window actually used is stored as `rs_window` and marked with `*` in the UI. **The TradingView indicator has no equivalent** — it simply plots nothing before its anchor bar — so starred rows are ours alone and will never match a chart.
- Two known, deliberate departures from the Pine source, both affecting verdicts rather than RS values: green candle is `close >= prev_close` here but `close > close[1]` in Pine (differs only on an exactly unchanged close), and the RS verdict allows 0.01% slack (`>= max * 0.9999`) where Pine compares exactly. Pine also forward-fills the comparison symbol onto the stock's bars, whereas `calculate_rs_outperformance` inner-joins and drops sessions the two don't share.
- **If the hosted app disagrees with a local run, check the deployed commit before the maths.** PythonAnywhere deploys from `main`; development happens on the `claude/init-*` branch. A branch commit that has not been merged means the two are running different code, and the difference can be subtle — an `auto_adjust` flag one commit behind changed only the RS *numbers* while leaving prices and every Y/N verdict identical (WORKFLOWS.md §7.16). Relatedly, `requirements.txt` pins nothing, so two environments installing "latest" `yfinance` on different days can drift on their own; `pip show yfinance` on both is the second thing to check.
- `app.secret_key` is a fixed hardcoded string (to avoid logging users out on reload) — this is a known/intentional single-user-app tradeoff, not an oversight to silently "fix".
