"""
Sector Analytics Engine

Core computation logic for sector-level scoring and risk metrics.
Aggregates company-level fundamental scores into sector intelligence.
"""

import sqlite3
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Tuple
import yfinance as yf

class SectorAnalytics:
    """Sector-level score computation and risk analysis."""
    
    # NSE Sector Index Tickers (for yfinance)
    SECTOR_TICKERS = {
        'Financial Services': '^CNXFIN',
        'Bank': '^NSEBANK',
        'IT': '^CNXIT',
        'Auto': '^CNXAUTO',
        'Pharma': '^CNXPHARMA',
        'FMCG': '^CNXFMCG',
        'Metal': '^CNXMETAL',
        'Realty': '^CNXREALTY',
        'Energy': '^CNXENERGY',
        'Media': '^CNXMEDIA',
        'PSU Bank': '^CNXPSUBANK'
    }
    
    NIFTY_500_TICKER = '^CRSLDX'  # Nifty 500 index
    
    def __init__(self, db_path=None):
        import os
        self.db_path = db_path or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ath_tracker.db')
    
    def get_db(self):
        """Get database connection."""
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute('PRAGMA journal_mode=WAL')
        return conn
    
    def compute_sector_scores(self, target_date: str = None) -> Dict:
        """
        Compute sector scores using both market-cap and equal weighting.
        
        Args:
            target_date: Date string (YYYY-MM-DD). If None, uses latest.
        
        Returns:
            Dict with sector scores for both weighting methods
        """
        conn = self.get_db()
        
        if target_date is None:
            target_date = datetime.now().strftime('%Y-%m-%d')
        
        # Get latest fundamental scores from scoring_history
        query = '''
        SELECT 
            sh.symbol,
            sd.sector,
            sh.overall_score,
            sh.growth_score,
            sh.quality_score,
            sh.value_score,
            sh.tech_score,
            (sd.close * sd.volume) AS market_cap
        FROM (
            SELECT symbol, MAX(calc_date) as max_date
            FROM scoring_history
            GROUP BY symbol
        ) latest
        JOIN scoring_history sh ON sh.symbol = latest.symbol AND sh.calc_date = latest.max_date
        LEFT JOIN (
            SELECT symbol, sector, close, volume, MAX(date) as max_date
            FROM stock_data
            GROUP BY symbol
        ) sd ON sh.symbol = sd.symbol
        WHERE sd.sector IS NOT NULL AND sd.sector != ''
        AND sh.overall_score IS NOT NULL
        '''
        
        df = pd.read_sql_query(query, conn)
        conn.close()
        
        if df.empty:
            return {}
        
        # Fill missing market caps with median
        df['market_cap'] = df['market_cap'].fillna(df['market_cap'].median())
        
        results = {}
        
        # Compute scores for each sector
        for sector in df['sector'].unique():
            sector_df = df[df['sector'] == sector].copy()
            
            # Market-cap weighted scores
            total_mcap = sector_df['market_cap'].sum()
            sector_df['weight'] = sector_df['market_cap'] / total_mcap
            
            mcap_scores = {
                'overall_score': (sector_df['overall_score'] * sector_df['weight']).sum(),
                'growth_score': (sector_df['growth_score'] * sector_df['weight']).sum(),
                'quality_score': (sector_df['quality_score'] * sector_df['weight']).sum(),
                'value_score': (sector_df['value_score'] * sector_df['weight']).sum(),
                'tech_score': (sector_df['tech_score'] * sector_df['weight']).sum(),
                'num_companies': len(sector_df),
                'total_market_cap': total_mcap
            }
            
            # Equal-weighted scores
            equal_scores = {
                'overall_score': sector_df['overall_score'].mean(),
                'growth_score': sector_df['growth_score'].mean(),
                'quality_score': sector_df['quality_score'].mean(),
                'value_score': sector_df['value_score'].mean(),
                'tech_score': sector_df['tech_score'].mean(),
                'num_companies': len(sector_df),
                'total_market_cap': total_mcap
            }
            
            # Store in database
            self._save_sector_scores(sector, target_date, mcap_scores, 'market_cap')
            self._save_sector_scores(sector, target_date, equal_scores, 'equal')
            
            # Track contributions for traceability
            self._save_company_contributions(sector, target_date, sector_df)
            
            results[sector] = {
                'market_cap': mcap_scores,
                'equal': equal_scores
            }
        
        return results
    
    def _save_sector_scores(self, sector: str, date: str, scores: Dict, weight_type: str):
        """Save sector scores to database."""
        conn = self.get_db()
        cursor = conn.cursor()
        
        cursor.execute('''
        INSERT OR REPLACE INTO sector_scores 
        (sector, date, overall_score, growth_score, quality_score, value_score, tech_score, 
         weight_type, num_companies, total_market_cap)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            sector, date,
            scores['overall_score'], scores['growth_score'], scores['quality_score'],
            scores['value_score'], scores['tech_score'],
            weight_type, scores['num_companies'], scores['total_market_cap']
        ))
        
        conn.commit()
        conn.close()
    
    def _save_company_contributions(self, sector: str, date: str, sector_df: pd.DataFrame):
        """Save company-level contributions to sector score."""
        conn = self.get_db()
        
        # Clear old contributions for this sector/date
        conn.execute('DELETE FROM sector_company_contributions WHERE sector = ? AND date = ?', 
                    (sector, date))
        
        # Insert new contributions
        for _, row in sector_df.iterrows():
            total_mcap = sector_df['market_cap'].sum()
            weight = row['market_cap'] / total_mcap if total_mcap > 0 else 1/len(sector_df)
            contribution = row['overall_score'] * weight
            
            conn.execute('''
            INSERT INTO sector_company_contributions 
            (sector, date, symbol, overall_score, market_cap, weight, contribution)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (sector, date, row['symbol'], row['overall_score'], 
                  row['market_cap'], weight, contribution))
        
        conn.commit()
        conn.close()
    
    def compute_risk_metrics(self, sector: str, lookback_years: int = 2) -> Dict:
        """
        Compute risk metrics for a sector using 2-year weekly data.
        
        Args:
            sector: Sector name
            lookback_years: Years of historical data to use
        
        Returns:
            Dict with beta, alpha, volatility, max drawdown, sharp ratio, R²
        """
        if sector not in self.SECTOR_TICKERS:
            return None
        
        end_date = datetime.now()
        start_date = end_date - timedelta(days=lookback_years*365)
        
        # Fetch sector index data
        sector_ticker = self.SECTOR_TICKERS[sector]
        sector_data = yf.download(sector_ticker, start=start_date, end=end_date, interval='1wk', progress=False)
        
        # Fetch Nifty 500 (market benchmark)
        market_data = yf.download(self.NIFTY_500_TICKER, start=start_date, end=end_date, interval='1wk', progress=False)
        
        if sector_data.empty or market_data.empty:
            return None
        
        # Calculate returns
        sector_returns = sector_data['Close'].pct_change().dropna()
        market_returns = market_data['Close'].pct_change().dropna()
        
        # Align datasets
        aligned_data = pd.DataFrame({
            'sector': sector_returns,
            'market': market_returns
        }).dropna()
        
        if len(aligned_data) < 20:  # Need min data points
            return None
        
        # Beta and Alpha (linear regression)
        from scipy import stats
        slope, intercept, r_value, p_value, std_err = stats.linregress(
            aligned_data['market'], aligned_data['sector']
        )
        
        beta = slope
        alpha = intercept * 52  # Annualize alpha
        r_squared = r_value ** 2
        
        # Volatility (annualized)
        volatility = aligned_data['sector'].std() * np.sqrt(52)
        
        # Max Drawdown
        cumulative = (1 + sector_returns).cumprod()
        running_max = cumulative.cummax()
        drawdown = (cumulative - running_max) / running_max
        max_drawdown = drawdown.min()
        
        # Sharpe Ratio (assuming risk-free rate = 0 for simplicity)
        mean_return = aligned_data['sector'].mean() * 52
        sharpe_ratio = mean_return / volatility if volatility > 0 else 0
        
        # VaR and CVaR (95% confidence)
        var_95 = np.percentile(aligned_data['sector'], 5)
        cvar_95 = aligned_data['sector'][aligned_data['sector'] <= var_95].mean()
        
        metrics = {
            'beta': beta,
            'alpha': alpha,
            'volatility': volatility,
            'max_drawdown': max_drawdown,
            'sharpe_ratio': sharpe_ratio,
            'r_squared': r_squared,
            'var_95': var_95,
            'cvar_95': cvar_95
        }
        
        # Save to database
        self._save_risk_metrics(sector, datetime.now().strftime('%Y-%m-%d'), metrics)
        
        return metrics
    
    def _save_risk_metrics(self, sector: str, calc_date: str, metrics: Dict):
        """Save risk metrics to database."""
        conn = self.get_db()
        cursor = conn.cursor()
        
        cursor.execute('''
        INSERT OR REPLACE INTO sector_risk_metrics 
        (sector, calc_date, beta, alpha, volatility, max_drawdown, sharpe_ratio, r_squared, var_95, cvar_95)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            sector, calc_date,
            metrics['beta'], metrics['alpha'], metrics['volatility'],
            metrics['max_drawdown'], metrics['sharpe_ratio'], metrics['r_squared'],
            metrics['var_95'], metrics['cvar_95']
        ))
        
        conn.commit()
        conn.close()
    
    def get_sector_ranking(self, weight_type: str = 'market_cap') -> pd.DataFrame:
        """
        Get sortable sector ranking table.
        
        Args:
            weight_type: 'market_cap' or 'equal'
        
        Returns:
            DataFrame with sector rankings
        """
        conn = self.get_db()
        
        query = f'''
        SELECT 
            s.sector,
            s.overall_score,
            s.growth_score,
            s.quality_score,
            s.value_score,
            s.tech_score,
            s.num_companies,
            s.total_market_cap,
            r.beta,
            r.alpha,
            r.volatility,
            r.sharpe_ratio
        FROM sector_scores s
        LEFT JOIN sector_risk_metrics r ON s.sector = r.sector
        WHERE s.weight_type = ?
        AND s.date = (SELECT MAX(date) FROM sector_scores WHERE weight_type = ?)
        AND (r.calc_date = (SELECT MAX(calc_date) FROM sector_risk_metrics WHERE sector = s.sector) OR r.calc_date IS NULL)
        ORDER BY s.overall_score DESC
        '''
        
        df = pd.read_sql_query(query, conn, params=(weight_type, weight_type))
        conn.close()
        
        return df


if __name__ == '__main__':
    # Test sector score computation
    analytics = SectorAnalytics()
    scores = analytics.compute_sector_scores()
    print(f"Computed scores for {len(scores)} sectors")
    
    # Test risk metrics for one sector
    if 'IT' in scores:
        metrics = analytics.compute_risk_metrics('IT')
        print(f"IT Sector Beta: {metrics.get('beta', 'N/A')}")
