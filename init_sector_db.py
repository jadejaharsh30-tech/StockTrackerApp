"""
Sector Analytics Database Schema Initialization

Creates tables for:
- Sector scores (market-cap and equal-weighted)
- Sector risk metrics (beta, alpha, volatility, etc.)
- Sector alerts configuration
"""

import os

def init_sector_tables(db_path=None):
    """Initialize sector analytics tables."""
    if db_path is None:
        db_path = os.environ.get('DATABASE_PATH', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tracker.db'))
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Sector Scores Table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS sector_scores (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sector TEXT NOT NULL,
        date DATE NOT NULL,
        overall_score REAL,
        growth_score REAL,
        quality_score REAL,
        value_score REAL,
        tech_score REAL,
        weight_type TEXT CHECK(weight_type IN ('market_cap', 'equal')),
        num_companies INTEGER,
        total_market_cap REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(sector, date, weight_type)
    )
    ''')
    
    # Sector Risk Metrics Table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS sector_risk_metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sector TEXT NOT NULL,
        calc_date DATE NOT NULL,
        beta REAL,
        alpha REAL,
        volatility REAL,
        max_drawdown REAL,
        sharpe_ratio REAL,
        r_squared REAL,
        var_95 REAL,
        cvar_95 REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(sector, calc_date)
    )
    ''')
    
    # Sector Alerts Table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS sector_alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        sector TEXT,
        alert_name TEXT,
        condition_json TEXT,
        is_active BOOLEAN DEFAULT 1,
        last_triggered TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )
    ''')
    
    # Sector Company Contributions Table (for traceability)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS sector_company_contributions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sector TEXT NOT NULL,
        date DATE NOT NULL,
        symbol TEXT NOT NULL,
        overall_score REAL,
        market_cap REAL,
        weight REAL,
        contribution REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    
    # Create indices for faster queries
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_sector_scores_date ON sector_scores(date, sector)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_sector_risk_date ON sector_risk_metrics(calc_date, sector)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_sector_contrib ON sector_company_contributions(sector, date)')
    
    conn.commit()
    conn.close()
    print("✅ Sector analytics tables created successfully")

if __name__ == '__main__':
    init_sector_tables()
