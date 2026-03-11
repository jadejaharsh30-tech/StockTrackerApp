"""
Complete setup for Sector Analytics.
This script will:
1. Fetch stock data for major NSE stocks
2. Run fundamental analysis on them
3. Compute sector scores
4. Calculate risk metrics
"""

import yfinance as yf
import sqlite3
from datetime import datetime, timedelta
import pandas as pd
from fundamental_analysis import get_stock_fundamentals
from sector_analytics import SectorAnalytics

# Major NSE stocks by sector
SAMPLE_STOCKS = {
    'IT': ['TCS.NS', 'INFY.NS', 'HCLTECH.NS', 'WIPRO.NS', 'TECHM.NS'],
    'Bank': ['HDFCBANK.NS', 'ICICIBANK.NS', 'SBIN.NS', 'KOTAKBANK.NS', 'AXISBANK.NS'],
    'Auto': ['MARUTI.NS', 'M&M.NS', 'TATAMOTORS.NS', 'BAJAJ-AUTO.NS', 'EICHERMOT.NS'],
    'Pharma': ['SUNPHARMA.NS', 'DRREDDY.NS', 'CIPLA.NS', 'DIVISLAB.NS', 'BIOCON.NS'],
    'FMCG': ['HINDUNILVR.NS', 'ITC.NS', 'NESTLEIND.NS', 'BRITANNIA.NS', 'DABUR.NS'],
    'Metal': ['TATASTEEL.NS', 'JSWSTEEL.NS', 'HINDALCO.NS', 'VEDL.NS', 'COALINDIA.NS'],
    'Energy': ['RELIANCE.NS', 'ONGC.NS', 'BPCL.NS', 'IOC.NS', 'NTPC.NS'],
    'Financial Services': ['BAJFINANCE.NS', 'HDFCLIFE.NS', 'SBILIFE.NS', 'HDFCAMC.NS', 'MUTHOOTFIN.NS']
}

def get_db():
    return sqlite3.connect('ath_tracker.db')

def fetch_stock_data(symbol, sector):
    """Fetch 1 year of stock data."""
    try:
        print(f"   Fetching {symbol}...")
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period='1y')
        
        if hist.empty:
            return False
        
        conn = get_db()
        for date, row in hist.iterrows():
            conn.execute('''
            INSERT OR REPLACE INTO stock_data 
            (symbol, date, open, high, low, close, volume, sector)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                symbol.replace('.NS', ''),
                date.strftime('%Y-%m-%d'),
                row['Open'],
                row['High'],
                row['Low'],
                row['Close'],
                int(row['Volume']),
                sector
            ))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"   ❌ Error: {e}")
        return False

def run_fundamental_scoring(symbol):
    """Run fundamental analysis for a stock."""
    try:
        print(f"   Scoring {symbol}...")
        data = get_stock_fundamentals(symbol.replace('.NS', ''))
        if data:
            print(f"   ✅ Score: {data.get('health_score', 'N/A')}")
            return True
        return False
    except Exception as e:
        print(f"   ❌ Error: {e}")
        return False

def main():
    print("🚀 Complete Sector Analytics Setup")
    print("=" * 60)
    
    # Step 1: Fetch stock data
    print("\n📊 Step 1: Fetching stock data...")
    fetched = 0
    for sector, symbols in SAMPLE_STOCKS.items():
        print(f"\n{sector}:")
        for symbol in symbols:
            if fetch_stock_data(symbol, sector):
                fetched += 1
    
    print(f"\n✅ Fetched data for {fetched} stocks")
    
    # Step 2: Run fundamental scoring
    print("\n📈 Step 2: Running fundamental analysis...")
    scored = 0
    for sector, symbols in SAMPLE_STOCKS.items():
        print(f"\n{sector}:")
        for symbol in symbols:
            if run_fundamental_scoring(symbol):
                scored += 1
    
    print(f"\n✅ Scored {scored} stocks")
    
    # Step 3: Compute sector scores
    print("\n🎯 Step 3: Computing sector scores...")
    analytics = SectorAnalytics()
    scores = analytics.compute_sector_scores()
    
    if scores:
        print(f"✅ Computed scores for {len(scores)} sectors:")
        for sector, data in scores.items():
            mcap_score = data['market_cap']['overall_score']
            print(f"   {sector}: {mcap_score:.1f}")
    
    # Step 4: Compute risk metrics
    print("\n📉 Step 4: Computing risk metrics...")
    for sector in scores.keys():
        if sector in SectorAnalytics.SECTOR_TICKERS:
            print(f"   {sector}...")
            try:
                analytics.compute_risk_metrics(sector)
                print(f"   ✅ Done")
            except Exception as e:
                print(f"   ⚠️ {e}")
    
    print("\n🎉 Setup complete!")
    print("   Visit: http://127.0.0.1:5000/sectors")

if __name__ == '__main__':
    main()
