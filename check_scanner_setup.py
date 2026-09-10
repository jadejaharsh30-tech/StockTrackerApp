"""
Preflight check for the ATH scanner.

Verifies everything the scan depends on before you run it, so a failure shows up
here with a fix attached rather than as a 500 in the browser.

    python check_scanner_setup.py           # checks only
    python check_scanner_setup.py --live    # also fetch 3 tickers from yfinance
"""

import argparse
import os
import sqlite3
import sys

DATABASE = os.environ.get(
    'DATABASE_PATH',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tracker.db')
)

OK, WARN, FAIL = "  \033[32mPASS\033[0m", "  \033[33mWARN\033[0m", "  \033[31mFAIL\033[0m"
problems = []
warnings = []


def fail(msg, fix):
    print(f"{FAIL}  {msg}")
    print(f"        fix: {fix}")
    problems.append(msg)


def warn(msg, note=""):
    print(f"{WARN}  {msg}")
    if note:
        print(f"        {note}")
    warnings.append(msg)


def ok(msg):
    print(f"{OK}  {msg}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--live', action='store_true',
                    help='Also test the yfinance price path (needs network)')
    args = ap.parse_args()

    print(f"\nDatabase: {DATABASE}")
    if not os.path.exists(DATABASE):
        fail("Database file does not exist.",
             "python init_base_tables.py && python init_profit_history.py")
        sys.exit(1)

    conn = sqlite3.connect(DATABASE, timeout=30)
    conn.row_factory = sqlite3.Row
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}

    print("\n1. Required tables")
    for table, fix in [
        ('users', 'python init_base_tables.py'),
        ('profit_tracker', 'python app.py  (creates it on startup)'),
        ('stocks', 'python app.py  (creates it on startup)'),
        ('ath_tracking_table', 'python init_base_tables.py'),
        ('scanner_state', 'python init_base_tables.py'),
        ('profit_history', 'python init_profit_history.py'),
    ]:
        if table in tables:
            ok(f"{table}")
        else:
            fail(f"{table} is missing", fix)

    print("\n2. Ticker universe")
    universe = 0
    if 'profit_tracker' in tables:
        universe = conn.execute(
            "SELECT COUNT(DISTINCT symbol) FROM profit_tracker").fetchone()[0]
        if universe:
            ok(f"{universe} symbols in profit_tracker")
        else:
            fail("profit_tracker is empty — the scan has nothing to scan",
                 "Add stocks via Profit Manager, or upload a list")

    print("\n3. ATH baselines")
    if 'ath_tracking_table' in tables:
        total = conn.execute("SELECT COUNT(*) FROM ath_tracking_table").fetchone()[0]
        broken = conn.execute(
            "SELECT COUNT(*) FROM ath_tracking_table "
            "WHERE previous_ath IS NULL OR previous_ath <= 0").fetchone()[0]
        staged = conn.execute(
            "SELECT COUNT(*) FROM ath_tracking_table WHERE today_ath IS NOT NULL").fetchone()[0]
        ok(f"{total} baselines stored")
        if broken:
            names = [r[0] for r in conn.execute(
                "SELECT symbol FROM ath_tracking_table "
                "WHERE previous_ath IS NULL OR previous_ath <= 0 LIMIT 10")]
            warn(f"{broken} baseline(s) missing/zero: {', '.join(names)}",
                 "These are skipped by the scan (they cannot false-trigger any more). "
                 "Click 'Refresh Baselines' -> OK to retry them.")
        if staged:
            warn(f"{staged} symbol(s) still carry an un-promoted today_ath",
                 "Left over from a scan where EOD Sync was not run. The next scan "
                 "clears them; promote first if those were real breakouts.")
        if 'profit_tracker' in tables and universe:
            missing = conn.execute("""
                SELECT COUNT(*) FROM (SELECT DISTINCT symbol FROM profit_tracker
                                      EXCEPT SELECT symbol FROM ath_tracking_table)""").fetchone()[0]
            if missing:
                warn(f"{missing} tracked symbol(s) have no baseline yet",
                     "Phase 1 of the next scan will seed them (slow first run).")

    print("\n4. Profit history (Phase 4)")
    if 'profit_history' in tables:
        row = conn.execute("""
            SELECT COUNT(*) n,
                   COUNT(DISTINCT CASE WHEN period_type='Q' THEN symbol END) q,
                   COUNT(DISTINCT CASE WHEN period_type='A' THEN symbol END) a
            FROM profit_history""").fetchone()
        if not row['n']:
            fail("profit_history is empty — every stock will show N/A",
                 "python import_profit_duckdb.py financial_data.duckdb")
        else:
            ok(f"{row['n']} rows | {row['q']} quarterly, {row['a']} annual series")
            if 'profit_tracker' in tables and universe:
                covered = conn.execute("""
                    SELECT COUNT(DISTINCT p.symbol) FROM profit_tracker p
                    WHERE EXISTS (SELECT 1 FROM profit_history h WHERE h.symbol=p.symbol)
                """).fetchone()[0]
                pct = 100.0 * covered / universe
                (ok if pct >= 90 else warn)(
                    f"coverage: {covered}/{universe} tracked symbols ({pct:.0f}%)")

    print("\n5. Scanner state")
    if 'scanner_state' in tables:
        rows = conn.execute("""
            SELECT user_id, is_running, message,
                   CAST(strftime('%s','now') AS INTEGER)
                     - CAST(strftime('%s', last_updated) AS INTEGER) AS age
            FROM scanner_state""").fetchall()
        if not rows:
            ok("idle (no scan has run yet)")
        for r in rows:
            if r['is_running'] and (r['age'] or 0) > 1800:
                warn(f"user {r['user_id']}: stuck 'running' from {r['age']//60} min ago",
                     "Auto-expires after 30 min; the UI offers a reset when you click Run.")
            elif r['is_running']:
                warn(f"user {r['user_id']}: a scan is currently running "
                     f"({r['message']})")
            else:
                ok(f"user {r['user_id']}: idle — {r['message']}")
    conn.close()

    if args.live:
        print("\n6. Live price path (yfinance)")
        try:
            import yfinance as yf
            data = yf.download(['TCS.NS', 'SBIN.NS', 'RELIANCE.NS'], period='5d',
                               interval='1d', progress=False, auto_adjust=False)
            if data is None or data.empty:
                fail("yfinance returned no data",
                     "Check network/proxy. The scan cannot fetch prices without this.")
            else:
                ok(f"fetched {len(data)} rows for 3 tickers")
        except Exception as e:
            fail(f"yfinance failed: {type(e).__name__}: {e}",
                 "Check network access to Yahoo Finance from this machine.")
    else:
        print("\n6. Live price path — skipped (re-run with --live to test)")

    print("\n" + "=" * 62)
    if problems:
        print(f"{len(problems)} blocking problem(s) — the scan will not work yet.")
        sys.exit(1)
    if warnings:
        print(f"Ready to scan. {len(warnings)} warning(s) above are non-blocking.")
    else:
        print("All checks passed — ready to scan.")
    print("=" * 62 + "\n")


if __name__ == '__main__':
    main()
