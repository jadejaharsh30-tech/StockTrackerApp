"""
Initialize all necessary database tables for the stock tracker app.
"""

import sqlite3

def init_all_tables():
    """Create all required tables."""
    conn = sqlite3.connect('ath_tracker.db')
    cursor = conn.cursor()
    
    # 1. Stock data table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS stock_data (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        date DATE NOT NULL,
        open REAL,
        high REAL,
        low REAL,
        close REAL,
        volume INTEGER,
        sector TEXT,
        UNIQUE(symbol, date)
    )
    ''')
    
    # 2. Scoring history table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS scoring_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        calc_date DATE NOT NULL,
        overall_score REAL,
        growth_score REAL,
        quality_score REAL,
        value_score REAL,
        tech_score REAL,
        grade TEXT,
        UNIQUE(symbol, calc_date)
    )
    ''')
    
    # 3. Users table (if not exists)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        email TEXT
    )
    ''')
    
    # Create indices
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_stock_data_symbol ON stock_data(symbol)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_stock_data_date ON stock_data(date)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_scoring_history_symbol ON scoring_history(symbol)')
    
    conn.commit()
    conn.close()
    
    print("✅ All base tables created successfully!")
    print("   - stock_data")
    print("   - scoring_history")
    print("   - users")

if __name__ == '__main__':
    init_all_tables()
