# StockTrackerApp — Workflows & Architecture

This document maps how the app actually works end-to-end: every major user-facing workflow, which routes and modules implement it, which tables it reads/writes, and how the pieces connect (or don't). It's a companion to `CLAUDE.md` (which is terse operational guidance) — this one is the deep, holistic picture, written after tracing every route in `app.py` against its backing module and template.

The app is a single-user-oriented (currently capped at 50 accounts) Flask monolith for tracking and researching NSE/BSE equities: a rules-based "ATH breakout" trading system, a 4-pillar fundamental scoring model, sector/market analytics, portfolio and watchlist tracking, a PMS fund-review tool, and price alerts. There is no external market-data service — every price, history, fundamental, and news item is fetched live from `yfinance` at request time (with several ad-hoc SQLite caching layers to avoid hammering it).

## 1. The big picture

Two independent "engines" sit at the heart of the app and rarely talk to each other directly — they're stitched together by the user manually moving data between tables via the UI:

```mermaid
flowchart LR
    subgraph Universe["Curated ticker universe"]
        PT[("profit_tracker<br/>(ATH-profit Y/N, FNO/TM, result date)")]
    end

    PT --> SCAN["ATH Scanner<br/>scanner_engine.py (live)<br/>ath_scanner.py (legacy, still wired)"]
    SCAN --> ATT[("ath_tracking_table<br/>ath_scanning_results")]
    ATT --> REVIEW{{"User reviews scan hits<br/>on /ath-scanner"}}
    REVIEW -->|"EOD Promote<br/>(today_ath → previous_ath)"| ATT
    REVIEW -->|"Bulk Add to Tracker"| STOCKS[("stocks<br/>(today's daily tracker)")]
    PT -.joined by symbol.-> STOCKS

    STOCKS --> TRIGGER["get_investment_category()<br/>+ GO/WAIT rule<br/>(duplicated in app.py & daily_tasks.py)"]
    TRIGGER --> DASH["/dashboard, /go-list"]
    TRIGGER --> ARCHIVE["Daily Archive<br/>daily_tasks.perform_archive_for_user<br/>(cron via run_pa.sh, or /manual-archive)"]
    ARCHIVE --> HIST[("historical_log")]
    ARCHIVE -->|"remark ⇒ WAIT reasons"| WATCH[("watchlist")]

    subgraph Scoring["Fundamental scoring"]
        FA["/fundamental-analysis<br/>fundamental_analysis.py + ScoringEngine"]
    end
    FA --> SCOREHIST[("scoring_history")]
    SCOREHIST --> TURTLE["Turtle Dashboard<br/>market_stats.py<br/>(populated by /run_market_scan)"]
    SCOREHIST -.only if ath_tracker.db is<br/>separately seeded.-> SECTORDB[("ath_tracker.db<br/>sector_scores etc.")]
    SECTORDB --> SECTORS["/sectors pages<br/>(read-only via SectorAnalytics)"]
```

Everything else (Portfolio, Watchlist, Performance, PMS Fund Review, Alerts, News, Deep Analytics) is a mostly-independent satellite that reads live prices/fundamentals through the shared `DataManager` cache and its own tables, without feeding back into the scanner/tracker pipeline above.

## 2. Data stores (three separate SQLite files — not one)

| File | Driven by | Used for |
|---|---|---|
| **`tracker.db`** (path from `DATABASE_PATH` env var, default repo root) | `app.py`, `daily_tasks.py`, `data_manager.py`, `scanner_engine.py`, `ath_scanner.py`, `scanner_status.py`, `market_stats.py`, `alerts_manager.py` | Everything user/session-facing: auth, daily tracker, profit tracker, portfolios/holdings, watchlist, history, alerts, fund snapshots, scoring history, scanner staging/state tables, DataManager's own price/fundamental/history caches |
| **`ath_tracker.db`** (hardcoded relative path in `sector_analytics.py`) | `SectorAnalytics` class, `setup_sector_analytics.py` | `sector_scores`, `sector_risk_metrics`, `sector_alerts`, `sector_company_contributions`. **`app.py`'s `/sectors*` routes only ever read from this via `SectorAnalytics.get_sector_ranking()`/queries — they never call the class's `compute_sector_scores()`/`compute_risk_metrics()` write methods.** Those are only invoked by the standalone `setup_sector_analytics.py` script, run manually/offline. If nobody has run that script against `ath_tracker.db`, the Sector Analytics pages render empty. |
| **`widget_cache.db`** (hardcoded relative path in `app.py`) | Dashboard performer widget only | `performer_cache` table (best/worst performer per user) — a bespoke cache that bypasses `DataManager` entirely and talks to `yfinance` directly |

Within `tracker.db`, table creation is itself scattered across `init_base_tables.py`, `init_alerts_db.py`, `init_sector_db.py` (writes to `ath_tracker.db`, not `tracker.db`, despite the name), `app.py:create_tables()`, ad-hoc `CREATE TABLE IF NOT EXISTS` in `data_manager.py`/`scanner_engine.py`, and one-off `migrate_*.py` scripts for column additions. There is no single schema file — see `CLAUDE.md` for the full table inventory.

## 3. Navigation map

The nav bar (`templates/base.html`) groups every workflow below into three menus plus auth:

- **Research**: Turtle Dashboard, Sector Analytics, Fundamental Analysis, Deep Analytics (Sector), News Feed, Results Calendar
- **Portfolio**: Portfolio, Watchlist, Profit Manager, Trade History, Performance, PMS Review
- **Trading Tools**: GO List (Breakouts), ATH Scanner, Price Alerts

`/` itself is not a page — it just redirects to `/login`. The home page after login is `/dashboard`.

## 4. Workflows in depth

### 4.1 Auth
`/signup` (hard-capped at 50 users) hashes passwords with `werkzeug.security.generate_password_hash`; `/login` verifies and calls `login_user`; `/logout` calls `logout_user`. Session cookie is signed with a **hardcoded** `app.secret_key` (`'ATH_SCANNER_FIXED_SECRET_KEY_V1'`) — intentional, per its comment, so users aren't logged out when the dev server reloads.

### 4.2 Daily Tracker & GO List (`stocks` table)
The operational core. `/dashboard` LEFT JOINs `stocks` with `profit_tracker` (by symbol+user) and batch-fetches live prices via `get_live_prices_cache()` → `DataManager` (5-min cache). Adding a stock (`/add`, `/add_stock_ajax`) validates the ticker, upserts a `profit_tracker` row if missing, then inserts into `stocks` (rounding, stop_loss, ath_outperformance, is_green_candle, is_close_above_ath — these three flags are entered by hand, not computed by the app itself here).

Every row is classified by the same rule, computed inline wherever it's needed (dashboard, GO List, archive) — **not** a shared/importable function, and duplicated verbatim between `app.py` and `daily_tasks.py`:

```
category, remark = get_investment_category(ath_outperformance, ath_profit, idx_type)
  ath_outperformance != 'Y'                          → NO ENTRY ("ATH OP is 'N'")
  ath_outperformance == 'Y' and ath_profit != 'Y'     → NO ENTRY ("Index is not 'FNO'" — misleading text)
  ath_outperformance == 'Y' and ath_profit == 'Y':
      idx_type == 'FNO'                               → FUND
      else                                             → PROP

is_tracked = category in (FUND, PROP)
price_gt_rounding = cmp > rounding
final_trigger = "GO" if is_tracked and is_green_candle=='Y'
                        and price_gt_rounding and is_close_above_ath=='Y'
                else "WAIT"
```

`/go-list` re-runs this filter (recomputed inline again, a third copy of the same conditional) to show only the `GO` subset, fetching live prices one-by-one rather than batched. Deleting from the daily tracker (`/delete`, `/delete-all`, `/delete-selected`) never touches `profit_tracker` — the cascade only goes the other direction (see 4.3).

### 4.3 Profit Manager & Results Calendar (`profit_tracker` table)
`/profit-manager` lists/searches the permanent per-symbol master record (`ath_profit` Y/N, `idx_type` FNO/TM, `result_date`). Toggle routes flip `ath_profit`/`idx_type` in place. Deleting a symbol here (`/delete-profit-record`, `/delete-profit-entries`) **cascades** into `stocks`, `watchlist`, and (for the bulk route) `historical_log` — the asymmetric opposite of 4.2.

Bulk edits use a two-step preview→confirm pattern backed by the `upload_previews` staging table: `/upload-ath-profit-preview` and `/upload-result-date-preview` validate an uploaded spreadsheet, diff it against existing rows, and stash the plan as JSON; `/confirm-ath-profit-upload` / `/confirm-result-date-upload` apply it. A legacy one-shot `/upload-profits` route also exists and upserts directly with no preview step.

`/results-calendar` filters `profit_tracker.result_date`, surfacing `'CONFLICT'` sentinel values separately. `/refresh-result-dates` doesn't scrape locally — it calls the PythonAnywhere Consoles API to kick off `run_pa.sh`, which runs `daily_tasks.update_all_result_dates()` (screener.in scraping via BeautifulSoup) on the production host. This only works when deployed on PythonAnywhere with the credentials that script expects.

### 4.4 Trade History / Daily Archive (`historical_log` table)
`daily_tasks.archive_all_users()` (invoked by cron via `run_pa.sh`, or on-demand per-user via `/manual-archive`) computes the correct trading-day log date (weekends roll back to Friday), fetches true EOD closes from `yfinance`, and re-runs the same category/GO-WAIT logic using each stock's *stored* flags (not live-recomputed ones) to write one `historical_log` row per tracked stock. It's idempotent (deletes that user+date's rows before reinserting) but **does not clear `stocks`** — the daily tracker persists across archive runs; it's not date-scoped.

As a side effect, archiving also maintains `watchlist`: a `GO` result removes any existing watchlist entry for that symbol; specific WAIT remarks (`Rounding > CMP`, `ATH Profit is 'N'`, `ATH OP is 'N'`) add one. `/history`, `/history/<date>`, `/history/search` browse the log; `/upload-history` **destructively replaces** the entire log from a backup Excel file (not a merge).

### 4.5 ATH Breakout Scanner
The most complex subsystem, and it has **two live implementations wired into the same page** (`/ath-scanner`, `templates/ath_scanner.html`):

- **`scanner_engine.py`** (current/primary, imported at `app.py:2970`) — a 3-phase batch design:
  1. **Sync**: any `profit_tracker` symbol missing from `ath_tracking_table` gets a full `yf.download(period="max")` to seed its lifetime-high baseline (`previous_ath`).
  2. **Fast batch detect**: 50-ticker batches, 5-day history, flags "potential hits" where today's high ≥ stored `previous_ath`.
  3. **In-depth analysis** (only for hits): Green Candle (`close ≥ prev_close`), Close > ATH (`close > previous_ath`), and RS Outperformance — a 211-day "rolling fixed-anchor RS" against Nifty 500 (`^CRSLDX`, falling back to `^NSEI`): anchor the stock/index close ratio at the start of the window, and flag `Y` if today's anchored ratio is within 0.01% of the window's max.
  Results land in `ath_scanning_results` (wiped/rebuilt each run) and `ath_tracking_table.today_ath`.
- **`ath_scanner.py`** (`ATHScanner` class, still imported and instantiated at `app.py:2988`/`3013`, driving `/api/ath/run-daily` and `/api/ath/run-refresh`) — an older single-strategy, non-batched ATH-break detector, explicitly labeled `LEGACY SCANNER` in the template JS/button IDs but not removed.

Both are backed by `scanner_status.py`'s `ScannerStatusManager`, which persists scan progress to the `scanner_state` table (keyed by `user_id`) specifically because gunicorn runs multiple worker processes — an in-memory progress dict can't be polled cross-process, so the frontend's `setInterval` polling (`/api/scanner/status`, aliased to `/api/ath/status`) reads the DB instead.

**EOD Promote** (`/api/eod-promote` → `promote_ath_eod`) is the manual, opt-in step that turns a confirmed intraday high into the new permanent baseline: copies `today_ath → previous_ath` for selected symbols, then clears `today_ath` globally. **Bulk Add to Tracker** (`/api/bulk-add-portfolio` — despite the name, this writes to `stocks`, not `portfolios`/`holdings`) is how a scan result becomes a row in the daily tracker (4.2), closing the loop back to `profit_tracker`'s universe.

### 4.6 Portfolio & Watchlist
`/portfolio` lists the user's `portfolios`, each with `holdings` (symbol, exit_price, allocation%, `rg_status`). Per holding it computes live `cmp`/`change_pct` (batched via `DataManager`), `from_exit = (exit_price/cmp - 1) * 100`, and an allocation-weighted daily `contribution`, plus a pie-chart payload (with a synthetic "Cash" slice if allocations don't sum to 100). `rg_status` is a free-form `'R'`/`'G'` dropdown tag with **no semantic meaning anywhere in the code** — just a manually-set label, not derived from any calculation.

`/refresh_portfolio_data/<id>` is a separate AJAX path that bypasses `DataManager` entirely and hits `yfinance` directly for a fresher read. Upload/download (`/upload-portfolio`, `/download-portfolio`, and similarly for `/upload-watchlist`/`/download-watchlist`) are **full destructive replace** operations — uploading wipes and re-inserts, not merges. `/api/bulk-add-portfolio`, despite its name, belongs to the scanner workflow (4.5), not this one.

### 4.7 Performance
`/performance` is a read-only rollup: per portfolio, `total_value = Σ(allocation% × cmp)` and `total_change` = a simple (not allocation-weighted) mean of holdings' daily change%, using the same 5-min `DataManager` cache as the dashboard. Holdings with no resolvable price are silently excluded from the aggregate.

### 4.8 Fundamental Analysis, Scoring & Checklist
`/fundamental-analysis` is a wizard: enter a symbol, optionally override any of 21 weight sliders (4 pillars + 17 sub-factors), and `get_stock_fundamentals()` (`fundamental_analysis.py`) scores it — Growth/Profitability(Quality)/Valuation/Technicals at default weights 30/25/20/25, with BFSI-specific metric swaps (ROE↔ROCE etc.), sector-relative percentiles via `sector_manager.SectorManager`, and technicals (RSI/CAGR/drawdown) via its own `get_technical_analysis()`. This produces a 0–100 `health_score` and a grade (Strong Buy ≥80, Buy ≥60, Hold ≥40, else Sell), logged once per symbol per day into `scoring_history` via `log_scoring_run()`.

**Note**: this is a *second, independent* implementation of the same "4-pillar" idea — `scoring_engine.ScoringEngine` implements a stricter, hard-bucketed version of the identical 30/25/20/25 model, but `fundamental_analysis.py` does not call it; the two coexist rather than sharing logic.

The **checklist** (`fundamental_checklist` table: `analysis_status`, `fund_allocation`, `breakout_status`) is a separate manual tracking log surfaced as a tab on `/portfolio` — it doesn't trigger or read scoring itself.

### 4.9 Turtle Dashboard
`/dashboard/turtle` reads pre-computed market breadth (`market_stats_history`) and top-50 ranked stocks (`stock_analytics_snapshot`) via `MarketStats.get_dashboard_data()`. That data only exists after someone manually triggers `/run_market_scan`, which runs `MarketStats.calculate_daily_stats()`: iterates every distinct symbol in `stocks`, pulls 1y history, computes RS return/ATH proximity/200DMA breadth/trend state, joins in the latest `scoring_history` score, and upserts both tables — including a market "regime" label (BULLISH/BEARISH/NEUTRAL) from 200DMA breadth and ATH proximity thresholds. `/turtle-research` is a bare redirect to this same page (legacy alias).

### 4.10 Sector Analytics
`/sectors`, `/sectors/<name>`, `/sectors/export/csv`, `/api/sector-scatter-data` all **read** `sector_scores`/`sector_risk_metrics`/`sector_company_contributions` from `ath_tracker.db` via `SectorAnalytics` (see §2). The **write/compute** side (`compute_sector_scores()` — market-cap and equal-weighted sector aggregation of `scoring_history`; `compute_risk_metrics()` — beta/alpha/volatility/Sharpe/VaR against sector NSE indices like `^CNXFIN`/`^NSEBANK`) is only ever called from the standalone `setup_sector_analytics.py` script, run manually and disconnected from the live app's request cycle. The `sector_alerts` table exists in the schema (`init_sector_db.py`) but has no read/write code path anywhere — unimplemented.

### 4.11 Deep Analytics (`/analytics`)
A distinct, on-request, user-scoped alternative to 4.9/4.10: builds a universe from the user's `watchlist` + `profit_tracker` + `holdings` (capped to 20 symbols), scores each fresh via `get_stock_fundamentals()` (no DB caching of the aggregate), and aggregates sector scores in-memory via `analytics_engine.calculate_sector_scores()` — a third, independent reimplementation of "sector aggregation," parallel to but not sharing code with `SectorAnalytics.compute_sector_scores()`. Advances/declines "breadth" here is currently **hardcoded** (`{'advances': 1250, 'declines': 850}`), and `calculate_portfolio_metrics()` is an explicit stub returning fixed placeholder values.

### 4.12 PMS Fund Review
`/fund-review` renders a mostly client-side tool (`static/js/fund_review.js`) tracking two named funds' weekly relative-strength/price tables against Nifty. The backend only provides two things: `/api/fund-review/fetch-prices` (batch `yfinance` lookup to auto-fill a price cell for a given date, not persisted) and `/api/fund-review/snapshots` (CRUD on `fund_snapshots`, keyed `(user_id, fund, review_date, period)`). A "snapshot" is a full immutable JSON blob of the entire review state at that moment — there's no server-side diffing; the "Trends" tab fetches ≥2 snapshots and charts them client-side.

### 4.13 Price Alerts
`/alerts` creates rows (`symbol`, `target_price`, `condition` = `'ABOVE'`/`'BELOW'`) via `alerts_manager.create_alert()`. Critically, **alerts are only ever evaluated on page load of `/alerts` itself** — `check_alerts()` (per-symbol uncached `yfinance` lookup) is called nowhere else in the codebase: no scheduler, no `daily_tasks.py` hook, no background job. There is no notification mechanism beyond an inline banner shown if you happen to be looking at that page when a threshold is crossed.

### 4.14 News Feed
`/news` fetches three separate `yfinance` news sets in parallel (general Nifty, current dashboard `stocks` symbols, current portfolio `holdings` symbols) via `news_feed.get_market_news()` — no caching or persistence, refetched every load.

### 4.15 Dashboard widgets & caching split
The dashboard's "Best/Worst Performer" widget is deliberately split across two routes to keep page load fast: `/dashboard_performer_widget_cached` (used on every page load) just reads the last computed result from `performer_cache` in `widget_cache.db` — instant, no network calls. `/dashboard_performer_widget` (triggered only by an explicit manual refresh button) does the actual N direct `yfinance` calls and writes the new winner/loser back into that cache. A separate, older `/dashboard_widgets_data` JSON endpoint also exists but isn't wired into the current template's JS.

## 5. Cross-cutting: the caching layer

`data_manager.DataManager` (`data_manager.py`) is the shared price/fundamentals/history cache most workflows go through — `stock_price_cache`, `stock_fundamental_cache`, `stock_history_cache` in `tracker.db`, each with its own staleness window. Notable exceptions that **bypass** it and hit `yfinance` directly: the ATH scanner (its own batching needs), `/refresh_portfolio_data`, the dashboard performer widget, `alerts_manager.check_alerts`, `news_feed.py`, and the sector scatter/risk-metric routes. When touching price-fetching code, check whether the surrounding function already goes through `DataManager` before adding a new direct `yfinance` call.

## 6. Known quirks worth knowing before you change things

- `get_investment_category()` — the central GO/WAIT/FUND/PROP rule — is copy-pasted between `app.py` and `daily_tasks.py`, and the GO-list filter in `/go-list` is a third inline reimplementation of the same condition. Changing the trading rule means changing it in three places.
- Two ATH scanners are both live and reachable from the same page (`scanner_engine.py` primary, `ath_scanner.py` legacy) — confirm which one a bug report is actually about before debugging.
- Two independent 4-pillar scoring implementations exist (`scoring_engine.ScoringEngine`'s strict-bucket version vs. `fundamental_analysis.py`'s proportional-points version); only the latter is wired into `/fundamental-analysis` and `scoring_history`.
- Sector aggregation is implemented three separate times with three different persistence stories: `SectorAnalytics.compute_sector_scores()` (writes `ath_tracker.db`, only run via the offline `setup_sector_analytics.py` script), `analytics_engine.calculate_sector_scores()` (in-memory, per-`/analytics`-request, user-scoped), and the portfolio-only `analytics_engine.get_portfolio_allocation()`.
- `/sectors*` pages will appear empty on a fresh checkout until someone manually runs `setup_sector_analytics.py` against `ath_tracker.db` — the live app never populates that database itself.
- Deleting a symbol from **Profit Manager** cascades to `stocks`/`watchlist`/`historical_log`; deleting from the **Daily Tracker** does not cascade back to `profit_tracker`. The cascade only goes one direction.
- Portfolio/watchlist upload endpoints **replace**, not merge — re-uploading a partial spreadsheet silently deletes anything not in the file.
- `rg_status` on holdings is a bare `'R'`/`'G'` tag with no defined meaning anywhere in code, comments, or its migration script — it's manual user classification only.
- Price alerts have no scheduler; they're evaluated only when the `/alerts` page happens to be open.
- `app.secret_key` is a fixed literal string (intentional — avoids logging users out on dev-server reload — but means session cookies are forgeable if the key leaks).
- `/api/ath/status` is registered by two different route declarations (`app.py:3000` and `app.py:3328`, the latter stacked with `/api/scanner/status`); both call the identical `status_manager.get_status()`, so it's inert duplication rather than a functional bug, but only one handler is ever reachable for that exact path.
- `analytics_engine.calculate_portfolio_metrics()` is an explicit placeholder stub (fixed beta/alpha/volatility), and its "breadth" (advances/declines) numbers on `/analytics` are hardcoded, not computed.
