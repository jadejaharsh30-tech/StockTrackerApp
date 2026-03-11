import yfinance as yf
import pandas as pd
import numpy as np
import traceback
import os
from sector_manager import SectorManager
from data_manager import DataManager

# Initialize Managers with absolute DB path
_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tracker.db')
sector_manager = SectorManager()
data_manager = DataManager(db_path=_DB_PATH)

def get_safe_float(val, default=0.0):
    """Safely converts value to float."""
    try:
        if val is None: return default
        return float(val)
    except:
        return default

def calculate_cagr(end_val, start_val, years):
    """Calculates CAGR safely."""
    if start_val is None or end_val is None or start_val <= 0 or years <= 0:
        return 0.0
    try:
        return ((end_val / start_val) ** (1 / years)) - 1
    except:
        return 0.0

def calculate_std_dev(series):
    """Calculates population standard deviation."""
    try:
        if not series or len(series) < 2: return 0.0
        return np.std(series)
    except:
        return 0.0

def calculate_rsi(series, period=14):
    """Calculate Relative Strength Index (RSI)."""
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def get_technical_analysis(symbol, period="1y"):
    """
    Calculates RSI, SMA50, SMA100, SMA200, MACD, Drawdown, Swing Low.
    Uses DataManager for cached history.
    """
    try:
        hist = data_manager.get_history(symbol, period=period)
        if hist.empty or len(hist) < 200:
            return None
        
        # Ensure 'Date' is index if it was reset during JSON serialization
        if 'Date' in hist.columns:
            hist['Date'] = pd.to_datetime(hist['Date'])
            hist.set_index('Date', inplace=True)
            
        # Indicators
        hist['SMA50'] = hist['Close'].rolling(window=50).mean()
        hist['SMA100'] = hist['Close'].rolling(window=100).mean()
        hist['SMA200'] = hist['Close'].rolling(window=200).mean()
        hist['RSI'] = calculate_rsi(hist['Close'])
        
        # MACD
        exp12 = hist['Close'].ewm(span=12, adjust=False).mean()
        exp26 = hist['Close'].ewm(span=26, adjust=False).mean()
        macd = exp12 - exp26
        signal = macd.ewm(span=9, adjust=False).mean()
        
        current_close = hist['Close'].iloc[-1]
        
        # Swing Low (Lowest Low of last 60 days / 3 months)
        if len(hist) >= 60:
            recent_low = hist['Low'].tail(60).min()
        else:
            recent_low = hist['Low'].min()
            
        dist_swing_low = ((current_close - recent_low) / recent_low) * 100
        
        # Drawdown from 52W High (Window = 252 days)
        high_52w = hist['High'].tail(252).max() if len(hist) >= 252 else hist['High'].max()
        drawdown = ((current_close - high_52w) / high_52w) * 100
        
        # Returns
        ret_6m = ((current_close / hist['Close'].iloc[-126]) - 1) * 100 if len(hist) > 126 else 0
        ret_12m = ((current_close / hist['Close'].iloc[0]) - 1) * 100 # Approx if period=1y
        
        # Sparkline Data (Last 30 days, normalized to 0-100 logic or just raw)
        # We'll pass raw last 30 closes for Sparkline.js or similar
        sparkline = hist['Close'].tail(30).tolist()
        
        return {
            'current_price': current_close,
            'sma50': hist['SMA50'].iloc[-1],
            'sma100': hist['SMA100'].iloc[-1],
            'sma200': hist['SMA200'].iloc[-1],
            'rsi': hist['RSI'].iloc[-1],
            'macd': macd.iloc[-1],
            'macd_signal': signal.iloc[-1],
            'dist_swing_low': dist_swing_low,
            'high_52w': high_52w,
            'drawdown': drawdown,
            'return_6m': ret_6m,
            'return_12m': ret_12m,
            'sparkline': sparkline
        }
    except Exception as e:
        print(f"Error in technicals: {e}")
        return None

def get_stock_fundamentals(symbol, custom_weights=None):
    """
    Fetches fundamental data and calculates a score based on the 4-Pillar framework.
    Now supports Granular Factor Weights (Checklist 2.4).
    """
    symbol = symbol.upper()
    
    # Use DataManager to get all cached data at once
    master_data = data_manager.get_fundamentals(symbol)
    if not master_data:
        return None
        
    info = master_data.get('info', {})
    financials = master_data.get('financials', pd.DataFrame())
    cashflow = master_data.get('cashflow', pd.DataFrame())
    balance_sheet = master_data.get('balance_sheet', pd.DataFrame())
    
    # Get current price from price cache (more frequent TTL)
    price_data = data_manager.get_prices([symbol]).get(symbol, {})
    current_price = price_data.get('price', info.get('currentPrice', info.get('regularMarketPrice', 0)))

    if not info or current_price == 0 or current_price == 'N/A':
        return None

    sector = info.get('sector', 'Unknown')
    industry = info.get('industry', 'Unknown')
    
    # BFSI Detection
    is_bfsi = sector in ['Financial Services', 'Real Estate'] or 'Bank' in industry or 'Insurance' in industry
    
    # --- WEIGHT CONFIGURATION ---
    # Default granular weights (Total Points per Pillar need not sum to 100 here, normalization happens later)
    default_weights = {
        # Growth Factors
        'G_Rev': 35, 'G_EPS': 35, 'G_OCF': 20, 'G_Cons': 10,
        # Quality Factors
        'Q_ROE': 30, 'Q_Mar': 20, 'Q_Deb': 20, 'Q_Sta': 15, 'Q_Cas': 15,
        # Value Factors
        'V_PEG': 15, 'V_PER': 30, 'V_EV': 20, 'V_PB': 10,
        # Technical Factors
        'T_Tre': 35, 'T_Mom': 30, 'T_RSI': 15, 'T_Dra': 10, 'T_Exi': 10,
        # Pillar Weights (Used for Final weighted sum)
        'Growth': 30, 'Profitability': 25, 'Valuation': 20, 'Technicals': 25
    }
    
    # Merge custom weights if provided
    weights = default_weights.copy()
    if custom_weights:
        for k, v in custom_weights.items():
            if v is not None:
                 try:
                    weights[k] = float(v)
                 except: 
                    pass # Ignore invalid inputs

    # --- SCORING CONTAINERS ---
    scores = {'Growth': 0, 'Profitability': 0, 'Valuation': 0, 'Technicals': 0}
    max_scores = {'Growth': 0, 'Profitability': 0, 'Valuation': 0, 'Technicals': 0} # Track max possible points for normalization
    
    breakdown = {'Growth': [], 'Profitability': [], 'Valuation': [], 'Technicals': []}

    # Helper function to add score item
    def add_score(pillar, key, metric, value_str, tooltip, score_val, max_val, is_good, range_str):
        breakdown[pillar].append({
            'metric': metric,
            'value': value_str,
            'tooltip': tooltip,
            'score': round(score_val, 1),
            'max': max_val,
            'good': is_good,
            'range': range_str
        })
        scores[pillar] += score_val
        max_scores[pillar] += max_val

    # --- PILLAR 1: GROWTH (Checklist 1.2, 1.3) ---
    # 1.1 Revenue / Loan Book CAGR
    rev_label = "Loan Book CAGR" if is_bfsi else "Revenue CAGR"
    rev_cagr = get_safe_float(info.get('revenueGrowth', 0)) * 100 # Fallback
    
    # Attempt simple CAGR from financials if available for cleaner number
    try:
        if not financials.empty and 'Total Revenue' in financials.index:
            revs = financials.loc['Total Revenue']
            curr_rev = get_safe_float(revs.iloc[0])
            prev_rev = get_safe_float(revs.iloc[min(3, len(revs)-1)])
            rev_cagr = calculate_cagr(curr_rev, prev_rev, 3) * 100
    except: pass

    r_max = weights.get('G_Rev', 35)
    r_score = 0
    r_range = ""
    
    if rev_cagr > 15: 
        r_score = r_max
        r_range = "> 15%"
    elif rev_cagr > 5: 
        r_score = r_max * 0.6 # Proportional
        r_range = "5% - 15%"
    else:
        r_range = "< 5%"
        
    add_score('Growth', 'G_Rev', rev_label, f"{rev_cagr:.1f}%", "3-Year Compound Annual Growth Rate", r_score, r_max, r_score > 0, r_range)

    # 1.2 Net Profit / EPS Growth
    eps_label = "EPS Growth"
    pat_cagr = 0.0
    try:
        if not financials.empty and 'Net Income' in financials.index:
            pats = financials.loc['Net Income']
            curr_pat = get_safe_float(pats.iloc[0])
            prev_pat = get_safe_float(pats.iloc[min(3, len(pats)-1)])
            pat_cagr = calculate_cagr(curr_pat, prev_pat, 3) * 100
        else:
             pat_cagr = get_safe_float(info.get('earningsGrowth', 0)) * 100
    except: pass
        
    e_max = weights.get('G_EPS', 35)
    e_score = 0
    e_range = ""
    
    if pat_cagr > 15: 
        e_score = e_max
        e_range = "> 15%"
    elif pat_cagr > 0: 
        e_score = e_max * 0.6
        e_range = "0% - 15%"
    else: 
        e_range = "< 0%"
        
    add_score('Growth', 'G_EPS', eps_label, f"{pat_cagr:.1f}%", "3-Year Profit Growth", e_score, e_max, e_score > 0, e_range)

    # 1.3 OCF / NII Growth
    ocf_label = "NII Growth" if is_bfsi else "OCF CAGR"
    ocf_val = 0.0
    try:
        if is_bfsi:
            ocf_val = pat_cagr # Proxy
        else:
            if not cashflow.empty and 'Operating Cash Flow' in cashflow.index:
                ocfs = cashflow.loc['Operating Cash Flow']
                curr_ocf = get_safe_float(ocfs.iloc[0])
                prev_ocf = get_safe_float(ocfs.iloc[min(3, len(ocfs)-1)])
                ocf_val = calculate_cagr(curr_ocf, prev_ocf, 3) * 100
            else:
                 ocf_val = get_safe_float(info.get('operatingCashflow', 0)) / max(1, get_safe_float(info.get('totalRevenue', 1))) * 100
    except: pass
        
    o_max = weights.get('G_OCF', 20)
    o_score = 0
    o_range = ""
    
    if ocf_val > 10: 
        o_score = o_max
        o_range = "> 10%"
    elif ocf_val > 0: 
        o_score = o_max * 0.5
        o_range = "0% - 10%"
    else: 
        o_range = "< 0%"
    
    add_score('Growth', 'G_OCF', ocf_label, f"{ocf_val:.1f}%", "Cash Flow / NII Trend", o_score, o_max, o_score > 0, o_range)

    # 1.4 Consistency
    cons_val = 5.0
    try:
         if not financials.empty and 'Total Revenue' in financials.index:
            revs = financials.loc['Total Revenue']
            pct_changes = revs.pct_change(periods=-1).dropna() * 100
            cons_val = calculate_std_dev(pct_changes)
    except:
        cons_val = 15.0

    c_max = weights.get('G_Cons', 10)
    c_score = 0
    c_range = ""
    
    if cons_val < 10:
        c_score = c_max
        c_range = "< 10 (Stable)"
    else:
        c_range = "> 10 (Volatile)"
        
    add_score('Growth', 'G_Cons', 'Growth StdDev', f"{cons_val:.1f}", "Rev Growth Volatility", c_score, c_max, c_score > 0, c_range)

    # --- PILLAR 2: QUALITY ---
    # 2.1 ROE
    roe = get_safe_float(info.get('returnOnEquity', 0)) * 100
    q_roe_max = weights.get('Q_ROE', 30)
    q_roe_score = 0
    q_roe_range = ""
    
    if roe > 20: 
        q_roe_score = q_roe_max
        q_roe_range = "> 20%"
    elif roe > 15: 
        q_roe_score = q_roe_max * 0.66
        q_roe_range = "15% - 20%"
    elif roe > 10:
        q_roe_score = q_roe_max * 0.33
        q_roe_range = "10% - 15%"
    else:
        q_roe_range = "< 10%"
        
    add_score('Profitability', 'Q_ROE', 'ROE', f"{roe:.1f}%", "Return on Equity", q_roe_score, q_roe_max, q_roe_score > 0, q_roe_range)

    # 2.2 Margins
    npm = get_safe_float(info.get('profitMargins', 0)) * 100
    q_mar_max = weights.get('Q_Mar', 20)
    q_mar_score = 0
    q_mar_range = ""
    
    if npm > 15: 
        q_mar_score = q_mar_max
        q_mar_range = "> 15%"
    elif npm > 8: 
        q_mar_score = q_mar_max * 0.5
        q_mar_range = "8% - 15%"
    else:
        q_mar_range = "< 8%"
        
    add_score('Profitability', 'Q_Mar', 'Net Margin', f"{npm:.1f}%", "Net Profit Margin", q_mar_score, q_mar_max, q_mar_score > 0, q_mar_range)

    # 2.3 Leverage
    de = get_safe_float(info.get('debtToEquity', 0)) / 100
    roa = get_safe_float(info.get('returnOnAssets', 0)) * 100
    
    q_deb_max = weights.get('Q_Deb', 20)
    q_deb_score = 0
    q_deb_range = ""
    
    if is_bfsi:
        val_display = f"ROA {roa:.1f}%"
        if roa > 1.5: 
            q_deb_score = q_deb_max
            q_deb_range = "ROA > 1.5%"
        elif roa > 1.0: 
            q_deb_score = q_deb_max * 0.5
            q_deb_range = "ROA > 1.0%"
        else:
            q_deb_range = "ROA < 1.0%"
        metric_label = "Asset Quality (ROA)"
    else:
        val_display = f"{de:.2f}"
        if de < 0.5: 
            q_deb_score = q_deb_max
            q_deb_range = "< 0.5"
        elif de < 1.0: 
            q_deb_score = q_deb_max * 0.5
            q_deb_range = "0.5 - 1.0"
        else:
            q_deb_range = "> 1.0"
        metric_label = "Debt to Equity"

    add_score('Profitability', 'Q_Deb', metric_label, val_display, "Financial Solvency", q_deb_score, q_deb_max, q_deb_score > 0, q_deb_range)
    
    # 2.4 Stability
    mar_std = 5.0
    try:
        if not financials.empty and 'Net Income' in financials.index and 'Total Revenue' in financials.index:
             n = financials.loc['Net Income']
             r = financials.loc['Total Revenue']
             margins = (n/r) * 100
             mar_std = calculate_std_dev(margins)
    except: pass
        
    q_sta_max = weights.get('Q_Sta', 15)
    q_sta_score = 0
    q_sta_range = ""
    
    if mar_std < 3: 
        q_sta_score = q_sta_max
        q_sta_range = "< 3 (Stable)"
    else:
        q_sta_range = "> 3 (Volatile)"
        
    add_score('Profitability', 'Q_Sta', 'Margin Stability', f"{mar_std:.1f}", "Std Dev of Margins", q_sta_score, q_sta_max, q_sta_score > 0, q_sta_range)

    # 2.5 Cash Conversion
    cc_ratio = 1.0
    try:
        if not cashflow.empty and not financials.empty:
            oc = get_safe_float(cashflow.loc['Operating Cash Flow'].iloc[0])
            ni = get_safe_float(financials.loc['Net Income'].iloc[0])
            if ni != 0: cc_ratio = oc / ni
    except: pass
         
    q_cas_max = weights.get('Q_Cas', 15)
    q_cas_score = 0
    q_cas_range = ""
    
    if cc_ratio > 0.8: 
        q_cas_score = q_cas_max
        q_cas_range = "> 0.8"
    else:
        q_cas_range = "< 0.8"
        
    add_score('Profitability', 'Q_Cas', 'Cash Conversion', f"{cc_ratio:.1f}", "OCF / Net Income", q_cas_score, q_cas_max, q_cas_score > 0, q_cas_range)

    # --- PILLAR 3: VALUATION ---
    peg = get_safe_float(info.get('pegRatio', 0))
    v_peg_max = weights.get('V_PEG', 15)
    v_peg_score = 0
    v_peg_range = ""
    
    if peg > 0 and peg < 1.5: 
        v_peg_score = v_peg_max
        v_peg_range = "0 - 1.5"
    else:
        v_peg_range = "> 1.5 or < 0"
        
    add_score('Valuation', 'V_PEG', 'PEG Ratio', f"{peg:.2f}", "Price/Earnings to Growth", v_peg_score, v_peg_max, v_peg_score > 0, v_peg_range)

    # Sector Relatives
    try:
        pe = get_safe_float(info.get('trailingPE', 0))
        pb = get_safe_float(info.get('priceToBook', 0))
        ev_ebitda = get_safe_float(info.get('enterpriseToEbitda', 0))
        
        # PE Rank
        pe_score_raw = sector_manager.calculate_percentile(pe, 'PE', sector)
        v_per_max = weights.get('V_PER', 30)
        v_per_score = (pe_score_raw / 100.0) * v_per_max
        add_score('Valuation', 'V_PER', 'PE Sector Rank', f"Top {pe_score_raw:.0f}%", "Percentile vs Sector", v_per_score, v_per_max, v_per_score > v_per_max/2, f"Rank {pe_score_raw:.0f}%")

        # EV/EBITDA Rank
        ev_score_raw = sector_manager.calculate_percentile(ev_ebitda, 'EV_EBITDA', sector)
        v_ev_max = weights.get('V_EV', 20)
        v_ev_score = (ev_score_raw / 100.0) * v_ev_max
        add_score('Valuation', 'V_EV', 'EV/EBITDA Rank', f"Top {ev_score_raw:.0f}%", "Percentile vs Sector", v_ev_score, v_ev_max, v_ev_score > v_ev_max/2, f"Rank {ev_score_raw:.0f}%")

        # PB Rank
        pb_score_raw = sector_manager.calculate_percentile(pb, 'PB', sector)
        v_pb_max = weights.get('V_PB', 10)
        v_pb_score = (pb_score_raw / 100.0) * v_pb_max
        add_score('Valuation', 'V_PB', 'P/B Rank', f"Top {pb_score_raw:.0f}%", "Percentile vs Sector", v_pb_score, v_pb_max, v_pb_score > v_pb_max/2, f"Rank {pb_score_raw:.0f}%")
        
    except Exception as e:
        print(f"Valuation Error: {e}")

    # --- PILLAR 4: TECHNICALS ---
    tech = get_technical_analysis(symbol)
    
    if tech:
        # 4.1 Trend
        t_tre_max = weights.get('T_Tre', 35)
        t_tre_score = 0
        is_uptrend = tech['sma50'] > tech['sma200']
        
        t_tre_score = t_tre_max if is_uptrend else 0
        add_score('Technicals', 'T_Tre', 'Trend Structure', "Bullish" if is_uptrend else "Bearish", "50 DMA > 200 DMA", t_tre_score, t_tre_max, is_uptrend, "Golden Cross")
        
        # 4.2 Momentum (RSI)
        rsi = tech['rsi']
        t_rsi_max = weights.get('T_RSI', 15)
        t_rsi_score = 0
        t_rsi_range = ""
        
        if 40 <= rsi <= 70: 
            t_rsi_score = t_rsi_max
            t_rsi_range = "40-70 (Strong)"
        else:
            t_rsi_range = "<40 or >70"
            
        add_score('Technicals', 'T_RSI', 'RSI Zone', f"{rsi:.1f}", "Relative Strength Index", t_rsi_score, t_rsi_max, t_rsi_score > 0, t_rsi_range)
        
        # 4.3 Momentum (Rel Strength - simplified)
        mom_val = (tech['return_6m'] + tech['return_12m']) / 2
        t_mom_max = weights.get('T_Mom', 30)
        t_mom_score = 0
        t_mom_range = ""
        
        if mom_val > 20:
             t_mom_score = t_mom_max
             t_mom_range = "> 20%"
        elif mom_val > 0:
             t_mom_score = t_mom_max * 0.5
             t_mom_range = "0% - 20%"
        else:
             t_mom_range = "< 0%"

        add_score('Technicals', 'T_Mom', 'Momentum', f"{mom_val:.1f}%", "Relative Return Strength", t_mom_score, t_mom_max, t_mom_score > 0, t_mom_range)
        
        # 4.4 Drawdown
        dd = abs(tech['drawdown'])
        t_dra_max = weights.get('T_Dra', 10)
        t_dra_score = 0
        if dd < 15: t_dra_score = t_dra_max
        add_score('Technicals', 'T_Dra', 'Drawdown', f"{dd:.1f}%", "From 52W High", t_dra_score, t_dra_max, t_dra_score > 0, "< 15%")

        # 4.5 Exit Risk
        t_exi_max = weights.get('T_Exi', 10)
        t_exi_score = 0
        is_safe = tech['current_price'] > tech['sma200']
        if is_safe: t_exi_score = t_exi_max
        add_score('Technicals', 'T_Exi', 'Exit Risk', "Low" if is_safe else "High", "Price > 200 DMA", t_exi_score, t_exi_max, is_safe, "Safe")
    
    else:
        # Add zeroes if tech fails
        pass

    # --- FINAL NORMALIZATION ---
    final_pillar_scores = {}
    
    for pillar in ['Growth', 'Profitability', 'Valuation', 'Technicals']:
        earned = scores[pillar]
        max_possible = max_scores[pillar]
        
        if max_possible > 0:
            performance_ratio = earned / max_possible 
        else:
            performance_ratio = 0
            
        # UI expects something out of the Pillar Weight (e.g. 24/30)
        # So we scale the performance ratio to the Pillar Weight
        pillar_weight = weights.get(pillar, 25)
        final_pillar_scores[pillar] = round(performance_ratio * pillar_weight, 1)
    
    total_health_score = sum(final_pillar_scores.values())
    
    # Normalize Total if Pillar Weights don't sum to 100
    total_weight = sum([weights.get(p, 25) for p in ['Growth', 'Profitability', 'Valuation', 'Technicals']])
    if total_weight > 0:
        total_health_score = (total_health_score / total_weight) * 100

    grade = "Strong Buy" if total_health_score >= 80 else "Buy" if total_health_score >= 60 else "Hold" if total_health_score >= 40 else "Sell"

    return {
        'symbol': symbol,
        'name': info.get('shortName', symbol),
        'sector': sector,
        'current_price': current_price,
        'market_cap': info.get('marketCap', 0),
        'high_52w': info.get('fiftyTwoWeekHigh', 0),
        'low_52w': info.get('fiftyTwoWeekLow', 0),
        'pe_ratio': info.get('trailingPE'),
        'peg_ratio': peg,
        'roe': roe / 100.0,
        'debt_to_equity': de,
        'description': info.get('longBusinessSummary', 'No description available.'),
        'health_score': round(total_health_score, 1),
        'grade': grade,
        'pillar_scores': final_pillar_scores,
        'score_breakdown': breakdown,
        'weights_used': weights,
        'sparkline': tech.get('sparkline', []) if tech else [],
        'return_12m': tech.get('return_12m', 0) if tech else 0,
        'is_above_200dma': (tech['current_price'] > tech['sma200']) if tech else False,
        'drawdown': tech.get('drawdown', 0) if tech else 0
    }
