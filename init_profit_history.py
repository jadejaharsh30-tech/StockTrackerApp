"""
Initialize the profit_history table used by the ATH profit scan.

Stores reported net profit per company per period, for both quarterly ('Q')
and annual ('A') series. This is the source of truth for the Dual/Growth
profit classification — yfinance only exposes ~4-6 quarters / ~4 years,
which is far too shallow to establish an all-time high.

Run once:  python init_profit_history.py
"""

import os
import sqlite3

DATABASE = os.environ.get(
    'DATABASE_PATH',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tracker.db')
)


def get_db():
    conn = sqlite3.connect(DATABASE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    return conn


def init_profit_history(verbose=True):
    conn = get_db()
    cursor = conn.cursor()

    # period_type: 'Q' = quarterly, 'A' = annual/yearly
    # period_end:  'YYYY-MM-DD' preferred; any sortable ISO-ish label works
    # net_profit:  reported net profit in the company's native reporting unit
    #              (crores/millions — only relative magnitude matters here)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS profit_history (
            symbol TEXT NOT NULL,
            period_type TEXT NOT NULL CHECK (period_type IN ('Q', 'A')),
            period_end TEXT NOT NULL,
            net_profit REAL,
            PRIMARY KEY (symbol, period_type, period_end)
        )
    ''')

    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_profit_history_lookup
        ON profit_history(symbol, period_type, period_end)
    ''')

    conn.commit()

    counts = conn.execute('''
        SELECT period_type, COUNT(*) AS n, COUNT(DISTINCT symbol) AS syms
        FROM profit_history GROUP BY period_type
    ''').fetchall()
    conn.close()

    if not verbose:
        return
    print("profit_history table ready.")
    if counts:
        for row in counts:
            label = 'quarterly' if row['period_type'] == 'Q' else 'annual'
            print(f"  {label}: {row['n']} rows across {row['syms']} symbols")
    else:
        print("  (empty — load data with import_profit_history.py)")


if __name__ == '__main__':
    init_profit_history()
