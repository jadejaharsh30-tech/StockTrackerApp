import sqlite3

def migrate_add_preview_table():
    """Add upload_previews table to existing database"""
    conn = sqlite3.connect('tracker.db')
    cursor = conn.cursor()
    
    try:
        # Create the upload_previews table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS upload_previews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                preview_type TEXT NOT NULL,
                preview_data TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        print("✅ upload_previews table created successfully!")
        
        # Verify the table exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='upload_previews'")
        if cursor.fetchone():
            print("✅ Table verification successful!")
        else:
            print("❌ Table creation failed!")
            
    except Exception as e:
        print(f"❌ Error creating table: {e}")
    finally:
        conn.commit()
        conn.close()

if __name__ == '__main__':
    print("Starting migration to add upload_previews table...")
    migrate_add_preview_table()
    print("Migration completed!") 