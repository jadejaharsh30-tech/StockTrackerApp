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
  ath_outperformance != 'Y'                                   → NO ENTRY ("ATH OP is 'N'")
  ath_outperformance == 'Y' and ath_profit == 'Y'             → FUND    (idx_type irrelevant)
  ath_outperformance == 'Y' and ath_profit != 'Y' and FNO     → PROP
  ath_outperformance == 'Y' and ath_profit != 'Y' and not FNO → NO ENTRY ("Index is not 'FNO'")

ATH outperformance is the hard gate — nothing enters without it. ath_profit == 'Y'
promotes to FUND on its own; FNO-only names fall back to PROP.

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
- **`ath_scanner.py`** (`ATHScanner` class, still imported and instantiated at `app.py:2988`/`3013`, driving `/api/ath/run-daily` and `/api/ath/run-refresh`) — an older single-strategy, non-batched ATH-break detector, explicitly labeled `LEGACY SCANNER` in the template JS/button IDs. **It is wired to live buttons but non-functional**: it reads and writes an `ath_price` column that does not exist in the current `ath_tracking_table` schema (the column is `previous_ath`), so both its scan methods raise `OperationalError: no such column: ath_price`. See §6 for the full defect list.

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
- Two ATH scanners are reachable from the same page, but only `scanner_engine.py` works — the `ath_scanner.py` legacy path is broken (see "Verified defects" below). Confirm which one a bug report is about before debugging.
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

## 7. Deep dive: the ATH pipeline (workflows §4.3 → §4.5 → §4.2 → §4.4)

Workflows 3, 4 and 5 are not independent features — they are four stages of one loop, and the handoffs between them are all **manual** (a button, an upload, or a cron job), never automatic.

```mermaid
stateDiagram-v2
    [*] --> Universe: user adds symbol<br/>(Profit Manager / Excel upload)
    Universe: profit_tracker<br/>ath_profit · idx_type · result_date
    Universe --> Baseline: Scan Phase 1 (sync)<br/>yf period=max, excl. today
    Baseline: ath_tracking_table.previous_ath<br/>(lifetime high — the trigger price)
    Baseline --> Hits: Scan Phase 2+3<br/>high >= previous_ath
    Hits: ath_scanning_results (staging)<br/>+ today_ath set
    Hits --> Baseline: EOD Promote (manual)<br/>today_ath → previous_ath
    Hits --> Tracker: Bulk Add (manual)<br/>/api/bulk-add-portfolio
    Tracker: stocks<br/>rounding = trigger_price
    Tracker --> Archive: cron / manual-archive<br/>EOD close + GO/WAIT
    Archive: historical_log (+ watchlist side-effects)
    Archive --> [*]
```

### 7.1 The two-column ATH state machine

`ath_tracking_table` (755 rows in the shipped DB, keyed on `symbol`, **no `user_id` — it is global, not per-user**) carries the whole scanner's memory in two columns:

| Column | Meaning |
|---|---|
| `previous_ath` | The confirmed lifetime high. **This doubles as the trigger price** — `calculate_single_ticker` receives it as `trigger_price`, and Bulk Add maps it into `stocks.rounding`. |
| `today_ath` | Scratch space for an *unconfirmed* intraday breakout. Set by `update_today_ath()` on every hit, cleared globally at the start of every scan (Phase 0) and at the end of every EOD Promote. |

The promote step is the only thing that makes a breakout permanent:

```sql
UPDATE ath_tracking_table SET previous_ath = today_ath, ath_date = <today>
WHERE symbol IN (<selected>) AND today_ath IS NOT NULL AND today_ath > previous_ath;
UPDATE ath_tracking_table SET today_ath = NULL WHERE today_ath IS NOT NULL;  -- global
```

Two consequences worth internalising:
- **Skipping EOD Promote is not neutral — it re-arms the same signal.** `previous_ath` stays where it was, so tomorrow's Phase 2 filter (`live_high >= previous_ath`) flags the identical stock again. A breakout you never promote reappears every day.
- **The global `today_ath` clear is unconditional.** Promoting 2 of 10 hits wipes `today_ath` for the other 8 as well. They aren't lost (they're still in `ath_scanning_results` until the next scan) but their intraday high is gone from the master table.

### 7.2 The Phase 2 filter is `>=`, and the zero-baseline trap

```python
if round(live_high, 2) >= round(current_ath, 2):   # scanner_engine.py:514
```

`>=` (not `>`) means a stock sitting exactly at its recorded ATH is a "hit" every single scan. More importantly, `sync_new_stocks_to_ath_tracker` inserts `previous_ath = 0.0` whenever the batch download fails or a symbol has no history (lines 214, 229, 233-234). **A symbol stuck at `previous_ath = 0.0` passes the filter unconditionally forever** — any positive price is `>= 0` — and it will be reported as an ATH hit with a `trigger_price` of `0.0` on every scan until someone fixes it via `/api/ath/update-record`. Worth a sanity query after adding a batch of new symbols:

```sql
SELECT symbol FROM ath_tracking_table WHERE previous_ath = 0 OR previous_ath IS NULL;
```

### 7.3 The RS-outperformance calculation (the one genuinely subtle formula)

"ATH Outperformance" is *not* a simple return comparison. `calculate_rs_outperformance` builds a **rolling fixed-anchor relative-strength line** over `LOOKBACK = 211` sessions:

1. Append today's live close to the fetched history, inner-join against the Nifty 500 series (`^CRSLDX`, fallback `^NSEI`) so holidays drop out.
2. `RS_Raw = Stock_Close / Index_Close` per session.
3. Take the last 211 rows; **re-anchor** to the first row of that window: `anchored = RS_Raw / RS_Raw[0] * 100`.
4. Flag `Y` if `current_anchored >= max_anchored * 0.9999`.

So the question it answers is *"is the stock's ratio-to-benchmark at its own 211-session high right now?"* — an ATH in **relative** terms, mirroring the price ATH. The `0.9999` factor is a float-equality tolerance, not a real tolerance band (≈0.01%).

Constraint to know: it needs 211 *aligned* rows but `run_full_scan` only fetches `period="1y"` (~245 sessions). Any symbol with a shorter listing history, or enough missing sessions to drop the join below 211 rows, silently returns `'N/A'` rather than `Y`/`N`. Since ATH outperformance is the hard gate in `get_investment_category`, **`N/A` behaves as a rejection downstream** (it isn't `'Y'`).

### 7.4 Progress reporting is not linear

`run_full_scan`'s progress counter reaches `total` when Phase 2 ends, then Phase 3 calls `update_status(total, total, ...)` for every symbol it analyses. The UI therefore sits at **100% for the entire in-depth analysis phase**, which is the slow part (a 1-year batch history fetch plus per-symbol RS maths). Combined with `ScannerStatusManager`'s 3-second write throttle, a long scan looks stalled at 100% while it is still working normally.

Scan state also has **no recovery path**: if the worker process dies mid-scan, `scanner_state.is_running` stays `1` and every future scan is refused with "Scanner already in progress." `ScannerStatusManager.reset_all()` exists precisely for this but is exposed by no route and no UI — recovery today means `UPDATE scanner_state SET is_running = 0;` by hand.

### 7.5 The archive's decision tree (and where rows silently vanish)

`perform_archive_for_user` (`daily_tasks.py:114`) is idempotent per user+date (it deletes that date's rows first), and re-derives GO/WAIT from **stored** flags plus a freshly fetched EOD close — it does not re-evaluate green-candle or close-above-ATH against market data:

```
for each row in stocks LEFT JOIN profit_tracker:
    eod_price = yfinance close (5d window ending log_date+1)
    if not eod_price:  ← row is SKIPPED ENTIRELY, no log entry, no warning
    category = "NO ENTRY"/"New, add to Profit Mgr."   if no profit_tracker match
             = get_investment_category(...)           otherwise
    if category in (FUND, PROP):
        if green_candle=='Y' and eod_price > rounding and close_above_ath=='Y':
            trigger = GO,  remark = ""
        elif eod_price <= rounding:
            remark = "Rounding > CMP"
    GO           → DELETE from watchlist
    remark in (…) → UPSERT into watchlist
```

Three things to note:
- **Silent data loss**: a `yfinance` miss (delisted ticker, network blip, bad symbol) means that stock gets *no* `historical_log` row for that date at all — no error, no placeholder. The archive reports success regardless.
- The watchlist upsert list is `["Rounding > CMP", "ATH Profit is 'N'", "ATH OP is 'N'"]`, but `get_investment_category` never emits the string `"ATH Profit is 'N'"` — it emits `"Index is not 'FNO'"`. So that branch is **dead**, and NO-ENTRY-due-to-`ath_profit` names never reach the watchlist.
- Archiving **does not clear `stocks`**. The daily tracker is not date-scoped; it carries forward until manually cleared, which is why re-running the archive for the same date is a delete-then-reinsert rather than an append.

`get_correct_log_date()` maps Saturday→Friday and Sunday→Friday, but has **no holiday calendar** — running the job on an Indian market holiday archives that date using the previous session's close (`get_correct_eod_price` takes the last row of a 5-day window), producing a duplicate-priced log entry for a non-trading day.

### 7.6 Verified defects in this pipeline

Each of these was confirmed against the shipped `tracker.db` schema, not inferred:

| Defect | Location | Effect |
|---|---|---|
| `ath_tracking_table` is created by **no** init script or migration | referenced in `app.py`, `scanner_engine.py`, `ath_scanner.py` | It exists only because the committed `tracker.db` already contains it. A fresh bootstrap (`init_base_tables.py` + `app.py`) produces a DB where **every scanner route fails**. |
| Legacy scanner reads/writes column `ath_price` | `ath_scanner.py:137,153-154,206-207` | Column doesn't exist (it's `previous_ath`) → `OperationalError: no such column: ath_price`. Both `/api/ath/run-daily` and `/api/ath/run-refresh` are dead. |
| `/api/ath/todays-results` selects `ath_price` | `app.py:3035` | Same missing column → endpoint always 500s. |
| `SCAN_STATUS` referenced but never defined | `app.py:3010` | `NameError` on every `/api/ath/run-refresh` call, before it even reaches the broken scanner. |
| `scanner_engine.py` hardcodes `tracker.db` | `scanner_engine.py:22` | Ignores `DATABASE_PATH`, unlike every other module (`ath_scanner.py:16` respects it). Setting `DATABASE_PATH` splits the app across two databases — routes read one, the scanner writes the other. |
| Scanner tables are global, not user-scoped | `ath_tracking_table` (no `user_id`), `save_scan_results` (`DELETE FROM ath_scanning_results` with no filter) | Two users scanning concurrently overwrite each other's results. Fine for the single-user deployment; a blocker for the 50-user signup cap the app advertises. |
| `/manual-archive` flashes a count from a `None` return | `app.py:1033`, `daily_tasks.py:114` | `perform_archive_for_user` returns nothing, so the success message always reports `None` archived. Cosmetic. |

### 7.7 Phase 4 — profit ATH classification (Dual / Growth)

Added after the four-strategy analysis. It answers: *of the stocks that hit a price ATH today, which also have profit at an all-time high?*

**Definitions** (both require the TTM/yearly gate; `D`'s quarterly condition is strictly stronger than `G`'s, so the test is ordered D → G):

| Flag | Condition |
|---|---|
| **D** (Dual) | latest quarter profit at ATH **AND** TTM/yearly profit at ATH |
| **G** (Growth) | TTM/yearly profit at ATH **AND** latest quarter > same quarter last year |
| `N` | TTM/yearly gate failed, or neither quarterly condition met |
| `N/A` | not enough profit history loaded to judge |

**Why a local table rather than yfinance.** `ticker.quarterly_financials` returns roughly 4–6 quarters and `ticker.financials` roughly 4 years. Rolling a 4-quarter TTM over ~5 points leaves 1–2 usable values, so `max()` is taken over almost nothing and "at ATH" comes out trivially true. (This is exactly the flaw in the older `analytics_engine.check_ath_profit`, which is still used by `/analytics`.) Phase 4 therefore reads a locally-loaded `profit_history` table instead:

```
profit_history(symbol, period_type 'Q'|'A', period_end, net_profit)
```

Because it is a pure DB read, Phase 4 adds no network calls and runs in milliseconds regardless of universe size.

**Sign-safety.** The ATH test is `current >= peak - abs(peak) * tolerance`, not `current >= peak * (1 - tolerance)`. For a loss-making company whose peak is negative, the multiplicative form raises the bar *above* the peak and can never be satisfied. The existing `check_ath_profit` still has this bug.

**Where the verdict goes.** `ath_scanning_results` gained `profit_ttm_ath`, `profit_qtr_ath`, `profit_yoy`, `profit_flag`, `profit_basis`, `profit_points`, `manual_ath_profit`. `init_scanning_results_table()` drops and recreates the table each run, so this schema change needed no migration.

**The scan never writes `profit_tracker.ath_profit`.** That column is the user's manual flag and feeds `get_investment_category`, the gate for the whole FUND/PROP/GO chain — silently recomputing it would change dashboard and archive output. Instead the scanner shows computed vs. manual side by side (a mismatch renders as `N → Y` in warning colour), and `POST /api/profit/apply-flag` applies the computed value only for explicitly selected symbols.

**Req. Profit is a view filter, not a universe filter.** It filters already-loaded results client-side. Filtering the *input* universe would mean fetching fundamentals for all ~754 tracked symbols on every scan instead of the ~10–40 that actually hit ATH.

### 7.8 Profit history data source

`profit_history` is fed from a colleague-maintained pipeline, not from yfinance:

```
Two Google Apps Script endpoints  (yearly + quarterly, refreshed daily)
        │  build_db.py
        ▼
financial_data.duckdb   quarterly(ACCORD CODE, NSE CODE, QL1..QL48)
                        yearly(..., TTM, FYL1..FYL15, MCAP, TRADING/LISTING STATUS)
                        classifications(SYMBOL, TAG_TYPE, TAG_VALUE)
        │  import_profit_duckdb.py
        ▼
tracker.db :: profit_history(symbol, period_type, period_end, net_profit)
```

Measured against the real feed: 5525 companies, of which 3322 carry an `NSE CODE`; the import yields 2861 quarterly and 3294 annual series, covering **739 of the 754** tracked symbols. The 15 uncovered ones are corporate actions rather than gaps — TATAMOTORS now appears as `TMCV`/`TMPV`, PEL as `PIRAMALFIN`, and so on. (Those same names are also the zero-baseline rows in `ath_tracking_table` noted in §7.2, which is consistent: a renamed ticker stops resolving in both feeds at once.)

Classification of the tracked universe on this data: **133 Dual, 175 Growth, 431 neither, 15 N/A** — and it disagrees with the hand-maintained `ath_profit` flag on 261 of 739 symbols, which is the gap the manual apply flow exists to let you review rather than silently overwrite.

`profit_points` (surfaced in the UI tooltip) is the size of the window each verdict was judged against, so a `D` resting on 9 TTM points is visibly weaker than one resting on 45. The classifier returns `N/A` below `MIN_TTM_POINTS` (8 TTM points ≈ 11 quarters); `import_profit_duckdb.py` derives its "thin history" warning from that same constant so the two cannot drift apart.

The `classifications` table (INDEX and INDUSTRY tags for 755 symbols) is not yet consumed by the app — it is a natural source for the sector mapping that `sector_manager.py` currently approximates from yfinance.

### 7.9 TTM-at-ATH: reported financial years, not rolling windows

Phase 4's TTM leg went through one correction worth recording, because the
naive implementation is subtly wrong and its output looks plausible.

**Deprecated approach.** Build 45 rolling 4-quarter windows across QL1..QL48
(1-4, 2-5, 3-6, …) and check whether the current window is the largest. The
flaw: a window such as QL3..QL6 spans Sep-24 through Jun-25 — two part-years,
a period the company never reported to anyone. A "record" found inside such a
window is an artifact of where the window happens to sit.

**Current approach.** Compute TTM **once** from the four most recent quarters
(`QL1+QL2+QL3+QL4`), then compare that single figure against the reported
financial-year series. The comparison run is `[TTM, FY1 … FY15]`; TTM is at
ATH when it is `>=` every FY **and** the peak FY is positive (a loss-making
peak is not a record to beat). Quarter-at-ATH is unchanged: `QL1` against
`max(QL1..QL48)`.

Edge case: when QL1 is the March quarter, QL1..QL4 spans exactly one financial
year, so TTM equals FY1. The test uses `>=`, so equality passes — it fails only
if an *earlier* FY beat it.

**Why this matters in practice.** Two real cases from the tracked universe:

| Symbol | TTM | Max rolling window | Peak reported FY | Old verdict | New verdict |
|---|---|---|---|---|---|
| NESTLEIND | 3,697 | 3,697 (current = max) | **3,928** | at ATH ✗ | not at ATH ✓ |
| BHEL | 2,432 | 2,432 (current = max) | **7,087** | at ATH ✗ | not at ATH ✓ |

Nestlé changed its year-end (Dec→Mar), so the 3,928 year spanned more than four
quarters and no rolling window can ever reach it. BHEL's peak sits ~15 years
back, beyond the 12-year quarterly series but inside the 15-year FY series.
The rolling method called a company earning a third of its historical peak
"profit at ATH"; the FY comparison catches both.

Because the FY series (15y) reaches further back than the quarterly series
(12y), comparing against reported FYs is not merely more principled — it sees
history the rolling method structurally cannot.

Distribution on the tracked universe after the change: **144 Dual, 188 Growth,
406 neither, 16 N/A** (was 133 / 175 / 431 / 15). The new rule is *less*
restrictive overall, because the phantom peaks the rolling windows invented
were suppressing legitimate records — while still correctly rejecting the four
symbols above that the old rule wrongly passed.

`ath_scanning_results` carries `profit_ttm` and `profit_peak_fy` so every
verdict is auditable from the UI tooltip.

### 7.10 Req. Profit as a pre-scan criterion

The profit verdict is presented as a **single binary column** rather than a
four-way badge plus filters. The user picks **Req. Profit** *before* running the
scan (default **Dual**), and the Profit column then reports only `At ATH` /
`Not at ATH` against that criterion.

Only two settings exist, and that is exhaustive rather than a simplification:
**Dual is a strict subset of Growth**. A quarter at an all-time high necessarily
beats its year-ago comparator, so every Dual stock also satisfies Growth —
verified against the tracked universe, where 0 of 144 Dual companies fail the
Growth leg. Consequently "either D or G" is identical to Growth, and "G but not
D" would exclude precisely the strongest names. The former four-option
post-scan filter (`Show all` / `Either` / `Dual Only` / `Growth Only`) collapsed
to these two.

The component legs (TTM-at-ATH, quarter-at-ATH, quarter-vs-year-ago, and the
`TTM vs peak FY` figures) moved into the badge tooltip, so a verdict stays
auditable without four extra columns.

**`profit_tracker.ath_profit` tracks Dual only — deliberately decoupled from the
scan criterion.** Apply Profit Flag sets `Y` for `D` and `N` for everything
else, *even when the scan required Growth*. The rationale: `ath_profit` gates
the FUND category through `get_investment_category`, and FUND is reserved for
the strict condition. Choosing Growth widens what the scan reports; it does not
widen what qualifies for portfolio classification.

This produces one deliberate asymmetry worth knowing: under `Req. Profit =
Growth`, a Growth-only stock shows **At ATH** in the Profit column while Apply
Profit Flag still writes **N**. The Manual cell renders that as `N → N` with a
tooltip reading "computed Growth, not Dual — the flag tracks Dual only", and the
apply dialog states it too. Worked example (AETHER, Growth, quarter not at ATH):

| Req. Profit | Profit column | Apply Profit Flag writes |
|---|---|---|
| Dual | Not at ATH | N |
| Growth | **At ATH** | **N** |

### 7.11 No minimum-history gate

An earlier version withheld a verdict below 3 reported FYs or 8 quarters,
returning `N/A`. That was wrong, and RUBICON is the case that showed it:

```
quarters (oldest->newest): 34.48 38.07 36.25 43.30 53.85 72.80 76.79 84.78
latest quarter 84.78 == max(all 8)          -> quarter at ATH
TTM  = 84.78+76.79+72.80+53.85 = 288.22
reported FYs: 134.36, 246.74  -> peak 246.74
288.22 >= 246.74                            -> TTM at ATH
=> DUAL
```

Both legs pass unambiguously. The only thing suppressing it was an arbitrary
threshold. A company's all-time high is over its **whole existence**, however
short — if a two-year-old listing's TTM beats both years it has reported, that
is a record for every year it has existed. Withholding a verdict there invents
uncertainty the criterion does not actually have.

The thresholds are now 1 FY and 1 quarter — i.e. evaluate whenever any data
exists. The single remaining limit is structural: TTM is the sum of the latest
four quarters, so a symbol with fewer than four cannot produce one (58 in the
feed). Depth is disclosed through `profit_points` in the tooltip, so a verdict
resting on 2 FYs is visibly weaker than one resting on 15, rather than being
silently withheld.

After the change the only remaining un-evaluable symbols are the 15 with no
feed coverage at all — the renamed/demerged tickers — and those display as
`Not at ATH` per §7.10.

### 7.12 Refresh Profit Data — live API → profit_history → flags

`profit_feed.py` replaces the `build_db.py → financial_data.duckdb →
import_profit_duckdb.py` chain for this app. The duckdb file was a staging post
on the way to SQLite — the data paused there and moved on unchanged — and
nothing here reads its `classifications` table, which was the only part
requiring the colleague's `fetch_classifications` module.

```
two Apps Script endpoints ──► profit_feed.refresh() ──► profit_history
                                                            │
                          ┌─────────────────────────────────┴──────────────┐
                          ▼                                                ▼
              Phase 4 of Run ATH Scan                    preview → apply flags
              → "At ATH / Not at ATH"                    → profit_tracker.ath_profit
```

**Mutual exclusion is the load-bearing part.** Both the refresh and the scan
take `scanner_state.is_running`, so neither can start while the other runs.
Without it a scan could read `profit_history` mid-rewrite and judge some stocks
on old data and some on new — with no error, no partial-write marker, and a
plausible-looking result table. Verified in both directions.

**Preview then apply, with a real cancel.** The refresh always rewrites
`profit_history` (reference data — safe, and wanted regardless). Only the write
to `profit_tracker.ath_profit` is gated: the UI reports how many flags would
change and in which direction, and Cancel leaves every flag untouched while
keeping the refreshed data. That split matters because `ath_profit` gates the
FUND category through `get_investment_category`, so applying moves stocks
between trading categories in one shot.

Symbols with no usable profit history are **never written** — the feed has no
opinion on them, and several are renamed tickers it structurally cannot cover
(TATAMOTORS → TMCV/TMPV). Overwriting would invent an opinion and silently drop
them out of FUND.

Applied flags follow the **Dual** test only, independent of the scan's Req.
Profit setting — see §7.10.
