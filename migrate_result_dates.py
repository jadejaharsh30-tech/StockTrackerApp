import sqlite3
from datetime import datetime

DATABASE = 'tracker.db'

SPECIAL_VALUES = {'Not Announced', 'N/A', 'CONFLICT'}

conn = sqlite3.connect(DATABASE)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

rows = cur.execute("SELECT id, result_date FROM profit_tracker").fetchall()
updated_count = 0

for row in rows:
    rid = row['id']
    result_date = row['result_date']
    if result_date in SPECIAL_VALUES or not result_date:
        continue
    try:
        # Try to parse old format
        date_obj = datetime.strptime(result_date, '%d %B %Y')
        new_date = date_obj.strftime('%Y-%m-%d')
        if new_date != result_date:
            cur.execute("UPDATE profit_tracker SET result_date = ? WHERE id = ?", (new_date, rid))
            updated_count += 1
    except Exception:
        # If parsing fails, skip
        continue

conn.commit()
conn.close()
print(f"Migration complete. Updated {updated_count} result_date values to YYYY-MM-DD format.") 