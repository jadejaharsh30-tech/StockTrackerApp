
import sqlite3
import pandas as pd
import yfinance as yf
from datetime import date, timedelta
import json
import statistics
import os

DATABASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tracker.db')

def get_db():
    conn = sqlite3.connect(DATABASE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    return conn

class MarketStats:
    """
    Engine for calculating market-wide breadth and ranking metrics.
    Designed for the Turtle Research Dashboard.
    """
    
    @staticmethod
    def calculate_daily_stats():
        """
        Main runner function.
        1. Calculates RS Rank for all stocks.
        2. Calculates Market Breadth.
        3. Updates 'stock_analytics_snapshot' and 'market_stats_history'.
        """
        conn = get_db()
        today = date.today().isoformat()
        
        # 1. Fetch All Symbols
        # Use existing stocks from DB or Master List
        # ideally we process all active stocks in the system
        symbols_rows = conn.execute("SELECT DISTINCT symbol FROM stocks").fetchall()
        symbols = [r['symbol'] for r in symbols_rows]
        
        if not symbols:
            print("No symbols to process.")
            conn.close()
            return
            
        print(f"Processing Market Stats for {len(symbols)} symbols...")
        
        # 2. Bulk Fetch Data (Optimized with threading if needed, here simple loop for MVP)
        # We need 1 year history for RS Rank
        
        snapshots = []
        breadth_metrics = {
            'total': 0,
            'at_ath_price': 0,
            'above_200dma': 0
        }
        
        for symbol in symbols:
            try:
                ticker = yf.Ticker(f"{symbol}.NS")
                # Fetch 1y + buffer
                hist = ticker.history(period="1y")
                
                if hist.empty: 
                    continue
                
                current_price = hist['Close'].iloc[-1]
                
                # --- RS Rank Calculation (12m Performance) ---
                # Simple return for now: (Current - YearAgo) / YearAgo
                if len(hist) > 200:
                    price_1y_ago = hist['Close'].iloc[0]
                    rs_raw = (current_price - price_1y_ago) / price_1y_ago
                else:
                    rs_raw = 0 # Not enough data
                
                # --- Technical Checks ---
                # ATH Check (52w High for proxy if max not avail, or use 'max' if affordable)
                # Let's use 1y high as proxy for speed, or max if possible. 
                # Strict Turtle sets "ATH" as All Time High.
                # For MVP speed, we use 52W High as "ATH" proxy unless we have full history.
                result_52w_high = hist['High'].max()
                is_ath = current_price >= (result_52w_high * 0.98) # Within 2% of 52w High
                if is_ath: breadth_metrics['at_ath_price'] += 1
                
                # 200 DMA
                if len(hist) > 200:
                    ma_200 = hist['Close'].rolling(window=200).mean().iloc[-1]
                    is_above_200 = current_price > ma_200
                    if is_above_200: breadth_metrics['above_200dma'] += 1
                else:
                    is_above_200 = False

                # Trend State
                trend = "UP" if is_above_200 else "DOWN"
                if is_ath: trend = "BREAKOUT"
                
                # Fetch latest score from scoring_history
                score_row = conn.execute(
                    "SELECT overall_score, tech_score FROM scoring_history WHERE symbol = ? ORDER BY calc_date DESC LIMIT 1",
                    (symbol,)
                ).fetchone()
                
                overall_score = score_row['overall_score'] if score_row else 0
                tech_score = score_row['tech_score'] if score_row else 0
                
                snapshots.append({
                    'date': today,
                    'symbol': symbol,
                    'rs_raw': rs_raw, # We will rank these later
                    'trend_state': trend,
                    'overall_score': overall_score,
                    'tech_score': tech_score,
                    'ath_flag': 'YES' if is_ath else 'NO',
                    'sector': 'Unknown' # Placeholder, needs sector mapping
                })
                
                breadth_metrics['total'] += 1
                
            except Exception as e:
                print(f"Error processing {symbol}: {e}")
                
        # 3. Calculate Percentile Ranks for RS
        if snapshots:
            df = pd.DataFrame(snapshots)
            df['rs_rank'] = df['rs_raw'].rank(pct=True) * 99 # 0-99
            
            # Update Snapshot Table
            for _, row in df.iterrows():
                conn.execute('''
                    INSERT OR REPLACE INTO stock_analytics_snapshot 
                    (date, symbol, rs_rank, trend_state, overall_score, tech_score, ath_flag, sector)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    row['date'], row['symbol'], row['rs_rank'], row['trend_state'], 
                    row['overall_score'], row['tech_score'], row['ath_flag'], row['sector']
                ))

        # 4. Calculate Broad Metrics
        total = breadth_metrics['total']
        if total > 0:
            ath_pct = (breadth_metrics['at_ath_price'] / total) * 100
            dma_pct = (breadth_metrics['above_200dma'] / total) * 100
            
            # Simple Regime Logic
            regime = "NEUTRAL"
            if dma_pct > 60 and ath_pct > 10: regime = "BULLISH (Trend-Friendly)"
            elif dma_pct < 40: regime = "BEARISH"
            
            # Update History Table
            conn.execute('''
                INSERT OR REPLACE INTO market_stats_history (date, ath_price_pct, ath_profit_pct, above_200dma, regime)
                VALUES (?, ?, ?, ?, ?)
            ''', (today, ath_pct, 0, dma_pct, regime))
            
        conn.commit()
        conn.close()
        print("Market Stats Calculation Complete.")

    @staticmethod
    def get_dashboard_data():
        """
        Fetches cached data for the frontend.
        Returns:
            - kpi: dict of latest breadth metrics
            - sparklines: list of dicts for sparkline charts
            - qualifiers: list of top stocks
        """
        conn = get_db()
        today = date.today().isoformat()
        
        # Latest KPIs
        kpi_row = conn.execute("SELECT * FROM market_stats_history ORDER BY date DESC LIMIT 1").fetchone()
        kpi = dict(kpi_row) if kpi_row else None
        
        # Sparklines (Last 30 days)
        history_rows = conn.execute("SELECT * FROM market_stats_history ORDER BY date ASC LIMIT 30").fetchall()
        sparklines = [dict(r) for r in history_rows]
        
        # Qualifiers Table (Top 50 by Score)
        qualifiers_rows = conn.execute('''
            SELECT * FROM stock_analytics_snapshot 
            WHERE date = (SELECT MAX(date) FROM stock_analytics_snapshot)
            ORDER BY overall_score DESC LIMIT 50
        ''').fetchall()
        qualifiers = [dict(r) for r in qualifiers_rows]
        
        conn.close()
        return kpi, sparklines, qualifiers
