import sqlite3

conn = sqlite3.connect('tracker.db')
try:
    conn.execute("ALTER TABLE holdings ADD COLUMN rg_status TEXT DEFAULT 'R';")
    print("Column 'rg_status' added to 'holdings' table.")
except Exception as e:
    print(f"Migration error or column already exists: {e}")
conn.commit()
conn.close() 