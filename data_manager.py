import sqlite3
import yfinance as yf
import pandas as pd
import numpy as np
import json
import time
import math
from datetime import datetime, timedelta


def _safe_json_default(obj):
    """Custom JSON serializer for numpy/pandas types that json.dumps can't handle."""
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return float(obj)
    if isinstance(obj, (np.ndarray,)):
        return obj.tolist()
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, (pd.Timestamp,)):
        return obj.isoformat()
    if isinstance(obj, (datetime,)):
        return obj.isoformat()
    if isinstance(obj, (np.datetime64,)):
        return pd.Timestamp(obj).isoformat()
    return str(obj)

class DataManager:
    """
    Centralized data hub for StockTrackerApp.
    Handles caching, batch fetching, and TTL management for market data.
    """
    
    def __init__(self, db_path='tracker.db'):
        self.db_path = db_path
        self._init_db()

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        return conn

    def _init_db(self):
        """Initialize cache tables."""
        conn = self._get_conn()
        cursor = conn.cursor()
        
        # Price Cache: symbol, price, change_pct, last_updated
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS stock_price_cache (
                symbol TEXT PRIMARY KEY,
                price REAL,
                change_pct REAL,
                last_updated INTEGER
            )
        ''')
        
        # Fundamentals Cache: symbol, data_json, last_updated
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS stock_fundamental_cache (
                symbol TEXT PRIMARY KEY,
                data_json TEXT,
                last_updated INTEGER
            )
        ''')
        
        conn.commit()
        conn.close()

    def get_prices(self, symbols, max_age_seconds=300):
        """
        Fetch prices for a list of symbols. 
        Uses cache if data is fresh, otherwise fetches batch from yfinance.
        """
        if not symbols:
            return {}
            
        now = int(time.time())
        symbols = [s.upper() for s in symbols]
        
        conn = self._get_conn()
        placeholders = ','.join(['?'] * len(symbols))
        query = f"SELECT symbol, price, change_pct, last_updated FROM stock_price_cache WHERE symbol IN ({placeholders})"
        
        cached_data = pd.read_sql_query(query, conn, params=symbols)
        conn.close()
        
        # Identify stale or missing symbols
        cached_symbols = cached_data['symbol'].tolist()
        stale_symbols = cached_data[now - cached_data['last_updated'] > max_age_seconds]['symbol'].tolist()
        # Also treat NaN-priced symbols as stale (leftover from old yfinance bug)
        nan_symbols = cached_data[cached_data['price'].isna()]['symbol'].tolist()
        missing_symbols = [s for s in symbols if s not in cached_symbols]
        
        to_fetch = list(set(stale_symbols + missing_symbols + nan_symbols))
        
        if to_fetch:
            self._refresh_prices_batch(to_fetch)
            # Re-fetch from DB after refresh
            conn = self._get_conn()
            cached_data = pd.read_sql_query(query, conn, params=symbols)
            conn.close()
            
        # Convert to dictionary format (with NaN protection)
        result = {}
        for _, row in cached_data.iterrows():
            price = row['price']
            change = row['change_pct']
            # Guard against NaN values still in cache
            if pd.isna(price) or price is None:
                price = 'N/A'
            if pd.isna(change) or change is None:
                change = 0
            result[row['symbol']] = {
                'price': price,
                'change_pct': change,
                'last_updated': row['last_updated']
            }

            
        # Fill in any absolute failures with N/A
        for s in symbols:
            if s not in result:
                result[s] = {'price': 'N/A', 'change_pct': 0, 'last_updated': 0}
                
        return result

    def _extract_scalar(self, val):
        """Safely extract a scalar float from a yfinance value (may be Series, numpy, or scalar)."""
        if isinstance(val, pd.Series):
            val = val.iloc[0] if len(val) > 0 else None
        if val is None:
            return None
        try:
            f = float(val)
            return None if (math.isnan(f) or math.isinf(f)) else f
        except (TypeError, ValueError):
            return None

    def _refresh_prices_batch(self, symbols):
        """Fetch multiple tickers at once using yfinance download.
        Handles both old and new yfinance multi-index column formats."""
        if not symbols:
            return

        print(f"DEBUG: Refreshing prices for {len(symbols)} tickers...")
        yf_tickers = [f"{s}.NS" for s in symbols]

        try:
            # period="1d" is fastest for current price/change
            data = yf.download(yf_tickers, period="1d", interval="1m", progress=False)

            if data is None or data.empty:
                print("DEBUG: yf.download returned empty data")
                return

            conn = self._get_conn()
            now = int(time.time())

            for s in symbols:
                try:
                    ticker_key = f"{s}.NS"
                    price = None
                    open_price = None

                    if data.columns.nlevels == 2:
                        # Multi-index columns: could be (Ticker, PriceType) or (PriceType, Ticker)
                        level0_vals = data.columns.get_level_values(0).unique().tolist()

                        if ticker_key in level0_vals:
                            # Structure: (Ticker, PriceType)  — new yfinance format
                            ticker_data = data[ticker_key]
                            if not ticker_data.empty:
                                price = self._extract_scalar(ticker_data['Close'].iloc[-1])
                                open_price = self._extract_scalar(ticker_data['Open'].iloc[0])
                        elif 'Close' in level0_vals:
                            # Structure: (PriceType, Ticker)  — old format with group_by='ticker'
                            try:
                                price = self._extract_scalar(data['Close'][ticker_key].iloc[-1])
                                open_price = self._extract_scalar(data['Open'][ticker_key].iloc[0])
                            except KeyError:
                                pass
                    elif data.columns.nlevels == 1:
                        # Flat columns — single ticker download (older yfinance)
                        if 'Close' in data.columns:
                            price = self._extract_scalar(data['Close'].iloc[-1])
                            open_price = self._extract_scalar(data['Open'].iloc[0])

                    if price is None or price <= 0:
                        print(f"DEBUG: Skipping {s} — no valid price")
                        continue

                    change_pct = 0.0
                    if open_price and open_price > 0:
                        change_pct = ((price / open_price) - 1) * 100

                    conn.execute('''
                        INSERT OR REPLACE INTO stock_price_cache (symbol, price, change_pct, last_updated)
                        VALUES (?, ?, ?, ?)
                    ''', (s, round(price, 2), round(change_pct, 2), now))
                except Exception as e:
                    print(f"Error processing batch item {s}: {e}")

            conn.commit()
            conn.close()
        except Exception as e:
            print(f"Batch fetch failed: {e}")

    def get_fundamentals(self, symbol, max_age_days=14):
        """
        Get fundamental data for a symbol.
        Cached for long periods as financials only change quarterly.
        Returns a dict: {'info': ..., 'financials': ..., 'cashflow': ..., 'balance_sheet': ...}
        """
        symbol = symbol.upper()
        now = int(time.time())
        max_age_seconds = max_age_days * 86400

        conn = self._get_conn()
        try:
            row = conn.execute("SELECT data_json, last_updated FROM stock_fundamental_cache WHERE symbol = ?", (symbol,)).fetchone()

            if row and (now - row['last_updated'] < max_age_seconds):
                data = json.loads(row['data_json'])
                # Convert back to DataFrames for compatibility with existing logic
                for key in ['financials', 'cashflow', 'balance_sheet']:
                    if key in data and data[key]:
                        data[key] = pd.DataFrame(data[key])
                return data

            # Missing or stale - fetch fresh
            print(f"DEBUG: Fetching fresh fundamentals for {symbol}...")
            try:
                ticker = yf.Ticker(f"{symbol}.NS")

                # Fetch all in one go
                info = ticker.info
                fin = ticker.financials
                cf = ticker.cashflow
                bs = ticker.balance_sheet

                # Serialize DataFrames to JSON-safe dicts (convert Timestamp keys to strings)
                def _df_to_safe_dict(df):
                    if df is None or df.empty:
                        return None
                    safe = {}
                    for col in df.columns:
                        col_key = col.isoformat() if isinstance(col, (pd.Timestamp, datetime)) else str(col)
                        safe[col_key] = {}
                        for idx in df.index:
                            val = df.loc[idx, col]
                            if isinstance(val, (np.integer,)):
                                val = int(val)
                            elif isinstance(val, (np.floating,)):
                                val = None if (math.isnan(val) or math.isinf(val)) else float(val)
                            safe[col_key][str(idx)] = val
                    return safe

                data = {
                    'info': info,
                    'financials': _df_to_safe_dict(fin),
                    'cashflow': _df_to_safe_dict(cf),
                    'balance_sheet': _df_to_safe_dict(bs)
                }

                # Save to cache using safe serializer
                conn.execute('''
                    INSERT OR REPLACE INTO stock_fundamental_cache (symbol, data_json, last_updated)
                    VALUES (?, ?, ?)
                ''', (symbol, json.dumps(data, default=_safe_json_default), now))
                conn.commit()

                # Return with DataFrames for compatibility
                data['financials'] = fin if fin is not None else pd.DataFrame()
                data['cashflow'] = cf if cf is not None else pd.DataFrame()
                data['balance_sheet'] = bs if bs is not None else pd.DataFrame()
                return data

            except Exception as e:
                print(f"Failed to fetch fundamentals for {symbol}: {e}")
                if row:  # Return stale data if fetch fails
                    data = json.loads(row['data_json'])
                    for key in ['financials', 'cashflow', 'balance_sheet']:
                        if key in data and data[key]:
                            data[key] = pd.DataFrame(data[key])
                    return data
                return None
        finally:
            conn.close()

    def get_history(self, symbol, period="1y", max_age_hours=24):
        """
        Get price history for a symbol. Cached for 24 hours.
        Returns a DataFrame.
        """
        symbol = symbol.upper()
        now = int(time.time())
        max_age_seconds = max_age_hours * 3600
        
        # We'll use a separate table for history blobs
        conn = self._get_conn()
        try:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS stock_history_cache (
                    symbol TEXT PRIMARY KEY,
                    data_json TEXT,
                    last_updated INTEGER
                )
            ''')
            
            row = conn.execute("SELECT data_json, last_updated FROM stock_history_cache WHERE symbol = ?", (symbol,)).fetchone()
            
            if row and (now - row['last_updated'] < max_age_seconds):
                return pd.DataFrame(json.loads(row['data_json']))
                
            # Fetch fresh
            print(f"DEBUG: Fetching fresh history for {symbol}...")
            try:
                ticker = yf.Ticker(f"{symbol}.NS")
                hist = ticker.history(period=period)
                if hist.empty:
                    return hist
                    
                # Save to cache
                conn.execute('''
                    INSERT OR REPLACE INTO stock_history_cache (symbol, data_json, last_updated)
                    VALUES (?, ?, ?)
                ''', (symbol, json.dumps(hist.reset_index().to_dict(orient='records'), default=_safe_json_default), now))
                conn.commit()
                return hist
            except Exception as e:
                print(f"Failed to fetch history for {symbol}: {e}")
                if row:
                    return pd.DataFrame(json.loads(row['data_json']))
                return pd.DataFrame()
        finally:
            conn.close()

    def invalidate_cache(self, symbol=None, cache_type='all'):

        """Manually clear cache for one or all items."""
        conn = self._get_conn()
        if cache_type in ['price', 'all']:
            if symbol:
                conn.execute("DELETE FROM stock_price_cache WHERE symbol = ?", (symbol.upper(),))
            else:
                conn.execute("DELETE FROM stock_price_cache")
        if cache_type in ['fundamental', 'all']:
            if symbol:
                conn.execute("DELETE FROM stock_fundamental_cache WHERE symbol = ?", (symbol.upper(),))
            else:
                conn.execute("DELETE FROM stock_fundamental_cache")
        conn.commit()
        conn.close()
