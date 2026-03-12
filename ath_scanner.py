import sqlite3
import pandas as pd
import yfinance as yf
import time
from datetime import datetime
import logging
from tqdm import tqdm
import os

# Suppress yfinance noisy output
logger = logging.getLogger('yfinance')
logger.setLevel(logging.CRITICAL)

# Use ABSOLUTE PATH to ensure we hit the correct DB
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get('DATABASE_PATH', os.path.join(BASE_DIR, 'tracker.db'))
BATCH_SIZE = 25

# Scanner progress state (Persistent via DB)
from scanner_status import ScannerStatusManager
status_manager = ScannerStatusManager()

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
            df = pd.DataFrame()
        conn.close()
        return df

    def run_daily_scan(self, progress_callback=None, user_id=None):
        """
        WEEKDAY MODE: Fast Batch Scan.
        Returns a list of NEW ATH hits.
        """
        def update_status(progress, total, message, running=True):
            if progress_callback:
                progress_callback(progress, total, message)
            if user_id:
                status_manager.set_status(user_id, running, progress, total, message)

        update_status(0, 0, "Initializing Scan...")
        
        df_db = self.get_tracked_tickers()
        if df_db.empty:
            update_status(0, 0, "No tickers to scan", running=False)
            return []

        total_tickers = len(df_db)
        update_status(0, total_tickers, "Starting Scan...")
        
        df_db['yf_symbol'] = df_db.apply(lambda row: f"{row['symbol']}.NS" if 'NSE' in str(row.get('exchange', 'NSE')).upper() else f"{row['symbol']}.BO", axis=1)
        ticker_map = df_db.set_index('yf_symbol').to_dict('index')
        tickers_to_fetch = list(ticker_map.keys())
        
        new_ath_list = []
        processed_count = 0
        
        batches = range(0, len(tickers_to_fetch), BATCH_SIZE)
        
        # Open audit log once to avoid repeated overhead
        audit_log = os.path.join(BASE_DIR, 'db_update_audit.log')
        
        for i in tqdm(batches, desc="Scanning Batches", unit="batch"):
            batch = tickers_to_fetch[i : i + BATCH_SIZE]
            update_status(processed_count, total_tickers, f"Processing batch {i//BATCH_SIZE + 1} ({processed_count}/{total_tickers})")
            
            data = pd.DataFrame()
            for attempt in range(3):
                try:
                    data = yf.download(batch, period="1mo", interval="1d", auto_adjust=False, actions=True, progress=False)
                    if not data.empty: break
                except:
                    time.sleep(2)
            
            if data.empty:
                processed_count += len(batch)
                continue

            # Process Batch results
            conn = self._get_conn()
            try:
                for full_ticker in batch:
                    if full_ticker not in ticker_map: continue
                    
                    try:
                        found_in_batch = False
                        todays_high = None
                        todays_date = None
                        
                        try:
                            if isinstance(data.columns, pd.MultiIndex):
                                if full_ticker in data.columns.get_level_values(1):
                                    highs_series = data['High'][full_ticker]
                                    found_in_batch = True
                            else:
                                if len(batch) == 1:
                                    highs_series = data['High']
                                    found_in_batch = True

                            if found_in_batch:
                                valid_highs = highs_series.dropna()
                                if not valid_highs.empty:
                                    todays_high = float(valid_highs.iloc[-1])
                                    todays_date = valid_highs.index[-1]
                        except:
                            found_in_batch = False

                        # Single fetch fallback
                        if not found_in_batch:
                            for _ in range(2):
                                try:
                                    single_data = yf.download(full_ticker, period="5d", interval="1d", auto_adjust=False, progress=False, timeout=10)
                                    if not single_data.empty:
                                        h = single_data['High'][full_ticker] if isinstance(single_data.columns, pd.MultiIndex) else single_data['High']
                                        if not h.empty:
                                            todays_high = float(h.iloc[-1])
                                            todays_date = h.index[-1]
                                            found_in_batch = True
                                            break
                                except: time.sleep(1)

                        if not found_in_batch or todays_high is None: continue

                        row_data = ticker_map[full_ticker]
                        raw_ath = row_data.get('ath_price')
                        current_ath = float(raw_ath) if raw_ath is not None and not pd.isna(raw_ath) and str(raw_ath).strip() != "" else 0.0
                        db_date = str(row_data.get('ath_date', ''))

                        is_new_high = False
                        if round(todays_high, 2) > round(current_ath, 2):
                            is_new_high = True
                        elif round(todays_high, 2) == round(current_ath, 2) and current_ath > 0:
                            if todays_date.strftime('%Y-%m-%d') > db_date:
                                is_new_high = True

                        if is_new_high:
                            # Update DB using reused connection
                            date_str = todays_date.strftime('%Y-%m-%d')
                            symbol = row_data['symbol']
                            conn.execute('''
                                UPDATE ath_tracking_table 
                                SET ath_price = ?, ath_date = ?, last_updated = ?
                                WHERE symbol = ?
                            ''', (todays_high, date_str, int(time.time()), symbol))
                            
                            with open(audit_log, "a") as f:
                                f.write(f"[{time.ctime()}] SCANNER SUCCESS: {symbol} detected ATH at {todays_high} on {date_str}\n")
                            
                            outperformance = ((todays_high / current_ath) - 1) * 100 if current_ath > 0 else 0
                            new_ath_list.append({
                                'symbol': symbol,
                                'prev_ath': current_ath,
                                'new_ath': todays_high,
                                'date': date_str,
                                'outperformance': round(outperformance, 2)
                            })
                    except: pass
                conn.commit()
            finally:
                conn.close()
            processed_count += len(batch)
            
        update_status(total_tickers, total_tickers, "Scan Complete", running=False)
        return new_ath_list

    def run_weekend_refresh(self, progress_callback=None, user_id=None):
        """
        WEEKEND MODE: Deep Clean (Hybrid Strategy).
        """
        def update_status(progress, total, message, running=True):
            if progress_callback:
                progress_callback(progress, total, message)
            if user_id:
                status_manager.set_status(user_id, running, progress, total, message)

        update_status(0, 0, "Starting Deep Clean...")
        df_db = self.get_tracked_tickers()
        total_tickers = len(df_db)
        update_status(0, total_tickers, "Initializing Deep Clean...")
        
        updated_count = 0
        conn = self._get_conn()
        try:
            for i, row in tqdm(df_db.iterrows(), total=total_tickers, desc="Refreshing History", unit="ticker"):
                update_status(i + 1, total_tickers, f"Refreshing: {row['symbol']}")
                    
                suffix = ".NS" if 'NSE' in str(row.get('exchange', 'NSE')).upper() else ".BO"
                full_ticker = f"{row['symbol']}{suffix}"
                
                res = self.fetch_full_history_single(full_ticker)
                if res:
                    price, date = res
                    conn.execute('''
                        UPDATE ath_tracking_table 
                        SET ath_price = ?, ath_date = ?, last_updated = ?
                        WHERE symbol = ?
                    ''', (float(price), date, int(time.time()), row['symbol']))
                    updated_count += 1
                
                if (i + 1) % 10 == 0: conn.commit() # Commit every 10 tickers
                time.sleep(0.1)
            conn.commit()
        finally:
            conn.close()
        
        update_status(total_tickers, total_tickers, f"Deep Clean Complete. Updated {updated_count} stocks.", running=False)
        return updated_count

    def fetch_full_history_single(self, full_ticker):
        backoff = [2, 5, 10]
        for attempt in backoff:
            try:
                df_monthly = yf.download(full_ticker, period="max", interval="1mo", auto_adjust=False, progress=False, timeout=20)
                if df_monthly.empty: return None
                highs_monthly = df_monthly['High'][full_ticker] if isinstance(df_monthly.columns, pd.MultiIndex) else df_monthly['High']
                ath_price = float(highs_monthly.max())
                ath_month_start = highs_monthly.idxmax()
                
                start_date = ath_month_start
                end_date = ath_month_start + pd.DateOffset(days=32)
                df_daily = yf.download(full_ticker, start=start_date, end=end_date, interval="1d", auto_adjust=False, progress=False, timeout=20)
                
                if df_daily.empty:
                    return ath_price, ath_month_start.strftime('%Y-%m-%d')
                
                highs_daily = df_daily['High'][full_ticker] if isinstance(df_daily.columns, pd.MultiIndex) else df_daily['High']
                final_date = highs_daily.idxmax()
                return ath_price, final_date.strftime('%Y-%m-%d')
            except: time.sleep(attempt)
        return None
