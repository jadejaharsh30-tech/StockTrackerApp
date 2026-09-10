"""
Scanner Engine — Database-Light ATH Scanner with 4 Strategy Calculations.

Scans tickers from profit_tracker using three phases:
Phase 1: Sync new stocks from profit_tracker into ath_tracking_table (batch yfinance).
Phase 2: Fast batch detection of potential ATH hits using yfinance batch download.
Phase 3: In-depth analysis for hits (Green Candle, Close > ATH, RS Outperformance).

No dependency on market_data_yfinance.db. All historical data fetched live from yfinance.
"""
import os
import sqlite3
import pandas as pd
import yfinance as yf
import logging
import time
from datetime import datetime

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Honour DATABASE_PATH like app.py/daily_tasks.py/scanner_status.py do — without
# this the scanner writes to a different database than the routes read from.
TRACKER_DB = os.environ.get('DATABASE_PATH', os.path.join(BASE_DIR, 'tracker.db'))
# RS anchor, expressed exactly as the TradingView indicator expresses it.
#
#   Pine:   anchoredRS = rs / rs_at(last_bar_index - barsBackInput) * 100
#           with barsBackInput = 212
#
# so the anchor sits 212 BARS BACK from the latest bar, and the window from the
# anchor to today inclusive holds 213 bars. Pandas slices by row count, not by
# bar offset, hence the +1: iloc[-213:] puts the anchor 212 rows before the end.
# Keep these two lines together — setting a row count directly is how this drifted
# two bars away from the indicator in the first place.
RS_BARS_BACK = 212               # == the Pine `barsBackInput` input
LOOKBACK = RS_BARS_BACK + 1      # rows to slice, so anchor lands RS_BARS_BACK back
# A short-listed stock still has a relative-strength history over its own life.
# Below this many aligned sessions the anchored line is too short to mean
# anything, so the verdict stays N/A; between here and LOOKBACK we use whatever
# history exists and report the window length alongside the numbers.
MIN_RS_SESSIONS = 20
BATCH_SIZE = 50

# Progress is split across phases so the bar keeps moving through the slow
# in-depth work instead of pinning at 100% once price download finishes.
PHASE2_SHARE = 0.75   # batch price download
PHASE3_SHARE = 0.20   # RS / green-candle analysis of hits
# remaining 0.05 = phase 4 profit classification


# ====================== DATABASE HELPERS ======================

# The tracked-universe scan and the custom-universe scan keep their results in
# SEPARATE tables, so running one never destroys the other's output. 'tracked'
# keeps the historic table name so existing databases carry straight over.
RESULTS_TABLES = {
    'tracked': 'ath_scanning_results',
    'custom': 'ath_scanning_results_custom',
}


def _results_table(scope):
    """Table name for a scan scope. Unknown scopes fall back to tracked."""
    return RESULTS_TABLES.get(scope, RESULTS_TABLES['tracked'])


def init_scanning_results_table(scope='tracked'):
    """Create or recreate one scope's results table with the current schema."""
    table = _results_table(scope)
    conn = sqlite3.connect(TRACKER_DB)
    conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute(f"""
        CREATE TABLE {table} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            new_ath_price REAL,
            trigger_price REAL,
            green_candle TEXT,
            close_gt_ath TEXT,
            ath_outperformance TEXT,
            current_rs REAL,
            ath_rs REAL,
            rs_window INTEGER,
            profit_ttm_ath TEXT,
            profit_qtr_ath TEXT,
            profit_yoy TEXT,
            profit_flag TEXT,
            profit_basis TEXT,
            profit_points INTEGER,
            profit_ttm REAL,
            profit_peak_fy REAL,
            profit_meets TEXT,
            profit_criterion TEXT,
            profit_reason TEXT,
            manual_ath_profit TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()
    logger.info(f"{table} initialized with the current schema.")


def get_profit_tracker_tickers(user_id=None):
    """Get distinct symbols from profit_tracker table."""
    conn = sqlite3.connect(TRACKER_DB)
    try:
        if user_id:
            rows = conn.execute(
                "SELECT DISTINCT symbol FROM profit_tracker WHERE user_id = ?",
                (user_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT DISTINCT symbol FROM profit_tracker").fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def save_scan_results(results, scope='tracked'):
    """Clear this scope's results table and insert fresh results."""
    table = _results_table(scope)
    conn = sqlite3.connect(TRACKER_DB)
    try:
        conn.execute(f"DELETE FROM {table}")
        for r in results:
            conn.execute(f"""
                INSERT INTO {table}
                (symbol, new_ath_price, trigger_price, green_candle, close_gt_ath, ath_outperformance, current_rs, ath_rs, rs_window,
                 profit_ttm_ath, profit_qtr_ath, profit_yoy, profit_flag, profit_basis, profit_points,
                 profit_ttm, profit_peak_fy, profit_meets, profit_criterion, profit_reason,
                 manual_ath_profit)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (r['symbol'], r['new_ath_price'], r['trigger_price'],
                  r['green_candle'], r['close_gt_ath'], r['ath_outperformance'],
                  r.get('current_rs'), r.get('ath_rs'), r.get('rs_window'),
                  r.get('profit_ttm_ath'), r.get('profit_qtr_ath'), r.get('profit_yoy'),
                  r.get('profit_flag'), r.get('profit_basis'), r.get('profit_points'),
                  r.get('profit_ttm'), r.get('profit_peak_fy'),
                  r.get('profit_meets'), r.get('profit_criterion'), r.get('profit_reason'),
                  r.get('manual_ath_profit')))
        conn.commit()
        logger.info(f"Saved {len(results)} scan results to {table}.")
    finally:
        conn.close()


def init_scan_runs_table(conn=None):
    """
    One row per completed scan. Each scope's results table already survives until
    the next scan of THAT scope overwrites it, so this only records what produced
    those rows — when, over which universe, and under which profit criterion — so
    the page can say what it is showing when the user asks to see it again.

    `universe` doubles as the scope key ('tracked' / 'custom'), which is why
    splitting the results tables needed no migration here.
    """
    own = conn is None
    conn = conn or sqlite3.connect(TRACKER_DB)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scan_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                finished_at TEXT,
                universe TEXT,
                universe_size INTEGER,
                criterion TEXT,
                hits INTEGER
            )
        """)
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()


def record_scan_run(user_id, universe, universe_size, criterion, hits):
    conn = sqlite3.connect(TRACKER_DB)
    try:
        init_scan_runs_table(conn)
        conn.execute(
            """INSERT INTO scan_runs (user_id, finished_at, universe, universe_size, criterion, hits)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
             universe, universe_size, criterion, hits))
        conn.commit()
    except Exception as e:
        logger.warning(f"Could not record scan run: {e}")
    finally:
        conn.close()


def get_last_scan_run(user_id=None, scope=None):
    """
    Metadata for the results currently sitting in one scope's results table.

    With no scope, returns the most recent run of either kind — used only where
    the caller genuinely wants "the last thing that ran".
    """
    conn = sqlite3.connect(TRACKER_DB)
    conn.row_factory = sqlite3.Row
    try:
        init_scan_runs_table(conn)
        conn.commit()
        where, params = [], []
        if user_id:
            where.append("user_id = ?")
            params.append(user_id)
        if scope:
            where.append("universe = ?")
            params.append(scope)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        row = conn.execute(
            f"SELECT * FROM scan_runs {clause} ORDER BY id DESC LIMIT 1", params).fetchone()
        return dict(row) if row else None
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()


def get_scan_results(scope='tracked'):
    """
    Fetch one scope's stored results.

    Returns [] rather than raising when that scope has never been scanned, so a
    fresh database answers "nothing yet" instead of failing the request.
    """
    table = _results_table(scope)
    conn = sqlite3.connect(TRACKER_DB)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(f"SELECT * FROM {table} ORDER BY symbol").fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


# ====================== ATH TRACKING TABLE HELPERS ======================

def get_current_ath_map():
    """Get current previous_ath prices for all tracked symbols."""
    conn = sqlite3.connect(TRACKER_DB)
    try:
        rows = conn.execute("SELECT symbol, previous_ath FROM ath_tracking_table").fetchall()
        return {r[0]: (float(r[1]) if r[1] is not None else 0.0) for r in rows}
    finally:
        conn.close()


def update_today_ath(results):
    """Update today_ath in ath_tracking_table for stocks that hit ATH today."""
    if not results:
        return
    conn = sqlite3.connect(TRACKER_DB)
    try:
        for r in results:
            conn.execute("""
                UPDATE ath_tracking_table 
                SET today_ath = ?, last_updated = ?
                WHERE symbol = ?
            """, (r['new_ath_price'], time.time(), r['symbol']))
        conn.commit()
        logger.info(f"Updated today_ath for {len(results)} stocks.")
    finally:
        conn.close()


def promote_ath_eod(symbols):
    """
    EOD Promotion: Copy today_ath -> previous_ath for explicitly selected stocks,
    then globally clear today_ath. Only promotes if today_ath > previous_ath.
    Returns the count of promoted stocks.
    """
    if not symbols:
        return 0
        
    conn = sqlite3.connect(TRACKER_DB)
    try:
        from datetime import datetime
        today_str = datetime.now().strftime('%Y-%m-%d')
        
        # Prepare placeholders for IN clause
        placeholders = ','.join(['?'] * len(symbols))
        
        # Only promote explicitly selected symbols if today's ATH is actually higher
        query = f"""
            UPDATE ath_tracking_table 
            SET previous_ath = today_ath, last_updated = ?, ath_date = ?
            WHERE symbol IN ({placeholders}) AND today_ath IS NOT NULL AND today_ath > previous_ath
        """
        params = [time.time(), today_str] + symbols
        cursor = conn.execute(query, params)
        promoted_count = cursor.rowcount

        # Clear all today_ath values globally to prevent leftover bugs tomorrow
        conn.execute("UPDATE ath_tracking_table SET today_ath = NULL WHERE today_ath IS NOT NULL")
        conn.commit()
        logger.info(f"EOD Promotion complete: {promoted_count} stocks promoted.")
        return promoted_count
    finally:
        conn.close()


# ====================== SYNC NEW STOCKS ======================

def sync_new_stocks_to_ath_tracker(tickers, progress_callback=None):
    """
    Phase 1: Sync new stocks from profit_tracker into ath_tracking_table.
    For any ticker not yet in ath_tracking_table, downloads period='max' from yfinance
    to find the true lifetime high, and inserts it as previous_ath.
    Uses batch downloads for speed.
    """
    conn = sqlite3.connect(TRACKER_DB)
    try:
        existing = set(r[0] for r in conn.execute("SELECT symbol FROM ath_tracking_table").fetchall())
        # A baseline of 0/NULL is a failed earlier sync, not a real lifetime high.
        # Left alone it passes the `high >= previous_ath` filter forever and
        # reports a bogus hit with a trigger price of 0 on every scan, so retry
        # those alongside genuinely new tickers.
        broken = set(r[0] for r in conn.execute(
            "SELECT symbol FROM ath_tracking_table WHERE previous_ath IS NULL OR previous_ath <= 0"
        ).fetchall())
    finally:
        conn.close()

    new_tickers = [t for t in tickers if t not in existing or t in broken]

    if not new_tickers:
        logger.info("No new stocks to sync.")
        return

    retrying = len([t for t in new_tickers if t in broken])
    if retrying:
        logger.info(f"Retrying {retrying} symbols with a missing/zero baseline.")

    logger.info(f"Syncing {len(new_tickers)} new stocks into ATH tracker...")
    if progress_callback:
        progress_callback(0, len(tickers), f"Syncing {len(new_tickers)} new stocks (fetching lifetime ATH)...")

    # Batch download max history for new tickers
    batches = [new_tickers[i:i + BATCH_SIZE] for i in range(0, len(new_tickers), BATCH_SIZE)]
    new_entries = []  # list of (symbol, previous_ath)

    for batch in batches:
        yf_symbols = [f"{s}.NS" for s in batch]
        try:
            data = yf.download(yf_symbols, period="max", interval="1d", auto_adjust=False, progress=False)
            if data.empty:
                continue

            for symbol in batch:
                yf_sym = f"{symbol}.NS"
                try:
                    if isinstance(data.columns, pd.MultiIndex):
                        if yf_sym not in data.columns.get_level_values(1):
                            continue
                        high_series = data['High'][yf_sym].dropna()
                    else:
                        # Single ticker in batch
                        high_series = data['High'].dropna()

                    if high_series.empty:
                        new_entries.append((symbol, 0.0))
                        continue

                    # Exclude today's candle for the lifetime ATH
                    today = pd.Timestamp.now().normalize()
                    hist_highs = high_series[high_series.index.normalize() < today]
                    
                    if hist_highs.empty:
                        lifetime_ath = float(high_series.max())
                    else:
                        lifetime_ath = float(hist_highs.max())

                    new_entries.append((symbol, lifetime_ath))
                except Exception as e:
                    logger.warning(f"Could not process {symbol} during sync: {e}")
                    new_entries.append((symbol, 0.0))
        except Exception as e:
            logger.error(f"Batch download failed during sync: {e}")
            # Insert with 0 so they're at least tracked
            for symbol in batch:
                new_entries.append((symbol, 0.0))

    # Insert all new entries into ath_tracking_table
    conn = sqlite3.connect(TRACKER_DB)
    try:
        for symbol, ath_price in new_entries:
            # Insert new symbols; for existing rows only fill in a baseline that
            # is still missing/zero. A good baseline is never overwritten here —
            # promotion of a real new high is the EOD promote step's job.
            conn.execute("""
                INSERT INTO ath_tracking_table (symbol, previous_ath, exchange, last_updated)
                VALUES (?, ?, 'NSE', ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    previous_ath = excluded.previous_ath,
                    last_updated = excluded.last_updated
                WHERE (ath_tracking_table.previous_ath IS NULL
                       OR ath_tracking_table.previous_ath <= 0)
                  AND excluded.previous_ath > 0
            """, (symbol, ath_price, time.time()))
        conn.commit()
        logger.info(f"Synced {len(new_entries)} new stocks into ATH tracker.")
    finally:
        conn.close()


# ====================== LIVE DATA ======================

RS_BENCHMARK = "^CRSLDX"        # Nifty 500 — the ONLY benchmark for RS
INDEX_FETCH_ATTEMPTS = 3


def fetch_nifty_live():
    """
    Fetch Nifty 500 (^CRSLDX) 2-year daily data for RS calculation.

    There is deliberately NO fallback to another index. RS is the ratio of the
    stock to this specific benchmark, so quietly swapping in Nifty 50 would
    change every number on the page by the ratio of the two indices while
    looking entirely plausible — the same class of silent wrongness as an
    unnoticed price-adjustment change. A missing benchmark is a failed scan,
    not a scan against something else.

    A transient empty response is retried, because retrying is how you make
    ^CRSLDX work; substituting a different index is not.
    """
    idx = pd.DataFrame()
    for attempt in range(1, INDEX_FETCH_ATTEMPTS + 1):
        logger.info(f"Fetching {RS_BENCHMARK} (attempt {attempt}/{INDEX_FETCH_ATTEMPTS})...")
        try:
            idx = yf.download(RS_BENCHMARK, period="2y", interval="1d",
                              progress=False, auto_adjust=True)
        except Exception as e:
            logger.warning(f"{RS_BENCHMARK} fetch attempt {attempt} raised: {e}")
            idx = pd.DataFrame()
        if not idx.empty:
            break
        if attempt < INDEX_FETCH_ATTEMPTS:
            time.sleep(2 * attempt)

    if idx.empty:
        raise RuntimeError(
            f"Could not fetch the {RS_BENCHMARK} (Nifty 500) benchmark after "
            f"{INDEX_FETCH_ATTEMPTS} attempts. RS is measured against this index only, "
            f"so the scan is stopped rather than run against a different one. "
            f"Check network access to Yahoo Finance and retry.")

    # Handle multi-index columns (yfinance change)
    if isinstance(idx.columns, pd.MultiIndex):
        series = idx['Close'].iloc[:, 0]
    else:
        series = idx['Close']

    # Normalize index to dates only
    series.index = pd.to_datetime(series.index).normalize()
    return series


def fetch_ticker_history_for_rs(symbol):
    """
    Fetch 1 year of daily close data for a single ticker for RS calculation.
    Returns a pandas Series (date index -> close price) or None on failure.
    """
    try:
        yf_sym = f"{symbol}.NS"
        # auto_adjust=False by deliberate choice — see calculate_rs_outperformance.
        data = yf.download(yf_sym, period="1y", interval="1d", progress=False, auto_adjust=False)
        
        if data.empty:
            return None

        # Handle MultiIndex
        if isinstance(data.columns, pd.MultiIndex):
            close_series = data['Close'].iloc[:, 0]
        else:
            close_series = data['Close']

        close_series.index = pd.to_datetime(close_series.index).normalize()
        return close_series.dropna()
    except Exception as e:
        logger.error(f"Failed to fetch history for {symbol}: {e}")
        return None


# ====================== CALCULATIONS ======================

def calculate_single_ticker(symbol, live_candle, nifty_series, trigger_price, history_closes=None):
    """
    Run all 4 strategy calculations for a single ticker hit.
    
    Args:
        symbol: Bare ticker (e.g. 'SBIN', no .NS suffix)
        live_candle: dict with 'High', 'Close', 'PrevClose'
        nifty_series: Pre-fetched Nifty close series
        trigger_price: The previous_ath value (== trigger price)
        history_closes: Pre-fetched 1y history closes
    
    Returns:
        dict with results, or None if data is insufficient
    """
    live_high = live_candle['High']
    live_close = live_candle['Close']
    prev_close = live_candle['PrevClose']
    
    today_date = pd.to_datetime(datetime.now().date()).normalize()

    # 1. Trigger Price is passed in directly as previous_ath
    #    (They are identical by design — true lifetime ATH excluding today)

    # 2. Green Candle: today_close >= prev_close
    green_candle = 'Y' if live_close >= prev_close else 'N'

    # 3. Close > ATH: today_close > trigger_price
    close_gt_ath = 'Y' if round(live_close, 2) > round(trigger_price, 2) else 'N'

    # 4. ATH Outperformance (Rolling RS calculation)
    if history_closes is None or history_closes.empty:
        # Fallback to single fetch only if batch failed
        history_closes = fetch_ticker_history_for_rs(symbol)
    
    if history_closes is None or history_closes.empty:
        return {
            'symbol': symbol,
            'new_ath_price': round(live_high, 2),
            'trigger_price': round(trigger_price, 2),
            'green_candle': green_candle,
            'close_gt_ath': close_gt_ath,
            'ath_outperformance': 'N/A',
            'current_rs': None,
            'ath_rs': None,
            'rs_window': 0,
        }

    # Filter to strictly before today for alignment with live candle
    history_closes = history_closes[history_closes.index < today_date]

    rs_results = calculate_rs_outperformance(
        history_closes, live_close, today_date, nifty_series
    )

    return {
        'symbol': symbol,
        'new_ath_price': round(live_high, 2),
        'trigger_price': round(trigger_price, 2),
        'green_candle': green_candle,
        'close_gt_ath': close_gt_ath,
        'ath_outperformance': rs_results['is_outperforming'],
        'current_rs': rs_results.get('current_rs'),
        'ath_rs': rs_results.get('ath_rs'),
        'rs_window': rs_results.get('rs_window', 0),
    }


def calculate_rs_outperformance(history_closes, live_close, today_date, nifty_series):
    """
    ATH Outperformance via a fixed-anchor relative-strength line.

    This mirrors the TradingView indicator ("Anchored & ATH RS"): the anchor is
    the stock/index ratio RS_BARS_BACK (212) bars before the latest bar, the line
    is that ratio re-expressed as a percentage of the anchor, and the verdict is
    whether today sits at the line's own maximum. Match any change here against
    the Pine source before shipping it — the two are meant to agree bar for bar.

    Preferred window is LOOKBACK sessions. A recently-listed stock has fewer,
    but its RS over its own listed life is still a real measurement, so the
    window shrinks to whatever history exists rather than refusing a verdict.
    Below MIN_RS_SESSIONS it stays N/A. The window actually used is returned as
    rs_window so a short-history reading is visibly weaker than a full one.

    KNOWN LIMITATION, accepted deliberately: the stock history is fetched
    UNADJUSTED (auto_adjust=False) while the index series is adjusted. A split
    therefore puts a cliff in the ratio, and since the anchor sits at the window
    start, sessions after the split read as a collapse — a 1:4 split shows
    current_rs at roughly a quarter of ath_rs and flips the verdict to N, even
    though the stock has done nothing wrong.

    This affects only the handful of stocks that split within the window, and
    the user handles those by hand. Do NOT "fix" this by switching to
    auto_adjust=True without asking: adjusted prices also fold in dividends and
    would shift RS for every stock, and the raw series is what the manually
    maintained previous_ath baselines are expressed in.
    """
    try:
        # Build full close series (history + today live)
        full_closes = history_closes.copy()
        full_closes[today_date] = live_close

        # Align stock and index (inner join handles holidays)
        aligned = pd.DataFrame({
            'Stock': full_closes,
            'Index': nifty_series,
        }).dropna()

        if len(aligned) < MIN_RS_SESSIONS:
            return {'is_outperforming': 'N/A', 'current_rs': None, 'ath_rs': None,
                    'rs_window': len(aligned)}

        # Raw ratio
        aligned['RS_Raw'] = aligned['Stock'] / aligned['Index']

        # Window = last LOOKBACK rows, or the whole series when it is shorter
        window = aligned.iloc[-LOOKBACK:]

        # Anchor value from start of window
        anchor_rs_raw = window['RS_Raw'].iloc[0]
        if anchor_rs_raw == 0:
            return {'is_outperforming': 'N/A', 'current_rs': None, 'ath_rs': None,
                    'rs_window': len(window)}

        # Anchored line for full window
        anchored_line = (window['RS_Raw'] / anchor_rs_raw) * 100
        current_anchored = float(anchored_line.iloc[-1])
        max_anchored = float(anchored_line.max())

        # Final check
        if current_anchored >= (max_anchored * 0.9999):
            is_op = 'Y'
        else:
            is_op = 'N'

        return {
            'is_outperforming': is_op,
            'current_rs': round(current_anchored, 2),
            'ath_rs': round(max_anchored, 2),
            'rs_window': len(window),
        }

    except Exception as e:
        logger.error(f"RS calculation error: {e}")
        return {'is_outperforming': 'N/A', 'current_rs': None, 'ath_rs': None,
                'rs_window': 0}


# ====================== MAIN SCAN ORCHESTRATOR ======================

def resync_ath_baselines(tickers, progress_callback=None, user_id=None, only_broken=False):
    """
    Recompute previous_ath from full history for tickers already in the tracker.

    Phase 1 of a normal scan only seeds symbols it has never seen, so a baseline
    that was wrong at seed time stays wrong forever. This is the periodic
    re-validation the (broken) legacy 'weekend refresh' was meant to provide.

    Unlike the legacy scanner it does NOT promote today's high: the lifetime max
    is computed excluding today's candle, so the EOD-promote discipline that the
    rest of the workflow depends on is preserved.

    only_broken=True limits the work to rows whose baseline is missing or <= 0.
    """
    from scanner_status import ScannerStatusManager
    status_manager = ScannerStatusManager()

    def update(progress, total, message, running=True):
        if progress_callback:
            progress_callback(progress, total, message)
        if user_id:
            status_manager.set_status(user_id, running, progress, total, message)

    conn = sqlite3.connect(TRACKER_DB)
    try:
        if only_broken:
            targets = [r[0] for r in conn.execute(
                "SELECT symbol FROM ath_tracking_table "
                "WHERE previous_ath IS NULL OR previous_ath <= 0").fetchall()]
        else:
            targets = list(tickers)
    finally:
        conn.close()

    total = len(targets)
    if not total:
        update(0, 0, "No baselines needed refreshing.", running=False)
        return {'checked': 0, 'updated': 0, 'failed': []}

    updated, failed = 0, []
    batches = [targets[i:i + BATCH_SIZE] for i in range(0, total, BATCH_SIZE)]
    today = pd.Timestamp.now().normalize()

    for i, batch in enumerate(batches):
        update(i * BATCH_SIZE, total, f"Refreshing baselines {i+1}/{len(batches)}...")
        try:
            data = yf.download([f"{s}.NS" for s in batch], period="max",
                               interval="1d", auto_adjust=False, progress=False)
        except Exception as e:
            logger.error(f"Baseline refresh batch failed: {e}")
            failed.extend(batch)
            continue
        if data.empty:
            failed.extend(batch)
            continue

        conn = sqlite3.connect(TRACKER_DB)
        try:
            for symbol in batch:
                yf_sym = f"{symbol}.NS"
                try:
                    if isinstance(data.columns, pd.MultiIndex):
                        if yf_sym not in data.columns.get_level_values(1):
                            failed.append(symbol)
                            continue
                        highs = data['High'][yf_sym].dropna()
                    else:
                        highs = data['High'].dropna()
                    hist = highs[highs.index.normalize() < today]
                    if hist.empty:
                        failed.append(symbol)
                        continue
                    lifetime = float(hist.max())
                    if lifetime <= 0:
                        failed.append(symbol)
                        continue
                    conn.execute(
                        """UPDATE ath_tracking_table
                           SET previous_ath = ?, last_updated = ?
                           WHERE symbol = ?""",
                        (lifetime, time.time(), symbol))
                    updated += 1
                except Exception:
                    failed.append(symbol)
            conn.commit()
        finally:
            conn.close()

    msg = f"Baseline refresh complete. {updated} updated"
    if failed:
        msg += f", {len(failed)} unresolved (likely renamed/delisted)"
    update(total, total, msg + ".", running=False)
    logger.info(msg)
    return {'checked': total, 'updated': updated, 'failed': failed}


def annotate_profit_flags(results, tolerance_pct=0.0, user_id=None, criterion='D'):
    """
    Phase 4: attach Dual/Growth profit classification to each scan result.

    Reads reported profit history from the local `profit_history` table (see
    profit_scanner.py) and, for comparison, the user's existing manual
    `profit_tracker.ath_profit` flag. Never overwrites the manual flag — the
    UI surfaces both and lets the user apply the computed value per stock.
    """
    from profit_scanner import classify_many, meets_criterion

    symbols = [r['symbol'] for r in results]
    verdicts = classify_many(symbols, tolerance_pct=tolerance_pct)

    # Pull the user's current manual flags so the UI can show computed vs manual
    manual = {}
    try:
        conn = sqlite3.connect(TRACKER_DB)
        if user_id:
            rows = conn.execute(
                "SELECT symbol, ath_profit FROM profit_tracker WHERE user_id = ?",
                (user_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT symbol, ath_profit FROM profit_tracker").fetchall()
        manual = {r[0]: r[1] for r in rows}
        conn.close()
    except Exception as e:
        logger.warning(f"Could not read manual ath_profit flags: {e}")

    classified = 0
    for r in results:
        v = verdicts.get(r['symbol'], {})
        r['profit_ttm_ath'] = v.get('profit_ttm_ath', 'N/A')
        r['profit_qtr_ath'] = v.get('profit_qtr_ath', 'N/A')
        r['profit_yoy'] = v.get('profit_yoy', 'N/A')
        r['profit_flag'] = v.get('profit_flag', 'N/A')
        r['profit_basis'] = v.get('profit_basis')
        r['profit_points'] = v.get('profit_points', 0)
        r['profit_ttm'] = v.get('profit_ttm')
        r['profit_peak_fy'] = v.get('profit_peak_fy')
        # Does it satisfy the criterion the user picked before this scan?
        r['profit_meets'] = meets_criterion(v, criterion) if v else 'N/A'
        r['profit_criterion'] = criterion
        r['profit_reason'] = v.get('profit_reason')
        r['manual_ath_profit'] = manual.get(r['symbol'])
        if r['profit_meets'] == 'Y':
            classified += 1

    label = 'Dual' if criterion == 'D' else 'Growth (incl. Dual)'
    logger.info(f"Profit classification: {classified}/{len(results)} meet {label}.")
    return results


def run_full_scan(tickers, progress_callback=None, user_id=None, profit_tolerance_pct=0.0,
                  profit_criterion='D', scope='tracked'):
    """
    Run the optimized full ATH scan using batch processing.

    Args:
        tickers: List of bare ticker symbols (no .NS)
        progress_callback: Optional fn(progress, total, message) for UI updates
        user_id: Optional user_id to persist status in DB
        profit_tolerance_pct: Slack (%) allowed below the profit peak when
            judging "at ATH" (0 = must equal or exceed the peak)
    """
    from scanner_status import ScannerStatusManager
    status_manager = ScannerStatusManager()

    total = len(tickers)
    results = []

    def update_status(progress, total, message, running=True):
        if progress_callback:
            progress_callback(progress, total, message)
        if user_id:
            status_manager.set_status(user_id, running, progress, total, message)

    update_status(0, total, "Initializing scanner...")

    # ── Phase 0: Safely clear old intraday tracking ──
    # Prevents stale data from bleeding into today's scan if EOD sync was skipped
    try:
        conn = sqlite3.connect(TRACKER_DB)
        conn.execute("UPDATE ath_tracking_table SET today_ath = NULL WHERE today_ath IS NOT NULL")
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to clear old today_ath values: {e}")

    # ── Phase 1: Sync new stocks into ath_tracking_table ──
    try:
        sync_new_stocks_to_ath_tracker(tickers, progress_callback=update_status)
    except Exception as e:
        logger.error(f"Sync failed: {e}")

    # ── Phase 2: Load Nifty once and Get current ATH Map ──
    try:
        update_status(0, total, "Fetching Nifty index data...")
        nifty_series = fetch_nifty_live()
        ath_map = get_current_ath_map()
    except Exception as e:
        logger.error(f"Initialization failed: {e}")
        if progress_callback: progress_callback(0, total, f"Error: {e}")
        return results

    # ── Phase 2: Fast ATH Filtering via batch download ──
    # Group into batches of 50 for fast batch download
    batches = [tickers[i:i + BATCH_SIZE] for i in range(0, total, BATCH_SIZE)]
    
    potential_hits = {}  # symbol -> live_candle_dict
    unresolved = []      # symbols with no usable baseline

    for i, batch in enumerate(batches):
        processed_count = int(i * BATCH_SIZE * PHASE2_SHARE)
        update_status(processed_count, total, f"Batch {i+1}/{len(batches)}: Downloading prices...")

        # Add .NS suffix for batch download
        yf_symbols = [f"{s}.NS" for s in batch]
        
        try:
            # period="5d" to ensure we have "yesterday's close" even on Mondays/Holidays
            data = yf.download(yf_symbols, period="5d", interval="1d", auto_adjust=False, progress=False)
            
            if data.empty: continue

            for symbol in batch:
                yf_sym = f"{symbol}.NS"
                try:
                    # Extract High/Close using MultiIndex handling
                    if isinstance(data.columns, pd.MultiIndex):
                        if yf_sym not in data.columns.get_level_values(1): continue
                        
                        high_series = data['High'][yf_sym].dropna()
                        close_series = data['Close'][yf_sym].dropna()
                    else:
                        # Single-ticker batch fallback
                        high_series = data['High'].dropna()
                        close_series = data['Close'].dropna()

                    if high_series.empty or close_series.empty: continue
                    
                    live_high = float(high_series.iloc[-1])
                    live_close = float(close_series.iloc[-1])
                    
                    # Previous close (for Green Candle)
                    prev_close = float(close_series.iloc[-2]) if len(close_series) >= 2 else live_close

                    # Fast Filter: Compare against previous_ath from ath_tracking_table
                    current_ath = ath_map.get(symbol, 0.0)

                    # No usable baseline (sync failed — typically a renamed or
                    # delisted ticker). Any price beats 0, so without this guard
                    # the symbol is reported as an ATH hit with trigger price 0
                    # on every single scan.
                    if current_ath <= 0:
                        unresolved.append(symbol)
                        continue

                    if round(live_high, 2) >= round(current_ath, 2):
                        potential_hits[symbol] = {
                            'High': live_high,
                            'Close': live_close,
                            'PrevClose': prev_close,
                            'TriggerPrice': current_ath  # previous_ath == trigger price
                        }
                except:
                    continue
        except Exception as e:
            logger.error(f"Batch download failed: {e}")

    # ── Phase 3: In-depth 4-strategy analysis for potential hits ──
    total_hits = len(potential_hits)
    phase3_base = int(total * PHASE2_SHARE)
    if unresolved:
        logger.warning(
            f"{len(unresolved)} symbols skipped — no usable ATH baseline "
            f"(likely renamed/delisted): {', '.join(unresolved[:10])}"
        )
    update_status(phase3_base, total,
                  f"Detected {total_hits} potential hits. Analyzing (Batch Fetching History)...")

    if total_hits > 0:
        # Batch Fetch 1y history for all hits to avoid 1-by-1 overhead
        hit_symbols = list(potential_hits.keys())
        yf_hit_symbols = [f"{s}.NS" for s in hit_symbols]
        try:
            # auto_adjust=False by deliberate choice — see calculate_rs_outperformance.
            hist_data = yf.download(yf_hit_symbols, period="1y", interval="1d", auto_adjust=False, progress=False)
        except Exception as e:
            logger.error(f"Batch history fetch failed: {e}")
            hist_data = pd.DataFrame()

        for i, symbol in enumerate(hit_symbols):
            try:
                live_candle = potential_hits[symbol]
                update_status(
                    phase3_base + int(total * PHASE3_SHARE * (i + 1) / max(total_hits, 1)),
                    total, f"Analyzing {symbol} ({i+1}/{total_hits})")
                
                # Extract history for this specific ticker from the batch
                history_closes = None
                if not hist_data.empty:
                    yf_sym = f"{symbol}.NS"
                    if isinstance(hist_data.columns, pd.MultiIndex):
                        if yf_sym in hist_data.columns.get_level_values(1):
                            history_closes = hist_data['Close'][yf_sym].dropna()
                    else:
                        history_closes = hist_data['Close'].dropna()
                    
                    if history_closes is not None:
                        history_closes.index = pd.to_datetime(history_closes.index).normalize()

                result = calculate_single_ticker(
                    symbol, live_candle, nifty_series, 
                    trigger_price=live_candle['TriggerPrice'],
                    history_closes=history_closes # Pass pre-fetched history
                )
                if result:
                    results.append(result)
            except Exception as e:
                logger.error(f"Analysis failed for {symbol}: {e}")
    else:
        update_status(phase3_base, total, "No potential hits detected.")

    # ── Phase 4: Profit ATH classification (Dual / Growth) ──
    # Runs only over confirmed hits, and reads the local profit_history table,
    # so it costs no network calls regardless of universe size.
    if results:
        update_status(int(total * (PHASE2_SHARE + PHASE3_SHARE)), total,
                      f"Classifying profit history for {len(results)} hits...")
        try:
            annotate_profit_flags(results, tolerance_pct=profit_tolerance_pct,
                                  user_id=user_id, criterion=profit_criterion)
        except Exception as e:
            logger.error(f"Profit classification failed: {e}")

    # Rebuild the results table only now that there is something to write. Doing
    # it up front would mean an aborted scan — a missing ^CRSLDX benchmark, say —
    # left the user with an empty table and no way back to the previous run.
    init_scanning_results_table(scope)
    save_scan_results(results, scope=scope)

    # Update today_ath for all hits
    update_today_ath(results)

    update_status(total, total, f"Scan complete. {len(results)} verified ATH hits found.", running=False)

    logger.info(f"Scan complete: {len(results)} hits found from {total} tickers.")
    return results


# ====================== CUSTOM UNIVERSE PIPELINE ======================

def run_custom_pipeline(tickers, user_id=None, refresh_profit=False, refresh_baselines=True,
                        profit_criterion='D', profit_tolerance_pct=0.0):
    """
    Run the whole chain over an ad-hoc universe, in the only order that is correct:

      1. (optional) refresh profit_history from the APIs — universe-independent,
         so it goes first and the scan's Phase 4 then reads fresh data.
      2. (optional) re-validate ATH baselines for THESE tickers. Must precede the
         scan: the scan's own Phase 1 only seeds symbols it has never seen, so a
         symbol already carrying a stale baseline would keep it and produce a
         wrong trigger price.
      3. Run the ATH scan over the universe.

    Progress from each stage is rescaled into its own band so the bar advances
    once across the whole run instead of resetting three times.
    """
    from scanner_status import ScannerStatusManager
    status_manager = ScannerStatusManager()

    stages = []
    if refresh_profit:
        stages.append('profit')
    if refresh_baselines:
        stages.append('baselines')
    stages.append('scan')
    n = len(stages)

    def band(i):
        """(start, end) percentage band for stage index i."""
        return (100 * i) // n, (100 * (i + 1)) // n

    def say(i, label, pct_within, message):
        lo, hi = band(i)
        pct = lo + int((hi - lo) * max(0.0, min(1.0, pct_within)))
        if user_id:
            status_manager.set_status(user_id, True, pct, 100,
                                      f"[{i+1}/{n}] {label}: {message}")

    idx = 0
    if refresh_profit:
        from profit_feed import refresh as refresh_profit_data
        say(idx, 'Profit data', 0.0, 'starting...')
        try:
            refresh_profit_data(progress_callback=lambda pct, msg: say(idx, 'Profit data', pct / 100.0, msg))
        except Exception as e:
            logger.error(f"Profit refresh failed during custom pipeline: {e}")
            if user_id:
                status_manager.set_status(user_id, False, 0, 100,
                                          f"Profit refresh failed: {e}", force=True)
            return {'error': f'Profit refresh failed: {e}'}
        idx += 1

    if refresh_baselines:
        stage = idx
        say(stage, 'Baselines', 0.0, f'refreshing {len(tickers)} symbols...')
        try:
            resync_ath_baselines(
                tickers,
                progress_callback=lambda p, t, m: say(stage, 'Baselines', (p / t) if t else 0, m))
        except Exception as e:
            logger.error(f"Baseline refresh failed during custom pipeline: {e}")
            if user_id:
                status_manager.set_status(user_id, False, 0, 100,
                                          f"Baseline refresh failed: {e}", force=True)
            return {'error': f'Baseline refresh failed: {e}'}
        idx += 1

    stage = idx
    say(stage, 'ATH scan', 0.0, f'scanning {len(tickers)} symbols...')
    # Writes to the CUSTOM results table — a custom run never overwrites the
    # tracked-universe results, and vice versa. run_full_scan rebuilds that table
    # itself, once it has results to put in it.
    results = run_full_scan(
        tickers,
        progress_callback=lambda p, t, m: say(stage, 'ATH scan', (p / t) if t else 0, m),
        user_id=None,                       # this orchestrator owns the status line
        profit_tolerance_pct=profit_tolerance_pct,
        profit_criterion=profit_criterion,
        scope='custom')

    record_scan_run(user_id, 'custom', len(tickers), profit_criterion, len(results))
    msg = f"Custom scan complete. {len(results)} ATH hits from {len(tickers)} symbols."
    if user_id:
        status_manager.set_status(user_id, False, 100, 100, msg, force=True)
    logger.info(msg)
    return {'hits': len(results), 'universe_size': len(tickers)}
