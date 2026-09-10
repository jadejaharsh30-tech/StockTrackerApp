import pandas as pd
import sqlite3
import yfinance as yf
from datetime import datetime, timedelta
import time

# ================= CONFIGURATION =================
DB_NAME = "market_data_yfinance.db"
TEST_TICKERS = ["JAMNAAUTO.NS", "JINDALSTEL.NS", "LT.NS", "MFSL.NS", "SBIN.NS", "SHRIRAMFIN.NS", "TORNTPHARM.NS"]
# Anchor 212 bars back from the latest bar, as the TradingView indicator does.
# 213 rows, because slicing by row count puts the anchor one row further in.
RS_BARS_BACK = 212
LOOKBACK = RS_BARS_BACK + 1
# =================================================

def fetch_nifty_live():
    print("Fetching LIVE Nifty 500 Data...")
    # Fetch 2y history + Today's live candle
    idx = yf.download("^CRSLDX", period="2y", interval="1d", progress=False, auto_adjust=True)
    # No fallback index, deliberately — see scanner_engine.fetch_nifty_live. This
    # script exists to check the scanner's RS maths, so it must measure against
    # the same benchmark or it is checking nothing.
    if idx.empty:
        raise RuntimeError("Could not fetch ^CRSLDX (Nifty 500). RS uses this index only.")


    if isinstance(idx.columns, pd.MultiIndex): series = idx['Close'].iloc[:, 0]
    else: series = idx['Close']
    
    # Normalize index
    series.index = pd.to_datetime(series.index).normalize()
    return series

def get_live_stock_price(ticker):
    """Fetches the absolute latest 1-minute price to ensure Real-Time accuracy"""
    try:
        # Fetch 1 day of data
        df = yf.download(ticker, period="1d", interval="1d", progress=False, auto_adjust=False)
        if not df.empty:
            if isinstance(df.columns, pd.MultiIndex):
                return df['Close'].iloc[-1].iloc[0], df['High'].iloc[-1].iloc[0]
            else:
                return df['Close'].iloc[-1], df['High'].iloc[-1]
    except:
        pass
    return None, None

def test_ticker(ticker, nifty_series):
    print(f"\n" + "="*50)
    print(f"   LIVE DIAGNOSTIC TEST: {ticker}")
    print("="*50)
    
    # 1. Get History from Local DB (Past Data Only)
    conn = sqlite3.connect(DB_NAME)
    query = f"SELECT date, high, close FROM ohlcv WHERE ticker='{ticker.replace('.NS','')}' ORDER BY date DESC LIMIT 600"
    df_db = pd.read_sql(query, conn)
    conn.close()
    
    if df_db.empty:
        print("❌ No data in local DB.")
        return

    # Clean & Sort DB Data
    df_db['date'] = pd.to_datetime(df_db['date']).dt.normalize()
    df_db.set_index('date', inplace=True)
    df_db.sort_index(inplace=True) 
    
    # 2. FETCH LIVE DATA (The Fix for Static Price)
    live_close, live_high = get_live_stock_price(ticker)
    if live_close is None:
        print("❌ Could not fetch live data from Yahoo.")
        return

    # 3. Combine History + Live Today
    # We remove 'Today' from DB if it exists (to avoid duplicates) and append the Live value
    today_date = pd.to_datetime(datetime.now().date()).normalize()
    
    # Create a Series for Close prices
    history_closes = df_db['close'][df_db.index < today_date] # Strictly past data
    
    # PREVIOUS CLOSE (Requested by you)
    prev_close = history_closes.iloc[-1]
    prev_date = history_closes.index[-1].strftime('%Y-%m-%d')

    # Construct the Full Series with Live Data
    full_closes = history_closes.copy()
    full_closes[today_date] = live_close
    
    # --- TRIGGER PRICE LOGIC ---
    # Trigger = Max High of History (Excluding Today)
    history_highs = df_db['high'][df_db.index < today_date]
    trigger_price = history_highs.max()
    
    print(f"Data Date (Live):       {today_date.strftime('%Y-%m-%d')} (Time: {datetime.now().strftime('%H:%M:%S')})")
    print(f"Live Price (CMP):       {live_close:.2f}")
    print(f"Previous Close:         {prev_close:.2f} ({prev_date})")
    print(f"Trigger Price (ATH):    {trigger_price:.2f}")
    print(f"Close > ATH Check:      {'Y' if live_close > trigger_price else 'N'}")
    
    # 4. RS Calculation (Strict Alignment)
    aligned = pd.DataFrame({'Stock': full_closes, 'Index': nifty_series}).dropna()
    
    print(f"\n--- RS MATH BREAKDOWN ---")
    
    if len(aligned) < LOOKBACK:
        print(f"❌ Error: Not enough overlapping data ({len(aligned)} rows).")
        return

    # A. Raw Ratio
    aligned['RS_Raw'] = aligned['Stock'] / aligned['Index']
    
    # B. The Window (Last 212 rows)
    window = aligned.iloc[-LOOKBACK:]
    
    # C. The Anchor
    anchor_date = window.index[0]
    anchor_rs_raw = window['RS_Raw'].iloc[0]
    
    current_rs_raw = window['RS_Raw'].iloc[-1]
    
    print(f"1. Window Start (Anchor): {anchor_date.strftime('%Y-%m-%d')}")
    print(f"2. Anchor Raw Ratio:      {anchor_rs_raw:.6f}  (Stock/Index on Start Date)")
    print(f"3. Current Raw Ratio:     {current_rs_raw:.6f}  (Stock/Index Today)")
    
    # D. Anchored RS Value
    current_anchored_value = (current_rs_raw / anchor_rs_raw) * 100
    
    # E. Max of Window
    anchored_line = (window['RS_Raw'] / anchor_rs_raw) * 100
    max_rs_in_window = anchored_line.max()
    
    print(f"4. Current Anchored RS:   {current_anchored_value:.4f}")
    print(f"5. Max RS in Window:      {max_rs_in_window:.4f}")
    
    # 5. Final Strategy Checks
    green_candle = "Y" if live_close >= prev_close else "N"
    rs_outperf = "Y" if current_anchored_value >= (max_rs_in_window * 0.9999) else "N"
    
    print(f"\n--- FINAL OUTPUTS ---")
    print(f"A. Trigger Price:       {trigger_price:.2f}")
    print(f"B. ATH Outperformance:  {rs_outperf}  (Is {current_anchored_value:.2f} >= {max_rs_in_window:.2f}?)")
    print(f"C. Green Candle:        {green_candle}  (Is {live_close:.2f} >= {prev_close:.2f}?)")
    print(f"D. Close > ATH:         {'Y' if live_close > trigger_price else 'N'}")

# RUN TEST
nifty = fetch_nifty_live()
for t in TEST_TICKERS:
    test_ticker(t, nifty)