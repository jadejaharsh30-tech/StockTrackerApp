import sqlite3
import os
import time

# A scan that has not written a status update in this long is treated as dead.
# A full 750-ticker scan runs in single-digit minutes, so this only trips when a
# worker was killed mid-scan — which previously left is_running=1 forever and
# locked the user out of scanning with no UI to recover.
STALE_AFTER_SECONDS = 30 * 60


class ScannerStatusManager:
    def __init__(self, db_path=None):
        if db_path is None:
            db_path = os.environ.get('DATABASE_PATH', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tracker.db'))
        self.db_path = db_path

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_table(self, conn):
        """
        Create scanner_state on demand. Without this a database that never had
        init_base_tables.py run against it makes every scanner route raise
        OperationalError before any work starts.
        """
        conn.execute('''
            CREATE TABLE IF NOT EXISTS scanner_state (
                user_id INTEGER PRIMARY KEY,
                is_running BOOLEAN DEFAULT 0,
                progress INTEGER DEFAULT 0,
                total INTEGER DEFAULT 0,
                message TEXT,
                last_updated DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')

    _last_update_cache = {}

    def set_status(self, user_id, running, progress=0, total=0, message="Idle", force=False):
        """
        Update scanner status in the database.
        Throttled to once every 3 seconds unless force=True or running=False.
        """
        now = time.time()
        if not force and running and user_id in self._last_update_cache:
            if now - self._last_update_cache[user_id] < 3:
                return # Throttle to reduce DB load on PythonAnywhere

        conn = self._get_conn()
        try:
            self._ensure_table(conn)
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
            self._last_update_cache[user_id] = now
        finally:
            conn.close()

    def get_status(self, user_id):
        """
        Fetch current scanner status for a user.

        A run whose last heartbeat is older than STALE_AFTER_SECONDS is reported
        as not running, and flagged via 'stale' so the UI can say why.
        """
        idle = {'running': False, 'progress': 0, 'total': 0, 'message': 'Idle', 'stale': False}
        conn = self._get_conn()
        try:
            self._ensure_table(conn)
            row = conn.execute(
                """SELECT is_running, progress, total, message,
                          CAST(strftime('%s','now') AS INTEGER)
                            - CAST(strftime('%s', last_updated) AS INTEGER) AS age_seconds
                   FROM scanner_state WHERE user_id = ?""",
                (user_id,)
            ).fetchone()
            if not row:
                return idle

            running = bool(row['is_running'])
            age = row['age_seconds'] if row['age_seconds'] is not None else 0
            if running and age > STALE_AFTER_SECONDS:
                return {
                    'running': False,
                    'progress': row['progress'],
                    'total': row['total'],
                    'message': f"Previous scan stopped responding ({age // 60} min ago). Ready to run.",
                    'stale': True,
                }
            return {
                'running': running,
                'progress': row['progress'],
                'total': row['total'],
                'message': row['message'],
                'stale': False,
            }
        except sqlite3.OperationalError:
            return idle
        finally:
            conn.close()

    def reset(self, user_id):
        """Clear a stuck 'running' state for one user."""
        conn = self._get_conn()
        try:
            self._ensure_table(conn)
            conn.execute(
                "UPDATE scanner_state SET is_running = 0, message = 'Reset by user' WHERE user_id = ?",
                (user_id,)
            )
            conn.commit()
        finally:
            conn.close()
        self._last_update_cache.pop(user_id, None)

    def reset_all(self):
        """Force reset all scanners (useful for crashes)."""
        conn = self._get_conn()
        try:
            self._ensure_table(conn)
            conn.execute("UPDATE scanner_state SET is_running = 0, message = 'Reset by System'")
            conn.commit()
        finally:
            conn.close()
        self._last_update_cache.clear()
