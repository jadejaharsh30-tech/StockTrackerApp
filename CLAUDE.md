# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A personal Flask web app for tracking an Indian equity (NSE/BSE) portfolio: ATH (all-time-high) breakout scanning, a "Turtle" 4-pillar fundamental/technical scoring model, sector analytics, a PMS fund-review dashboard, alerts, news, and daily archiving. All prices/fundamentals/news come from `yfinance` live — there is no separate market-data ingestion service.

## Running the app

```bash
pip install -r requirements.txt
python init_base_tables.py      # create/ensure core tables (users, stock_data, scoring_history, scanner_state, fund_snapshots)
python init_alerts_db.py        # create alerts table
python init_sector_db.py        # create sector_scores / sector_risk_metrics / sector_alerts tables
python app.py                   # runs Flask dev server on :5000 (debug=True, use_reloader=False)
```

`app.py` also calls `create_tables()` on startup, which creates the remaining tables (`stocks`, `profit_tracker`, `historical_log`, `watchlist`, `portfolios`, `holdings`, `fundamental_checklist`, `upload_previews`, `scoring_history`, `market_stats_history`, `stock_analytics_snapshot`) if missing — so a fresh `tracker.db` is bootstrapped just by running `app.py`.

In production (PythonAnywhere / gunicorn per the `Procfile`): `gunicorn app:app`.

Daily maintenance job (result-date refresh, EOD archiving) is `daily_tasks.py`, invoked via `run_pa.sh` — a cron-style helper with a hardcoded PythonAnywhere venv path, not meant to run as-is in other environments.

There is no lint or test-suite command configured (no pytest/unittest in the repo). `test_logic_live.py` is a standalone manual script that hits live `yfinance` data to sanity-check ATH/RS logic — run it directly with `python test_logic_live.py`, not via a test runner.

## Database

SQLite, default file `tracker.db` in the repo root, overridable via the `DATABASE_PATH` env var (used consistently across `app.py`, `daily_tasks.py`, `scanner_engine.py`, `ath_scanner.py`, `data_manager.py`, `scanner_status.py`, `market_stats.py`). All connections open with `PRAGMA journal_mode=WAL` to allow concurrent reads/writes. There is no ORM — every module runs raw SQL via `sqlite3` (`conn.row_factory = sqlite3.Row`).

Because table creation is scattered (`init_base_tables.py`, `init_alerts_db.py`, `init_sector_db.py`, `app.py:create_tables()`, ad-hoc `CREATE TABLE IF NOT EXISTS` inside `data_manager.py`/`scanner_engine.py`, plus one-off `migrate_*.py` scripts for ALTER TABLE changes), when adding a new persisted field prefer following the existing pattern for that table's owning module rather than centralizing — there is no single schema file. Key tables: `users`, `portfolios`/`holdings` (portfolio module), `profit_tracker`/`stocks`/`historical_log` (daily ATH tracker + archive), `watchlist`, `fundamental_checklist`, `scoring_history`, `sector_scores`/`sector_risk_metrics`/`sector_alerts`, `alerts`, `fund_snapshots` (PMS review), `ath_scanning_results`/`scanner_state` (scanner engine), `stock_price_cache`/`stock_fundamental_cache`/`stock_history_cache` (DataManager caching layer).

## Architecture

`app.py` is a single large monolithic Flask app (~90 routes, no blueprints) that owns auth (Flask-Login, session cookie, `werkzeug.security` password hashing) and most view/route logic directly. Business logic that's reused or non-trivial is factored into top-level modules that `app.py` imports functions from:

- **`data_manager.py`** (`DataManager`) — central live-price/fundamentals fetch + SQLite caching layer (`stock_price_cache`, `stock_fundamental_cache`, `stock_history_cache`) wrapping `yfinance` batch calls. Most other modules that need a price go through this instead of calling `yfinance` directly.
- **`scanner_engine.py`** / **`ath_scanner.py`** — the ATH breakout scanner. `scanner_engine.py` is the current 3-phase implementation (sync tickers from `profit_tracker` → batch-detect ATH hits → in-depth RS-outperformance/green-candle analysis), driven from `app.py`'s `/api/ath/*` and `/api/run-scanner` routes. `ath_scanner.py` (`ATHScanner` class) is an alternate/earlier scanner implementation — check which one a route actually calls before assuming both are live.
- **`scanner_status.py`** (`ScannerStatusManager`) — persists scan progress to the `scanner_state` table so long-running scans survive across gunicorn worker processes (progress can't live in memory).
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
- Benchmark for relative-strength/outperformance calculations is Nifty 500 (`^CRSLDX`, falling back to `^NSEI`).
- `app.secret_key` is a fixed hardcoded string (to avoid logging users out on reload) — this is a known/intentional single-user-app tradeoff, not an oversight to silently "fix".
