import sqlite3
import pandas as pd
import yfinance as yf
import time
from datetime import datetime, timedelta
import traceback
import logging
from tqdm import tqdm

# Suppress yfinance noisy output
logger = logging.getLogger('yfinance')
logger.setLevel(logging.CRITICAL)

import os

# Use ABSOLUTE PATH to ensure we hit the correct DB
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'tracker.db')
BATCH_SIZE = 25

# Global Progress Tracker (Simple In-Memory)
SCAN_STATUS = {
    'running': False,
    'progress': 0,
    'total': 0,
    'message': 'Idle',
    'results': [] 
}

class ATHScanner:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
    def _get_conn(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute('PRAGMA journal_mode=WAL')
        return conn

    def get_tracked_tickers(self):
        """Fetch all tickers from ath_tracking_table (Master)."""
        conn = self._get_conn()
        try:
            df = pd.read_sql_query("SELECT * FROM ath_tracking_table WHERE ignored = 0", conn)
        except:
            # Fallback if migration hasn't run yet or table mismatch
            df = pd.DataFrame()
        conn.close()
        return df

    def run_daily_scan(self, progress_callback=None):
        """
        WEEKDAY MODE: Fast Batch Scan.
        Returns a list of NEW ATH hits.
        """
        # Reset Global Status
        SCAN_STATUS['running'] = True
        SCAN_STATUS['progress'] = 0
        SCAN_STATUS['message'] = "Initializing Scan..."
        SCAN_STATUS['results'] = []
        
        df_db = self.get_tracked_tickers()
        if df_db.empty:
            SCAN_STATUS['running'] = False
            SCAN_STATUS['message'] = "No tickers found to scan."
            return []

        total_tickers = len(df_db)
        SCAN_STATUS['total'] = total_tickers
        
        print("\n" + "="*50)
        print(f"🚀 STARTING DAILY ATH SCAN: {total_tickers} Stocks")
        print("="*50 + "\n")

        # Map 'SYMBOL' -> Row Data for quick lookup
        # Add suffixes for yfinance
        # Assuming exchange column exists, else default to .NS
        df_db['yf_symbol'] = df_db.apply(lambda row: f"{row['symbol']}.NS" if 'NSE' in str(row.get('exchange', 'NSE')).upper() else f"{row['symbol']}.BO", axis=1)
        
        ticker_map = df_db.set_index('yf_symbol').to_dict('index')
        tickers_to_fetch = list(ticker_map.keys())
        
        new_ath_list = []
        processed_count = 0
        
        # Batch Loop with TQDM for Terminal
        batches = range(0, len(tickers_to_fetch), BATCH_SIZE)
        
        for i in tqdm(batches, desc="Scanning Batches", unit="batch"):
            batch = tickers_to_fetch[i : i + BATCH_SIZE]
            
            # Update Global Status
            SCAN_STATUS['progress'] = processed_count
            SCAN_STATUS['message'] = f"Processing batch {i//BATCH_SIZE + 1} ({processed_count}/{total_tickers})"
            
            # --- MANUAL RETRY LOOP ---
            data = pd.DataFrame()
            for attempt in range(3):
                try:
                    # CRITICAL: auto_adjust=False as per new requirements
                    data = yf.download(
                        batch, period="1mo", interval="1d", 
                        auto_adjust=False, actions=True, progress=False
                    )
                    if not data.empty: break
                except:
                    time.sleep(2)
            
            if data.empty:
                processed_count += len(batch)
                continue

            # Process Batch
            # Process Batch
            for full_ticker in batch:
                if full_ticker not in ticker_map: continue
                
                try:
                    # --- BATCH DATA RETRIEVAL ---
                    found_in_batch = False
                    todays_high = None
                    todays_date = None
                    
                    try:
                        # Handle MultiIndex vs SingleIndex ambiguity
                        if isinstance(data.columns, pd.MultiIndex):
                            # Multi-level: Columns are (PriceType, Ticker)
                            # Check if ticker exists in Top Level
                            if full_ticker in data.columns.get_level_values(1):
                                highs_series = data['High'][full_ticker]
                                found_in_batch = True
                        else:
                            # Single-level: Columns are PriceTypes
                            # THIS IS TRICKY: If simple index, it means results are for ONE ticker.
                            # We must verify if this batch result actually belongs to THIS full_ticker.
                            # yfinance DOES NOT provide symbol name in single-index result.
                            # Heuristic: If batch size was 1, it's ours. If batch size > 1, we can't be sure 
                            # (unless we assume yfinance only returns the valid ones and ignores others).
                            # Safer to treat SingleIndex as "Ambiguous" if batch > 1 and FALLBACK?
                            # Actually, if we pass list, yfinance usually returns MultiIndex.
                            # If only 1 valid, it MIGHT return SingleIndex. 
                            # Strategy: Try to access, if fail, fallback.
                            if len(batch) == 1:
                                highs_series = data['High']
                                found_in_batch = True
                            else:
                                # Start fallback to be safe
                                found_in_batch = False

                        if found_in_batch:
                            valid_highs = highs_series.dropna()
                            if not valid_highs.empty:
                                todays_high = valid_highs.iloc[-1]
                                todays_date = valid_highs.index[-1]
                    except:
                        found_in_batch = False

                    # --- FALLBACK RETRY (Single Fetch) ---
                    if not found_in_batch:
                        # tqdm.write(f"⚠️ Retry single fetch for {full_ticker}")
                        # Retry logic for fallback
                        for _ in range(2):
                            try:
                                single_data = yf.download(full_ticker, period="5d", interval="1d", auto_adjust=False, progress=False, timeout=10)
                                if not single_data.empty:
                                    if isinstance(single_data.columns, pd.MultiIndex):
                                        h = single_data['High'][full_ticker]
                                    else:
                                        h = single_data['High']
                                    
                                    if not h.empty:
                                        todays_high = h.iloc[-1]
                                        todays_date = h.index[-1]
                                        found_in_batch = True # Found in fallback
                                        break
                            except: 
                                time.sleep(1)

                    if not found_in_batch or todays_high is None:
                        continue

                    # --- ATH CHECK LOGIC ---
                    row_data = ticker_map[full_ticker]
                    
                    # Handle NULL/NaN ath_price
                    try:
                        raw_ath = row_data.get('ath_price')
                        current_ath = float(raw_ath) if raw_ath is not None and not pd.isna(raw_ath) and str(raw_ath).strip() != "" else 0.0
                    except:
                        current_ath = 0.0
                    
                    db_date = str(row_data.get('ath_date', ''))

                    # --- DEBUG LOGGING FOR 360ONE ---
                    if "360ONE" in full_ticker:
                        with open("scanner_trace.log", "a") as f:
                            f.write(f"[{datetime.now()}] TRACE 360ONE: TodaysHigh={todays_high}, TodaysDate={todays_date.strftime('%Y-%m-%d')}, DB_ATH={current_ath}, DB_Date={db_date}\n")

                    # --- THE COMPARISON (User requested >=) ---
                    # Only flag if:
                    # 1. Price is strictly higher OR
                    # 2. Price is equal but the date is NEWER than what's in DB
                    is_new_high = False
                    if round(todays_high, 2) > round(current_ath, 2):
                        is_new_high = True
                    elif round(todays_high, 2) == round(current_ath, 2) and current_ath > 0:
                        if todays_date.strftime('%Y-%m-%d') > db_date:
                            is_new_high = True

                    if is_new_high:
                        # Update Master DB
                        self.update_db_ath(row_data['symbol'], todays_high, todays_date.strftime('%Y-%m-%d'))
                        
                        # Add to results
                        outperformance = ((todays_high / current_ath) - 1) * 100 if current_ath > 0 else 0
                        
                        new_ath_list.append({
                            'symbol': row_data['symbol'],
                            'prev_ath': current_ath,
                            'new_ath': todays_high,
                            'date': todays_date.strftime('%Y-%m-%d'),
                            'outperformance': round(outperformance, 2)
                        })
                        
                except Exception as e:
                    pass
            
            processed_count += len(batch)
            
        SCAN_STATUS['running'] = False
        SCAN_STATUS['progress'] = total_tickers
        SCAN_STATUS['message'] = "Scan Complete"
        SCAN_STATUS['results'] = new_ath_list
        
        print("\n" + "="*50)
        print(f"✅ SCAN COMPLETE. Found {len(new_ath_list)} New ATHs.")
        print("="*50 + "\n")
        
        return new_ath_list

    def run_weekend_refresh(self, progress_callback=None):
        """
        WEEKEND MODE: Deep Clean (Hybrid Strategy).
        """
        # Reset Global Status
        SCAN_STATUS['running'] = True
        SCAN_STATUS['progress'] = 0
        SCAN_STATUS['message'] = "Starting Deep Clean..."
        SCAN_STATUS['results'] = []
        
        df_db = self.get_tracked_tickers()
        total_tickers = len(df_db)
        SCAN_STATUS['total'] = total_tickers
        
        print("\n" + "="*50)
        print(f"🧹 STARTING WEEKEND REFRESH: {total_tickers} Stocks")
        print("="*50 + "\n")
        
        updated_count = 0
        
        # Loop with progress bar
        for i, row in tqdm(df_db.iterrows(), total=total_tickers, desc="Refreshing History", unit="ticker"):
            
            # Update Status
            SCAN_STATUS['progress'] = i + 1
            SCAN_STATUS['message'] = f"Refreshing: {row['symbol']}"
                
            suffix = ".NS" if 'NSE' in str(row.get('exchange', 'NSE')).upper() else ".BO"
            full_ticker = f"{row['symbol']}{suffix}"
            
            # Use Hybrid Strategy
            res = self.fetch_full_history_single(full_ticker)
            if res:
                price, date = res
                self.update_db_ath(row['symbol'], price, date)
                updated_count += 1
                
            time.sleep(0.2) # Rate limit
        
        SCAN_STATUS['running'] = False
        SCAN_STATUS['message'] = f"Deep Clean Complete. Updated {updated_count} stocks."
        
        print("\n" + "="*50)
        print(f"✅ REFRESH COMPLETE. Synced {updated_count}/{total_tickers} stocks.")
        print("="*50 + "\n")
            
        return updated_count

    def fetch_full_history_single(self, full_ticker):
        """
        Hybrid Strategy (Optimized):
        1. Fetch MONTHLY data (period='max', interval='1mo') to find MAX High & Month.
        2. Fetch DAILY data (interval='1d') for ONLY that month to find exact Date.
        """
        backoff = [2, 5, 10]
        
        for attempt in backoff:
            try:
                # --- STEP 1: Monthly Scan (Fast) ---
                # auto_adjust=False as per requirement
                df_monthly = yf.download(full_ticker, period="max", interval="1mo", auto_adjust=False, progress=False, timeout=20)
                
                if df_monthly.empty: return None
                
                # Handle MultiIndex
                highs_monthly = df_monthly['High'][full_ticker] if isinstance(df_monthly.columns, pd.MultiIndex) else df_monthly['High']
                
                # Find Max High and its Month
                ath_price = highs_monthly.max()
                ath_month_start = highs_monthly.idxmax() # Timestamp of month start
                
                # --- STEP 2: Drill Down (Precise) ---
                start_date = ath_month_start
                end_date = ath_month_start + pd.DateOffset(days=32)
                
                df_daily = yf.download(full_ticker, start=start_date, end=end_date, interval="1d", auto_adjust=False, progress=False, timeout=20)
                
                if df_daily.empty:
                    # Fallback to monthly date if daily fetch fails
                    return ath_price, ath_month_start.strftime('%Y-%m-%d')
                
                highs_daily = df_daily['High'][full_ticker] if isinstance(df_daily.columns, pd.MultiIndex) else df_daily['High']
                
                # Find exact date
                final_date = highs_daily.idxmax()
                
                return ath_price, final_date.strftime('%Y-%m-%d')
                
            except Exception:
                time.sleep(attempt)
                
        return None

    def update_db_ath(self, symbol, price, date_str):
        conn = self._get_conn()
        now = int(time.time())
        # Update MASTER table
        conn.execute('''
            UPDATE ath_tracking_table 
            SET ath_price = ?, ath_date = ?, last_updated = ?
            WHERE symbol = ?
        ''', (float(price), date_str, now, symbol))
        conn.commit()
        conn.close()

        # --- AUDIT LOG ---
        audit_log = os.path.join(BASE_DIR, 'db_update_audit.log')
        with open(audit_log, "a") as f:
            f.write(f"[{time.ctime()}] SCANNER SUCCESS: {symbol} detected ATH at {price} on {date_str}\n")




    def get_tracked_tickers(self):
        """Fetch all tickers from ath_scanning_results that are not ignored."""
        conn = self._get_conn()
        df = pd.read_sql_query("SELECT * FROM ath_scanning_results WHERE ignored = 0", conn)
        conn.close()
        return df

    def run_daily_scan(self, progress_callback=None):
        """
        WEEKDAY MODE: Fast Batch Scan.
        Returns a list of NEW ATH hits.
        """
        # Reset Global Status
        SCAN_STATUS['running'] = True
        SCAN_STATUS['progress'] = 0
        SCAN_STATUS['message'] = "Initializing Scan..."
        
        df_db = self.get_tracked_tickers()
        total_tickers = len(df_db)
        SCAN_STATUS['total'] = total_tickers
        
        print("\n" + "="*50)
        print(f"🚀 STARTING DAILY ATH SCAN: {total_tickers} Stocks")
        print("="*50 + "\n")

        # Map 'SYMBOL' -> Row Data for quick lookup
        # Add suffixes for yfinance
        df_db['yf_symbol'] = df_db.apply(lambda row: f"{row['symbol']}.NS" if 'NSE' in str(row['exchange']).upper() or not str(row['exchange']).strip() else f"{row['symbol']}.BO", axis=1)
        
        ticker_map = df_db.set_index('yf_symbol').to_dict('index')
        tickers_to_fetch = list(ticker_map.keys())
        
        new_ath_list = []
        processed_count = 0
        
        # Batch Loop with TQDM for Terminal
        batches = range(0, len(tickers_to_fetch), BATCH_SIZE)
        
        for i in tqdm(batches, desc="Scanning Batches", unit="batch"):
            batch = tickers_to_fetch[i : i + BATCH_SIZE]
            
            # Update Global Status
            SCAN_STATUS['progress'] = processed_count
            SCAN_STATUS['message'] = f"Processing batch {i//BATCH_SIZE + 1} ({processed_count}/{total_tickers})"
            
            # --- MANUAL RETRY LOOP (From master_ath_manager.py) ---
            data = pd.DataFrame()
            for attempt in range(3):
                try:
                    data = yf.download(
                        batch, period="1mo", interval="1d", 
                        auto_adjust=True, actions=True, progress=False
                    )
                    if not data.empty: break
                except:
                    time.sleep(2)
            
            if data.empty:
                processed_count += len(batch)
                continue

            # Process Batch
            for full_ticker in batch:
                if full_ticker not in ticker_map: continue
                
                try:
                    # --- SPLIT CHECK ---
                    has_split = False
                    if 'Stock Splits' in data.columns:
                        try:
                            # Handle MultiIndex vs Single Index columns
                            splits = data['Stock Splits'][full_ticker] if isinstance(data.columns, pd.MultiIndex) else data['Stock Splits']
                            if splits.sum() > 0: has_split = True
                        except: pass
                    
                    row_data = ticker_map[full_ticker]
                    current_ath = float(row_data['ath_price']) if row_data['ath_price'] else 0.0
                    
                    if has_split:
                        msg = f"⚠️ Split detected for {full_ticker}. Repairing..."
                        SCAN_STATUS['message'] = msg
                        tqdm.write(msg) # Print above progress bar
                        
                        # Trigger deep repair
                        new_hist = self.fetch_full_history_single(full_ticker)
                        if new_hist:
                            current_ath, new_date = new_hist
                            self.update_db_ath(row_data['symbol'], current_ath, new_date)
                    
                    # --- ATH CHECK ---
                    try:
                        highs = data['High'][full_ticker] if isinstance(data.columns, pd.MultiIndex) else data['High']
                        valid_highs = highs.dropna()
                        
                        if not valid_highs.empty:
                            todays_high = valid_highs.iloc[-1]
                            todays_date = valid_highs.index[-1]
                            
                            # --- CRITICAL ROUNDING RULE ---
                            if round(todays_high, 2) >= round(current_ath, 2):
                                # It's a NEW ATH!
                                self.update_db_ath(row_data['symbol'], todays_high, todays_date.strftime('%Y-%m-%d'))
                                
                                # Add to results
                                outperformance = ((todays_high / current_ath) - 1) * 100 if current_ath > 0 else 0
                                
                                new_ath_list.append({
                                    'symbol': row_data['symbol'],
                                    'prev_ath': current_ath,
                                    'new_ath': todays_high,
                                    'date': todays_date.strftime('%Y-%m-%d'),
                                    'outperformance': round(outperformance, 2)
                                })
                                
                    except KeyError:
                        continue
                        
                except Exception as e:
                    pass
            
            processed_count += len(batch)
            
        SCAN_STATUS['running'] = False
        SCAN_STATUS['progress'] = total_tickers
        SCAN_STATUS['message'] = "Scan Complete"
        SCAN_STATUS['results'] = new_ath_list
        
        print("\n" + "="*50)
        print(f"✅ SCAN COMPLETE. Found {len(new_ath_list)} New ATHs.")
        print("="*50 + "\n")
        
        return new_ath_list

    def run_weekend_refresh(self, progress_callback=None):
        """
        WEEKEND MODE: Deep Clean (One-by-One).
        """
        # Reset Global Status
        SCAN_STATUS['running'] = True
        SCAN_STATUS['progress'] = 0
        SCAN_STATUS['message'] = "Starting Deep Clean..."
        SCAN_STATUS['results'] = []
        
        df_db = self.get_tracked_tickers()
        total_tickers = len(df_db)
        SCAN_STATUS['total'] = total_tickers
        
        print("\n" + "="*50)
        print(f"🧹 STARTING WEEKEND REFRESH: {total_tickers} Stocks")
        print("="*50 + "\n")
        
        updated_count = 0
        
        # Loop with progress bar
        for i, row in tqdm(df_db.iterrows(), total=total_tickers, desc="Refreshing History", unit="ticker"):
            
            # Update Status
            SCAN_STATUS['progress'] = i + 1
            SCAN_STATUS['message'] = f"Refreshing: {row['symbol']}"
                
            suffix = ".NS" if 'NSE' in str(row['exchange']).upper() else ".BO"
            full_ticker = f"{row['symbol']}{suffix}"
            
            res = self.fetch_full_history_single(full_ticker)
            if res:
                price, date = res
                self.update_db_ath(row['symbol'], price, date)
                updated_count += 1
                
            time.sleep(0.2) # Rate limit
        
        SCAN_STATUS['running'] = False
        SCAN_STATUS['message'] = f"Deep Clean Complete. Updated {updated_count} stocks."
        
        print("\n" + "="*50)
        print(f"✅ REFRESH COMPLETE. Synced {updated_count}/{total_tickers} stocks.")
        print("="*50 + "\n")
            
        return updated_count

    def fetch_full_history_single(self, full_ticker):
        """Downloads max history for single ticker with backoff."""
        for attempt in [2, 5, 10]:
            try:
                df = yf.download(full_ticker, period="max", auto_adjust=True, progress=False, timeout=20)
                if not df.empty:
                    # Handle MultiIndex
                    highs = df['High'][full_ticker] if isinstance(df.columns, pd.MultiIndex) else df['High']
                    return highs.max(), highs.idxmax().strftime('%Y-%m-%d')
            except:
                time.sleep(attempt)
        return None

    def update_db_ath(self, symbol, price, date_str):
        conn = self._get_conn()
        now = int(time.time())
        conn.execute('''
            UPDATE ath_scanning_results 
            SET ath_price = ?, ath_date = ?, last_updated = ?
            WHERE symbol = ?
        ''', (price, date_str, now, symbol))
        conn.commit()
        conn.close()
