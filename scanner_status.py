import sqlite3
import os
import time

class ScannerStatusManager:
    def __init__(self, db_path=None):
        if db_path is None:
            db_path = os.environ.get('DATABASE_PATH', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tracker.db'))
        self.db_path = db_path

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def set_status(self, user_id, running, progress=0, total=0, message="Idle"):
        """Update scanner status in the database."""
        conn = self._get_conn()
        try:
            conn.execute('''
                INSERT INTO scanner_state (user_id, is_running, progress, total, message, last_updated)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    is_running=excluded.is_running,
                    progress=excluded.progress,
                    total=excluded.total,
                    message=excluded.message,
                    last_updated=CURRENT_TIMESTAMP
            ''', (user_id, 1 if running else 0, progress, total, message))
            conn.commit()
        finally:
            conn.close()

    def get_status(self, user_id):
        """Fetch current scanner status for a user."""
        conn = self._get_conn()
        try:
            row = conn.execute('SELECT * FROM scanner_state WHERE user_id = ?', (user_id,)).fetchone()
            if row:
                return {
                    'running': bool(row['is_running']),
                    'progress': row['progress'],
                    'total': row['total'],
                    'message': row['message']
                }
            return {'running': False, 'progress': 0, 'total': 0, 'message': 'Idle'}
        finally:
            conn.close()

    def reset_all(self):
        """Force reset all scanners (useful for crashes)."""
        conn = self._get_conn()
        try:
            conn.execute('UPDATE scanner_state SET is_running = 0, message = "Reset by System"')
            conn.commit()
        finally:
            conn.close()
