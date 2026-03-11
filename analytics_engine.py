import sqlite3
import yfinance as yf
import pandas as pd
from collections import defaultdict
import concurrent.futures
from fundamental_analysis import get_stock_fundamentals

def get_portfolio_allocation(user_id, db_path='tracker.db'):
    """
    Aggregates holdings by Sector and calculates Sector Scores.
    """
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        
        # Get aggregated holdings for the user
        holdings = conn.execute('''
            SELECT h.symbol, SUM(h.allocation) as total_qty
            FROM holdings h
            JOIN portfolios p ON h.portfolio_id = p.id
            WHERE p.user_id = ?
            GROUP BY h.symbol
        ''', (user_id,)).fetchall()
        
        conn.close()
        
        if not holdings:
            return []

        # Data structures for aggregation
        sector_stats = defaultdict(lambda: {
            'total_value': 0.0, 
            'weighted_score_sum': 0.0, 
            'total_score_sum': 0.0, 
            'count': 0
        })
        
        # Helper to process one stock
        def process_stock(row):
            symbol = row['symbol']
            qty = row['total_qty']
            if qty <= 0: return None
            
            # Fetch fundamentals (includes Price, Sector, Health Score)
            # data dictionary keys: 'current_price', 'sector', 'health_score'
            try:
                data = get_stock_fundamentals(symbol)
                
                if data and data.get('current_price'):
                    value = data['current_price'] * qty
                    return {
                        'sector': data.get('sector', 'Unknown'),
                        'value': value,
                        'score': data.get('health_score', 0)
                    }
            except:
                pass
            return None

        # Fetch in parallel
        processed_data = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor: # Reduced workers to be safer
            results = executor.map(process_stock, holdings)
            for res in results:
                if res:
                    processed_data.append(res)
        
        # Aggregate
        total_portfolio_value = 0.0
        
        for item in processed_data:
            sec = item['sector']
            val = item['value']
            score = item['score']
            
            sector_stats[sec]['total_value'] += val
            sector_stats[sec]['weighted_score_sum'] += (score * val)
            sector_stats[sec]['total_score_sum'] += score
            sector_stats[sec]['count'] += 1
            
            total_portfolio_value += val
            
        # Final Format
        result = []
        if total_portfolio_value > 0:
            for sector, stats in sector_stats.items():
                w_avg = stats['weighted_score_sum'] / stats['total_value'] if stats['total_value'] > 0 else 0
                e_avg = stats['total_score_sum'] / stats['count'] if stats['count'] > 0 else 0
                
                result.append({
                    'sector': sector,
                    'value': stats['total_value'],
                    'percentage': round((stats['total_value'] / total_portfolio_value) * 100, 2),
                    'weighted_score': round(w_avg, 1),
                    'equal_score': round(e_avg, 1),
                    'count': stats['count']
                })
        
        # Sort by percentage desc
        result.sort(key=lambda x: x['percentage'], reverse=True)
        return result

    except Exception as e:
        print(f"Error in analytics: {e}")
        return []

   
def calculate_risk_metrics(sector_returns_series, benchmark_returns_series):
    """
    Calculates Beta and Alpha using Linear Regression.
    Returns: { 'beta': float, 'alpha': float, 'r_squared': float }
    """
    import numpy as np
    try:
        if len(sector_returns_series) < 30: return {'beta': 1.0, 'alpha': 0.0, 'r_squared': 0.0}
        
        X = np.array(benchmark_returns_series)
        y = np.array(sector_returns_series)
        
        # Calculate Beta (Slope) and Alpha (Intercept)
        covariance = np.cov(X, y)[0][1]
        variance = np.var(X)
        beta = covariance / variance
        
        alpha = np.mean(y) - (beta * np.mean(X))
        
        return {
            'beta': round(beta, 2), 
            'alpha': round(alpha * 52, 2), # Annualized Alpha (approx)
            'volatility': round(np.std(y) * np.sqrt(52), 2) # Annualized Volatility
        }
    except Exception:
        return {'beta': 1.0, 'alpha': 0.0, 'volatility': 0.0}

def calculate_sector_scores(stocks_data, mode='market_cap'):
    """
    Aggregates company scores into sector scores.
    :param stocks_data: List of dicts matching { 'sector', 'market_cap', 'overall_score', 'pillars': {...}, 'risk': {...} }
    :param mode: 'market_cap' (Weighted) or 'equal' (Simple Avg)
    """
    sectors = defaultdict(lambda: {
        'count': 0, 'total_mcap': 0, 
        'sum_score_w': 0, 'sum_score_e': 0,
        'pillars': defaultdict(lambda: {'sum_w': 0, 'sum_e': 0}),
        'risk_sums': defaultdict(lambda: {'sum_w': 0, 'sum_e': 0})
    })
    
    # Standardize Keys to what Fundamental Analysis Returns
    pillar_keys = ['Growth', 'Profitability', 'Valuation', 'Technicals']
    risk_keys = ['debt_to_equity', 'drawdown'] # We need to ensure input data has these
    
    for stock in stocks_data:
        sec = stock.get('sector', 'Unknown')
        mcap = stock.get('market_cap', 1.0) 
        score = stock.get('overall_score', 0)
        
        # Aggregate Overall
        sectors[sec]['count'] += 1
        sectors[sec]['total_mcap'] += mcap
        sectors[sec]['sum_score_w'] += (score * mcap)
        sectors[sec]['sum_score_e'] += score
        
        # Aggregate Pillars
        # Handle case sensitivity mapping if needed, but assuming strict Capitalized from get_stock_fundamentals
        stock_pillars = stock.get('pillars', {})
        for p in pillar_keys:
            # Look for exact key or lowercase variant
            p_val = stock_pillars.get(p, stock_pillars.get(p.lower(), 0))
            if isinstance(p_val, dict): p_val = p_val.get('score', 0) # Handle if passed as dict
            # Handling float values
            p_val = float(p_val) if p_val else 0.0
            
            sectors[sec]['pillars'][p]['sum_w'] += (p_val * mcap)
            sectors[sec]['pillars'][p]['sum_e'] += p_val

        # Aggregate Risk (Debt, Drawdown)
        stock_risk = stock.get('risk', {})
        for r in risk_keys:
             val = stock_risk.get(r, 0)
             if val is None: val = 0
             val = float(val)
             sectors[sec]['risk_sums'][r]['sum_w'] += (val * mcap)
             sectors[sec]['risk_sums'][r]['sum_e'] += val

    # Final Calculation
    results = []
    for sec, data in sectors.items():
        if mode == 'market_cap':
            divisor = data['total_mcap'] if data['total_mcap'] > 0 else 1
            key = 'sum_w'
        else:
            divisor = data['count'] if data['count'] > 0 else 1
            key = 'sum_e'
            
        final_pillars = {}
        for p in pillar_keys:
            final_pillars[p] = round(data['pillars'][p][key] / divisor, 0) # Round score to int
            
        final_risk = {}
        for r in risk_keys:
            final_risk[r] = round(data['risk_sums'][r][key] / divisor, 2)
            
        results.append({
            'sector': sec,
            'count': data['count'],
            'avg_mcap': round(data['total_mcap'] / data['count'], 2),
            'overall_score': round(data[f'sum_score_{key[-1]}'] / divisor, 0),
            'pillars': final_pillars,
            'risk': final_risk
        })
        
    # Sort by Score Desc
    results.sort(key=lambda x: x['overall_score'], reverse=True)
    return results

# --- TURTLE WEALTH ALGORITHMS (Master Prompt Part 4) ---

def check_ath_price(ticker_obj):
    """
    Checks if current price is within 0.1% of All-Time High.
    """
    try:
        # Fetch max history
        hist = ticker_obj.history(period="max")
        if hist.empty: return False, "No Data"
        
        # Calculate Adjusted Close Max
        max_price = hist['Close'].max()
        current_price = hist['Close'].iloc[-1]
        
        # Threshold: 99.9% of ATH
        threshold = max_price * 0.999
        is_ath = current_price >= threshold
        
        return is_ath, round(max_price, 2)
    except Exception as e:
        print(f"ATH Price Check Error: {e}")
        return False, 0

def check_ath_profit(ticker_obj):
    """
    Checks if TTM Net Income is at All-Time High.
    """
    try:
        # Get Financials (Annual + Quarterly to construct TTM)
        fin_q = ticker_obj.quarterly_financials
        
        if fin_q is None or fin_q.empty: 
            # Fallback to annual if quarterly missing
            fin_a = ticker_obj.financials
            if fin_a is None or fin_a.empty: return False
            net_income = fin_a.loc['Net Income'] if 'Net Income' in fin_a.index else pd.Series()
            if net_income.empty: return False
            
            # For annual, just check if last year is max
            # (Not strictly TTM but best proxy if only annual keys)
            current_ni = net_income.iloc[0]
            max_ni = net_income.max()
            return current_ni >= (max_ni * 0.999)

        # Construct TTM Series from Quarterly
        # TTM = Sum of last 4 quarters
        # We need historical TTMs to compare against
        net_income_q = fin_q.loc['Net Income'] if 'Net Income' in fin_q.index else pd.Series()
        # Sort index ascending date
        net_income_q = net_income_q.sort_index()
        
        # Calculate rolling 4Q sum
        ttm_series = net_income_q.rolling(window=4).sum()
        
        if ttm_series.empty: return False
        
        current_ttm = ttm_series.iloc[-1]
        historical_max_ttm = ttm_series.max()
        
        # If NaN (start of window), ignore
        if pd.isna(current_ttm): return False
        
        return current_ttm >= (historical_max_ttm * 0.999)

    except Exception as e:
        print(f"ATH Profit Check Error: {e}")
        return False

def calculate_outperformance(ticker_obj, benchmark_ticker='^CRSLDX'):
    """
    Checks if 12M Total Return > Nifty 500 (or proxy) Return.
    Using ^CRSLDX (Nifty 500) or ^NSEI (Nifty 50) as proxy.
    """
    try:
        # Get 1 year history for both
        stock_hist = ticker_obj.history(period="1y")
        bench_hist = yf.Ticker(benchmark_ticker).history(period="1y")
        
        if stock_hist.empty or bench_hist.empty: return False, 0
        
        # Calculate Returns: (Close_today / Close_start) - 1
        # Note: 'history' output is already adjusted for splits/dividends typically
        stock_ret = (stock_hist['Close'].iloc[-1] / stock_hist['Close'].iloc[0]) - 1
        bench_ret = (bench_hist['Close'].iloc[-1] / bench_hist['Close'].iloc[0]) - 1
        
        return stock_ret > bench_ret, round(stock_ret * 100, 1)

    except Exception:
        return False, 0

def get_turtle_metrics(symbol):
    """
    Wrapper to get all Turtle metrics for a symbol.
    """
    try:
        ticker = yf.Ticker(symbol)
        is_ath_p, max_p = check_ath_price(ticker)
        is_ath_profit = check_ath_profit(ticker)
        is_outperforming, ret_12m = calculate_outperformance(ticker)
        
        return {
            'is_ath_price': is_ath_p,
            'max_price': max_p,
            'is_ath_profit': is_ath_profit,
            'is_outperforming': is_outperforming,
            'return_12m': ret_12m
        }
    except Exception as e:
        print(f"Turtle Metrics Error for {symbol}: {e}")
        return {}
        
def calculate_portfolio_metrics(user_id, db_path='tracker.db'):
    """
    Legacy placeholder for backward compatibility.
    """
    return {
        'beta': 1.0, 
        'alpha': 0.0, 
        'volatility': 'Moderate'
    }
