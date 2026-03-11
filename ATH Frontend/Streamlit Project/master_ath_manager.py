import pandas as pd
import yfinance as yf
from datetime import datetime
import os
from tqdm import tqdm  # Progress bar
import time

# ================= CONFIGURATION =================
DATABASE_FILENAME = "ATH_Results.xlsx"
DAILY_REPORT_PREFIX = "ATH_Report_"
# Add symbols here that you want to permanently ignore
BLACKLIST = ['ISEC.NS', 'DHANI.NS'] 
BATCH_SIZE = 25  # Number of stocks to fetch in one chunk
# =================================================

def get_exchange_suffix(exchange_name):
    """Maps the 'Exchange Found' column to the actual suffix."""
    if "NSE" in str(exchange_name).upper():
        return ".NS"
    elif "BSE" in str(exchange_name).upper():
        return ".BO"
    return ""

def fetch_full_history_single(symbol):
    """
    Fetches the absolute max history for a single ticker.
    Uses Exponential Backoff and Explicit Timeouts to prevent hanging.
    """
    max_retries = 3
    # Wait times: 2s, 5s, 10s
    backoff_times = [2, 5, 10] 
    
    for attempt in range(max_retries):
        try:
            # timeout=20 ensures we don't hang forever
            df = yf.download(symbol, period="max", auto_adjust=True, progress=False, timeout=20)
            
            if not df.empty:
                if isinstance(df.columns, pd.MultiIndex):
                    try:
                        highs = df['High'][symbol]
                    except KeyError:
                        highs = df['High'].iloc[:, 0]
                else:
                    highs = df['High']
                
                return highs.max(), highs.idxmax()
            else:
                # If empty, treat as a fail (unless it's the last attempt)
                raise ValueError("Empty Data")
                
        except Exception:
            # If we have retries left, wait and try again
            if attempt < max_retries - 1:
                wait_time = backoff_times[attempt]
                # Optional: print(f"      ⚠️ Timeout/Error for {symbol}. Waiting {wait_time}s...") 
                time.sleep(wait_time)
                continue
            else:
                pass
                
    return None, None

def run_daily_update(df_db):
    """
    WEEKDAY MODE: Fast.
    Checks only today's data using Batching + Manual Retries.
    """
    print("--- ⚡ WEEKDAY MODE: Running Daily Update (Fast) ---")
    
    # 1. Prepare Ticker List
    ticker_map = {}
    tickers_to_fetch = []

    for index, row in df_db.iterrows():
        suffix = get_exchange_suffix(row['Exchange Found'])
        if suffix:
            clean_sym = str(row['Original Symbol']).strip()
            full_ticker = f"{clean_sym}{suffix}"
            
            if full_ticker in BLACKLIST:
                continue
                
            tickers_to_fetch.append(full_ticker)
            ticker_map[full_ticker] = index

    total_tickers = len(tickers_to_fetch)
    print(f"Fetching live data for {total_tickers} companies...")

    # 2. Batch Processing Loop
    today_date_str = datetime.now().strftime('%Y-%m-%d')
    new_ath_list = []
    failed_tickers = [] # Track stocks that didn't download
    
    # Calculate batches for tqdm
    batches = range(0, total_tickers, BATCH_SIZE)
    
    # Wrap loop with tqdm for a visual progress bar
    for i in tqdm(batches, desc="Processing Batches", unit="batch"):
        batch_tickers = tickers_to_fetch[i : i + BATCH_SIZE]
        
        # --- MANUAL RETRY LOOP ---
        data = pd.DataFrame()
        max_retries = 3
        
        for attempt in range(max_retries):
            try:
                # yfinance usually prints errors to stderr, we disable progress bar here to keep tqdm clean
                data = yf.download(
                    batch_tickers, 
                    period="1mo", 
                    interval="1d", 
                    auto_adjust=True, 
                    actions=True, 
                    progress=False
                )
                if not data.empty:
                    break # Success
            except Exception:
                time.sleep(2) # Wait before retry
        
        if data.empty:
            # If batch completely failed after 3 retries
            failed_tickers.extend(batch_tickers)
            continue

        # --- CHECK FOR MISSING TICKERS ---
        # Sometimes a batch succeeds but specific stocks return no data (e.g. Delisted)
        try:
            if isinstance(data.columns, pd.MultiIndex):
                # Level 1 usually contains the Ticker symbols in MultiIndex
                downloaded_symbols = set(data.columns.get_level_values(1))
            else:
                # If only 1 ticker was downloaded, it's not a MultiIndex in columns usually
                # But since we batch 100, it's almost always MultiIndex.
                # Just in case it downloaded 1 stock successfully out of 100:
                downloaded_symbols = set(batch_tickers) # Logic fallback might be needed for single column
            
            # Identify which requested tickers are NOT in the downloaded columns
            # Note: yfinance often uppercases symbols, so we ensure comparison is safe
            missing_in_batch = [t for t in batch_tickers if t not in downloaded_symbols]
            
            # However, yfinance sometimes keeps the column but fills it with NaN. 
            # Real missing check is complex, but this catches completely dropped columns.
            failed_tickers.extend(missing_in_batch)
            
        except Exception:
            pass # Downloading structure might vary, skip detailed missing check if error

        # --- PROCESS THIS BATCH ---
        for full_ticker in batch_tickers:
            if full_ticker not in ticker_map: continue
            
            # If this specific ticker is not in columns, we skip processing
            # (It was already added to failed_tickers above ideally, or will just KeyError here)
            if isinstance(data.columns, pd.MultiIndex) and full_ticker not in data.columns.get_level_values(1):
                continue

            row_idx = ticker_map[full_ticker]

            try:
                # --- A. SPLIT CHECK ---
                has_split = False
                if 'Stock Splits' in data.columns:
                    try:
                        if isinstance(data.columns, pd.MultiIndex):
                            splits = data['Stock Splits'][full_ticker]
                        else:
                            splits = data['Stock Splits']
                        
                        if splits.sum() > 0:
                            has_split = True
                    except KeyError:
                        pass

                current_ath_price = df_db.at[row_idx, 'ATH Price']

                if has_split:
                    # Print on a new line so it doesn't break the progress bar
                    tqdm.write(f"⚠️ Split detected for {full_ticker}. Repairing history...")
                    repaired_price, repaired_date = fetch_full_history_single(full_ticker)
                    if repaired_price is not None:
                        df_db.at[row_idx, 'ATH Price'] = repaired_price
                        df_db.at[row_idx, 'ATH Date'] = repaired_date.strftime('%Y-%m-%d')
                        current_ath_price = repaired_price

                # --- B. ATH CHECK ---
                try:
                    if isinstance(data.columns, pd.MultiIndex):
                        ticker_highs = data['High'][full_ticker]
                    else:
                        ticker_highs = data['High']
                    
                    valid_highs = ticker_highs.dropna()
                    if valid_highs.empty: continue

                    todays_high = valid_highs.iloc[-1]
                    todays_date_obj = valid_highs.index[-1]
                    
                    # --- FIX: ROUNDING TO 2 DECIMALS ---
                    if round(todays_high, 2) >= round(current_ath_price, 2):
                        df_db.at[row_idx, 'ATH Price'] = todays_high
                        df_db.at[row_idx, 'ATH Date'] = todays_date_obj.strftime('%Y-%m-%d')
                        
                        new_ath_list.append({
                            "Symbol": df_db.at[row_idx, 'Original Symbol'],
                            "New ATH Price": todays_high,
                            "Previous ATH": current_ath_price
                        })

                except KeyError:
                    continue

            except Exception:
                continue

    # 3. Save Report
    if new_ath_list:
        print(f"\n🚀 {len(new_ath_list)} New All-Time Highs detected!")
        report_file = f"{DAILY_REPORT_PREFIX}{today_date_str}.xlsx"
        pd.DataFrame(new_ath_list).to_excel(report_file, index=False)
        print(f"   -> Report saved to: {report_file}")
    else:
        print("\nNo new ATHs today.")
    
    # 4. Report Failures
    if failed_tickers:
        print(f"\n⚠️ The following {len(failed_tickers)} stocks failed to download data:")
        print(failed_tickers)
        print("Suggestion: Check if these are delisted or correct the symbols.")

    # Save Main DB
    df_db.to_excel(DATABASE_FILENAME, index=False)
    print("Main Database updated. ✅")

def run_weekly_refresh(df_db):
    """
    WEEKEND MODE: Slow & Deep (One-by-One).
    Re-downloads full history ('period=max') for EVERY ticker.
    """
    print("--- 🧹 WEEKEND MODE: Running Full History Refresh (Deep Clean) ---")
    print("This will verify every single data point. Please wait...")

    updates_count = 0
    failed_tickers = []
    
    # Loop through all rows with progress bar
    for index, row in tqdm(df_db.iterrows(), total=df_db.shape[0], unit="ticker"):
        
        suffix = get_exchange_suffix(row['Exchange Found'])
        if not suffix: continue 
        
        clean_sym = str(row['Original Symbol']).strip()
        full_ticker = f"{clean_sym}{suffix}"

        if full_ticker in BLACKLIST:
            continue

        # Fetch fresh full history (Now with Backoff & Timeout)
        new_price, new_date = fetch_full_history_single(full_ticker)
        
        if new_price is not None:
            df_db.at[index, 'ATH Price'] = new_price
            df_db.at[index, 'ATH Date'] = new_date.strftime('%Y-%m-%d')
            updates_count += 1
            
            # --- SPEED LIMIT ---
            # Sleep 0.2s to be gentle on Yahoo and prevent IP blocks
            time.sleep(0.2)
        else:
            failed_tickers.append(full_ticker)
            
    print(f"\nRefresh Complete! Verified {updates_count} tickers.")
    
    if failed_tickers:
        print(f"\n⚠️ The following stocks failed during refresh:")
        print(failed_tickers)

    df_db.to_excel(DATABASE_FILENAME, index=False)
    print("Database is now fully synchronized with Yahoo Finance. ✅")

def main():
    print(f"--- ATH Master Script ---")
    
    if not os.path.exists(DATABASE_FILENAME):
        print(f"Error: {DATABASE_FILENAME} not found. Please run the setup script first.")
        return

    df_db = pd.read_excel(DATABASE_FILENAME)
    
    today_weekday = datetime.today().weekday()
    
    if today_weekday >= 5:
        run_weekly_refresh(df_db)
    else:
        run_daily_update(df_db)

if __name__ == "__main__":
    main()