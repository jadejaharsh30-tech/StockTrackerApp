import numpy as np

class ScoringEngine:
    """
    Implements the Turtle Wealth 4-Pillar Scoring Model (Master Prompt Part 2).
    Strict adherence to ranges and weights.
    """

    def __init__(self):
        # --- PILLAR WEIGHTS (User Configurable Defaults) ---
        self.WEIGHTS = {
            'growth': 0.30,
            'quality': 0.25,
            'value': 0.20,
            'technical': 0.25
        }

    def calculate_score(self, ticker_data, market_data):
        """
        Main entry point.
        :param ticker_data: Dictionary containing fundamental data (revenue, pat, margins, etc.)
        :param market_data: Dictionary containing price, MA, RSI, etc.
        :return: comprehensive score dict
        """
        is_bfsi = ticker_data.get('sector', '').lower() in ['financial services', 'banking', 'banks']

        growth_score = self._calc_growth(ticker_data, is_bfsi)
        quality_score = self._calc_quality(ticker_data, is_bfsi)
        value_score = self._calc_value(ticker_data)
        tech_score = self._calc_technical(market_data)

        overall_score = (
            (growth_score['score'] * self.WEIGHTS['growth']) +
            (quality_score['score'] * self.WEIGHTS['quality']) +
            (value_score['score'] * self.WEIGHTS['value']) +
            (tech_score['score'] * self.WEIGHTS['technical'])
        )

        return {
            'overall_score': round(overall_score, 2),
            'pillars': {
                'growth': growth_score,
                'quality': quality_score,
                'value': value_score,
                'technical': tech_score
            }
        }

    # --- 2.1 GROWTH PILLAR (30%) ---
    def _calc_growth(self, data, is_bfsi):
        scores = {}
        
        # 1. Revenue / Loan Book CAGR (35%)
        # If BFSI, use 'loan_book_cagr'. Else 'revenue_cagr_3y'
        rev_cagr = data.get('loan_book_cagr', 0) if is_bfsi else data.get('revenue_cagr_3y', 0)
        scores['revenue_cagr'] = self._score_cagr(rev_cagr)

        # 2. PAT CAGR (35%)
        pat_cagr = data.get('pat_cagr_3y', 0)
        scores['pat_cagr'] = self._score_cagr(pat_cagr)

        # 3. OCF / NII Growth (20%)
        # If BFSI, use 'nii_growth' (Net Interest Income). Else 'ocf_cagr'
        ocf_metric = data.get('nii_growth', 0) if is_bfsi else data.get('ocf_cagr_3y', 0)
        scores['ocf_growth'] = self._score_cagr(ocf_metric)

        # 4. Consistency (10%)
        # Std Dev of YoY growth
        consistency = data.get('growth_consistency_std', 100) # Default high volatility
        if consistency < 10: scores['consistency'] = 100 # Very stable
        elif consistency < 20: scores['consistency'] = 80 # Stable
        elif consistency < 40: scores['consistency'] = 50 # Moderate
        else: scores['consistency'] = 20 # Volatile

        # Weighted Sum
        final_score = (
            (scores['revenue_cagr'] * 0.35) +
            (scores['pat_cagr'] * 0.35) +
            (scores['ocf_growth'] * 0.20) +
            (scores['consistency'] * 0.10)
        )
        scores['score'] = round(final_score, 1)
        return scores

    def _score_cagr(self, val):
        # Range Map: <5(10), 5-10(30), 10-15(55), 15-20(75), 20-30(90), >30(100)
        # Assuming val is percentage (e.g. 15.5 for 15.5%)
        if val > 30: return 100
        elif val > 20: return 90
        elif val > 15: return 75
        elif val > 10: return 55
        elif val > 5: return 30
        else: return 10

    # --- 2.2 QUALITY PILLAR (25%) ---
    def _calc_quality(self, data, is_bfsi):
        scores = {}

        # 1. ROE / ROCE (30%)
        # BFSI: Use ROE. Others: Use ROCE if available, else ROE
        if is_bfsi:
            return_ratio = data.get('roe', 0)
        else:
            return_ratio = data.get('roce', data.get('roe', 0))
            
        # Range: <8(10), 8-12(30), 12-16(55), 16-20(75), 20-25(90), >25(100)
        if return_ratio > 25: scores['return_ratio'] = 100
        elif return_ratio > 20: scores['return_ratio'] = 90
        elif return_ratio > 16: scores['return_ratio'] = 75
        elif return_ratio > 12: scores['return_ratio'] = 55
        elif return_ratio > 8: scores['return_ratio'] = 30
        else: scores['return_ratio'] = 10

        # 2. Operating Margin (20%)
        op_margin = data.get('operating_margin', 0)
        # Range: <5(10), 5-10(30), 10-15(55), 15-20(75), 20-30(90), >30(100)
        if op_margin > 30: scores['margins'] = 100
        elif op_margin > 20: scores['margins'] = 90
        elif op_margin > 15: scores['margins'] = 75
        elif op_margin > 10: scores['margins'] = 55
        elif op_margin > 5: scores['margins'] = 30
        else: scores['margins'] = 10

        # 3. Margin Stability (15%)
        # Range: Unstable(20), Moderate(50), Stable(80), Very Stable(100)
        margin_std = data.get('margin_std_5y', 100)
        if margin_std < 2: scores['margin_stability'] = 100 
        elif margin_std < 5: scores['margin_stability'] = 80
        elif margin_std < 10: scores['margin_stability'] = 50
        else: scores['margin_stability'] = 20

        # 4. Debt Metrics (20%) - D/E
        # Range: <0.1(100), <0.5(70), <1.0(40), >1.0(10)
        # Prompt: "Net cash/very low" 100, "Comfortable" 70, "Mod Leveraged" 40, "Highly Lev" 10
        de = data.get('debt_to_equity', 100)
        if de <= 0.1: scores['leverage'] = 100
        elif de <= 0.7: scores['leverage'] = 70 # Adjusted "Comfortable" threshold
        elif de <= 1.5: scores['leverage'] = 40
        else: scores['leverage'] = 10

        # 5. Cash Conversion (15%) - OCF/PAT
        # Ratio: <0.5(20), 0.5-0.8(50), 0.8-1.1(80), >1.1(100)
        if is_bfsi:
            # BFSI Override: Capital Adequacy
            car = data.get('capital_adequacy', 0)
            if car > 18: scores['cash_conversion'] = 100
            elif car > 14: scores['cash_conversion'] = 80 # Tweaked for Indian context
            elif car > 10: scores['cash_conversion'] = 50
            else: scores['cash_conversion'] = 20
        else:
            ocf_pat = data.get('ocf_to_pat', 0)
            if ocf_pat > 1.1: scores['cash_conversion'] = 100
            elif ocf_pat > 0.8: scores['cash_conversion'] = 80
            elif ocf_pat > 0.5: scores['cash_conversion'] = 50
            else: scores['cash_conversion'] = 20

        final_score = (
            (scores['return_ratio'] * 0.30) +
            (scores['margins'] * 0.20) +
            (scores['margin_stability'] * 0.15) +
            (scores['leverage'] * 0.20) +
            (scores['cash_conversion'] * 0.15)
        )
        scores['score'] = round(final_score, 1)
        return scores

    # --- 2.3 VALUE PILLAR (20%) ---
    def _calc_value(self, data):
        scores = {}
        
        # 1. PE Rank (35%)
        # >90(10), 70-90(30), 40-70(60), 20-40(85), <20(100)
        pe_rank = data.get('pe_percentile', 50) 
        scores['pe_rel'] = self._score_percentile_valuation(pe_rank)

        # 2. EV/EBITDA Rank (25%)
        ev_rank = data.get('ev_ebitda_percentile', 50)
        scores['ev_rel'] = self._score_percentile_valuation(ev_rank)

        # 3. P/B Rank (25%)
        pb_rank = data.get('pb_percentile', 50)
        scores['pb_rel'] = self._score_percentile_valuation(pb_rank)

        # 4. PEG Ratio (15%)
        peg = data.get('peg_ratio', 1.5)
        # PEG Adjustment is separate. Base score for PEG itself isn't explicitly defined in text apart from adjustment, 
        # but implied "Fair Value". We'll use a standard scale for the 15% component
        if peg < 1: scores['peg'] = 100
        elif peg < 1.5: scores['peg'] = 80
        elif peg < 2.0: scores['peg'] = 60
        else: scores['peg'] = 30 # Expensive

        base_score = (
            (scores['pe_rel'] * 0.35) +
            (scores['ev_rel'] * 0.25) +
            (scores['pb_rel'] * 0.25) +
            (scores['peg'] * 0.15) 
        )
        
        # PEG Adjustment: 
        # > 2 subtract 20.
        # < 1 add 10 (cap 100).
        if peg > 2.0: 
            base_score = base_score - 20
        elif peg < 1.0: 
            base_score = min(100, base_score + 10)
        
        scores['score'] = round(max(0, base_score), 1)
        return scores

    def _score_percentile_valuation(self, rank):
        # Rank is percentile (0-100). 
        # 100th percentile = Highest Valuation (Expensive) = Low Score
        if rank > 90: return 10
        elif rank > 70: return 30
        elif rank > 40: return 60
        elif rank > 20: return 85
        else: return 100

    # --- 2.4 TECHNICAL PILLAR (25%) ---
    def _calc_technical(self, market):
        scores = {}
        
        # 1. Trend Structure (35%)
        # Below 200(10), >200(40), >100&200(70), >50&100&200(100)
        is_abv_200 = market.get('price_above_200dma', False)
        is_abv_100 = market.get('price_above_100dma', False)
        is_abv_50 = market.get('price_above_50dma', False)
        
        if is_abv_50 and is_abv_100 and is_abv_200: scores['trend'] = 100
        elif is_abv_100 and is_abv_200: scores['trend'] = 70
        elif is_abv_200: scores['trend'] = 40
        else: scores['trend'] = 10

        # 2. Momentum (30%) - Avg 6M/12M return
        # <0(10), 0-10(40), 10-20(65), 20-40(85), >40(100)
        mom_6m = market.get('return_6m', 0)
        mom_12m = market.get('return_12m', 0)
        avg_mom = (mom_6m + mom_12m) / 2
        
        if avg_mom > 40: scores['momentum'] = 100
        elif avg_mom > 20: scores['momentum'] = 85
        elif avg_mom > 10: scores['momentum'] = 65
        elif avg_mom > 0: scores['momentum'] = 40
        else: scores['momentum'] = 10
        
        # 3. RSI Zone (15%)
        # <40(20), 40-50(40), 50-60(70), 60-70(90), >70(80 - Overbought)
        rsi = market.get('rsi', 50)
        if rsi > 70: scores['rsi'] = 80
        elif rsi > 60: scores['rsi'] = 90
        elif rsi > 50: scores['rsi'] = 70
        elif rsi > 40: scores['rsi'] = 40
        else: scores['rsi'] = 20
        
        # 4. Exit Risk (10%) - Distance from Swing Low
        # "Distance" = (Current - Low) / Low
        # <5%(20), 5-10%(50), 10-20%(80), >20%(100)
        dist_swing = market.get('dist_swing_low', 10)
        if dist_swing > 20: scores['risk'] = 100
        elif dist_swing > 10: scores['risk'] = 80
        elif dist_swing > 5: scores['risk'] = 50
        else: scores['risk'] = 20 # Stop loss danger
        
        # 5. Volatility / Drawdown (10%)
        # Prompt says "Drawdown / Volatility". We'll use Drawdown % from 52W High.
        # Drawdown is usually negative (-10%). We'll look at absolute magnitude or Distance from High.
        # Let's map "Distance from 52W High": 
        # <5% (Strong) -> 100? No, high drawdown is bad.
        # Drawdown 0-10% (Score 100), 10-20% (80), 20-30% (50), >30% (20)
        dd = abs(market.get('drawdown', 0)) # Ensure positive
        if dd < 10: scores['volatility'] = 100
        elif dd < 20: scores['volatility'] = 80
        elif dd < 30: scores['volatility'] = 50
        else: scores['volatility'] = 20

        final_score = (
            (scores['trend'] * 0.35) +
            (scores['momentum'] * 0.30) +
            (scores['rsi'] * 0.15) +
            (scores['risk'] * 0.10) +
            (scores['volatility'] * 0.10)
        )
        scores['score'] = round(final_score, 1)
        return scores
