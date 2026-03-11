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
TRACKER_DB = os.path.join(BASE_DIR, 'tracker.db')
LOOKBACK = 211
BATCH_SIZE = 50


# ====================== DATABASE HELPERS ======================

def init_scanning_results_table():
    """Create or recreate the ath_scanning_results table with the new schema."""
    conn = sqlite3.connect(TRACKER_DB)
    conn.execute("DROP TABLE IF EXISTS ath_scanning_results")
    conn.execute("""
        CREATE TABLE ath_scanning_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            new_ath_price REAL,
            trigger_price REAL,
            green_candle TEXT,
            close_gt_ath TEXT,
            ath_outperformance TEXT,
            current_rs REAL,
            ath_rs REAL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()
    logger.info("ath_scanning_results table initialized with new schema.")


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


def save_scan_results(results):
    """Clear staging table and insert fresh results."""
    conn = sqlite3.connect(TRACKER_DB)
    try:
        conn.execute("DELETE FROM ath_scanning_results")
        for r in results:
            conn.execute("""
                INSERT INTO ath_scanning_results 
                (symbol, new_ath_price, trigger_price, green_candle, close_gt_ath, ath_outperformance, current_rs, ath_rs)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (r['symbol'], r['new_ath_price'], r['trigger_price'],
                  r['green_candle'], r['close_gt_ath'], r['ath_outperformance'],
                  r.get('current_rs'), r.get('ath_rs')))
        conn.commit()
        logger.info(f"Saved {len(results)} scan results to staging table.")
    finally:
        conn.close()


def get_scan_results():
    """Fetch current results from the staging table."""
    conn = sqlite3.connect(TRACKER_DB)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM ath_scanning_results ORDER BY symbol"
        ).fetchall()
        return [dict(r) for r in rows]
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
    finally:
        conn.close()

    new_tickers = [t for t in tickers if t not in existing]

    if not new_tickers:
        logger.info("No new stocks to sync.")
        return

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
            conn.execute("""
                INSERT OR IGNORE INTO ath_tracking_table (symbol, previous_ath, exchange, last_updated)
                VALUES (?, ?, 'NSE', ?)
            """, (symbol, ath_price, time.time()))
        conn.commit()
        logger.info(f"Synced {len(new_entries)} new stocks into ATH tracker.")
    finally:
        conn.close()


# ====================== LIVE DATA ======================

def fetch_nifty_live():
    """Fetch Nifty 500 (^CRSLDX) 2-year daily data for RS calculation."""
    logger.info("Fetching Nifty 500 live data...")
    idx = yf.download("^CRSLDX", period="2y", interval="1d", progress=False, auto_adjust=True)

    if idx.empty:
        logger.warning("^CRSLDX empty, falling back to ^NSEI")
        idx = yf.download("^NSEI", period="2y", interval="1d", progress=False, auto_adjust=True)

    if idx.empty:
        raise RuntimeError("Could not fetch any Nifty index data.")

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

def calculate_single_ticker(symbol, live_candle, nifty_series, trigger_price):
    """
    Run all 4 strategy calculations for a single ticker hit.
    
    Args:
        symbol: Bare ticker (e.g. 'SBIN', no .NS suffix)
        live_candle: dict with 'High', 'Close', 'PrevClose'
        nifty_series: Pre-fetched Nifty close series
        trigger_price: The previous_ath value (== trigger price)
    
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
    #    Fetch 1y of closes live from yfinance for this specific hit
    history_closes = fetch_ticker_history_for_rs(symbol)
    
    if history_closes is None or history_closes.empty:
        # Can't calculate RS, but we still have the other 3 strategies
        return {
            'symbol': symbol,
            'new_ath_price': round(live_high, 2),
            'trigger_price': round(trigger_price, 2),
            'green_candle': green_candle,
            'close_gt_ath': close_gt_ath,
            'ath_outperformance': 'N/A',
            'current_rs': None,
            'ath_rs': None
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
        'ath_rs': rs_results.get('ath_rs')
    }


def calculate_rs_outperformance(history_closes, live_close, today_date, nifty_series):
    """
    Calculate ATH Outperformance using Rolling Fixed Anchor RS.
    Uses a 211-day lookback window.
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

        if len(aligned) < LOOKBACK:
            return {'is_outperforming': 'N/A', 'current_rs': None, 'ath_rs': None}

        # Raw ratio
        aligned['RS_Raw'] = aligned['Stock'] / aligned['Index']

        # Window = last LOOKBACK rows
        window = aligned.iloc[-LOOKBACK:]

        # Anchor value from start of window
        anchor_rs_raw = window['RS_Raw'].iloc[0]
        if anchor_rs_raw == 0:
            return {'is_outperforming': 'N/A', 'current_rs': None, 'ath_rs': None}

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
            'ath_rs': round(max_anchored, 2)
        }

    except Exception as e:
        logger.error(f"RS calculation error: {e}")
        return {'is_outperforming': 'N/A', 'current_rs': None, 'ath_rs': None}


# ====================== MAIN SCAN ORCHESTRATOR ======================

def run_full_scan(tickers, progress_callback=None):
    """
    Run the optimized full ATH scan using batch processing.
    
    Args:
        tickers: List of bare ticker symbols (no .NS)
        progress_callback: Optional fn(progress, total, message) for UI updates
    """
    total = len(tickers)
    results = []

    if progress_callback:
        progress_callback(0, total, "Initializing scanner...")

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
        sync_new_stocks_to_ath_tracker(tickers, progress_callback)
    except Exception as e:
        logger.error(f"Sync failed: {e}")

    # ── Phase 2: Load Nifty once and Get current ATH Map ──
    try:
        if progress_callback:
            progress_callback(0, total, "Fetching Nifty index data...")
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

    for i, batch in enumerate(batches):
        processed_count = i * BATCH_SIZE
        if progress_callback:
            progress_callback(processed_count, total, f"Batch {i+1}/{len(batches)}: Downloading prices...")

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
    if progress_callback:
        progress_callback(total, total, f"Detected {total_hits} potential hits. Analyzing...")

    for i, (symbol, live_candle) in enumerate(potential_hits.items()):
        try:
            if progress_callback:
                progress_callback(total, total, f"Analyzing {symbol} ({i+1}/{total_hits})")
            
            result = calculate_single_ticker(
                symbol, live_candle, nifty_series, 
                trigger_price=live_candle['TriggerPrice']
            )
            if result:
                results.append(result)
        except Exception as e:
            logger.error(f"Analysis failed for {symbol}: {e}")

    # Save to staging table
    save_scan_results(results)

    # Update today_ath for all hits
    update_today_ath(results)

    if progress_callback:
        progress_callback(total, total, f"Scan complete. {len(results)} verified ATH hits found.")

    logger.info(f"Scan complete: {len(results)} hits found from {total} tickers.")
    return results
