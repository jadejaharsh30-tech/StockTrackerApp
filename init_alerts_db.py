import sqlite3

def init_alerts_table():
    db_path = 'tracker.db'
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    print("Creating alerts table if not exists...")
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            target_price REAL NOT NULL,
            condition TEXT NOT NULL, -- 'ABOVE' or 'BELOW'
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    
    conn.commit()
    conn.close()
    print("Done.")

if __name__ == "__main__":
    init_alerts_table()
