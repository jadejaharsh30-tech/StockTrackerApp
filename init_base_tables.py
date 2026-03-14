"""
Initialize all necessary database tables for the stock tracker app.
"""

import sqlite3

import os

def init_all_tables():
    """Create all required tables."""
    db_path = os.environ.get('DATABASE_PATH', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tracker.db'))
    conn = sqlite3.connect(db_path)
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
    

    # PMS Review Dashboard Snapshots table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS fund_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            fund TEXT NOT NULL,
            review_date TEXT NOT NULL,
            period TEXT NOT NULL DEFAULT 'weekly',
            data_json TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
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
    
    # 4. Scanner state table (to persist progress in multi-process environments)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS scanner_state (
        user_id INTEGER PRIMARY KEY,
        is_running BOOLEAN DEFAULT 0,
        progress INTEGER DEFAULT 0,
        total INTEGER DEFAULT 0,
        message TEXT,
        last_updated DATETIME DEFAULT CURRENT_TIMESTAMP
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
