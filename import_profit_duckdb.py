"""
Load profit history from the colleague-maintained DuckDB feed into profit_history.

Source: financial_data.duckdb, built daily by build_db.py from two Google Apps
Script endpoints. Relevant tables:

    quarterly : ACCORD CODE, COMPANY NAME, ISIN, NSE CODE, BSE CODE, QL1..QL48
    yearly    : ... TTM, FYL1..FYL15, MCAP, TRADING STATUS, LISTING STATUS

Three things this importer has to get right:

1. ORDER. QL1/FYL1 are the MOST RECENT periods, not the oldest — verified
   against the feed's own TTM column, which equals QL1+QL2+QL3+QL4 exactly.
   profit_history stores oldest-first, so the series is reversed here.

2. PRE-LISTING PADDING. Companies listed less than 12 years ago carry 0.0 in
   their oldest quarters (e.g. 360ONE has QL40..QL48 = 0). Those are not
   reported profits. They are trimmed from the old end, otherwise a rolling
   TTM straddling the boundary sums real quarters with fake zeros, and for a
   loss-making company 0 would become the all-time peak.

3. POSITIONAL LABELS. The feed carries no period dates, only positions that
   shift every quarter. period_end is therefore a zero-padded sequence where
   HIGHER = MORE RECENT ('Q001' oldest .. 'Q048' newest), which sorts
   correctly ascending. Each symbol's series is replaced wholesale on import,
   so the shifting is consistent. Do not mix these with date-labelled rows
   for the same symbol — they would interleave wrongly.

Usage:
    python import_profit_duckdb.py financial_data.duckdb
    python import_profit_duckdb.py financial_data.duckdb --dry-run
    python import_profit_duckdb.py financial_data.duckdb --active-only
"""

import argparse
import os
import sys

try:
    import duckdb
except ImportError:
    sys.exit("duckdb is required:  pip install duckdb")

from init_profit_history import get_db, init_profit_history
from profit_scanner import MIN_QUARTERS_FOR_ATH

N_QUARTERS = 48
N_YEARS = 15

# Quarters needed before the quarterly-ATH leg is meaningful. TTM itself only
# needs 4 quarters now (it is computed once, not rolled) and is judged against
# the reported FY series instead.
MIN_QUARTERS_FOR_VERDICT = MIN_QUARTERS_FOR_ATH


def trim_padding(values):
    """
    Drop leading (oldest) zeros/None — pre-listing padding.
    Interior zeros are preserved: a genuinely break-even quarter is real data.
    """
    i = 0
    while i < len(values) and (values[i] is None or values[i] == 0):
        i += 1
    return values[i:]


def build_records(db_path, active_only=False):
    con = duckdb.connect(db_path, read_only=True)
    try:
        qcols = ', '.join(f'"QL{i}"' for i in range(1, N_QUARTERS + 1))
        acols = ', '.join(f'"FYL{i}"' for i in range(1, N_YEARS + 1))

        quarterly = con.execute(f'''
            SELECT "NSE CODE" AS sym, {qcols}
            FROM quarterly
            WHERE "NSE CODE" IS NOT NULL AND TRIM("NSE CODE") <> ''
        ''').fetchall()

        status_filter = ''
        if active_only:
            status_filter = "AND \"TRADING STATUS\" = 'Active'"
        annual = con.execute(f'''
            SELECT "NSE CODE" AS sym, {acols}
            FROM yearly
            WHERE "NSE CODE" IS NOT NULL AND TRIM("NSE CODE") <> ''
            {status_filter}
        ''').fetchall()

        # TTM cross-check — catches a future feed change that flips the ordering.
        # Restricted to companies with a full unpadded history: recently-listed
        # names legitimately fail this (their newest quarters can themselves be
        # zero while an annual TTM exists), which would mask a real regression.
        check = con.execute('''
            SELECT COUNT(*) AS n,
                   COUNT(CASE WHEN ABS(y.TTM - (q.QL1+q.QL2+q.QL3+q.QL4)) < 1 THEN 1 END) AS ok
            FROM quarterly q JOIN yearly y USING ("ACCORD CODE")
            WHERE y.TTM IS NOT NULL AND q.QL1 IS NOT NULL
              AND q.QL45 <> 0 AND q.QL46 <> 0 AND q.QL47 <> 0 AND q.QL48 <> 0
        ''').fetchone()
    finally:
        con.close()

    records = []
    stats = {'q_symbols': 0, 'a_symbols': 0, 'q_trimmed': 0, 'thin': []}

    for row in quarterly:
        symbol, values = str(row[0]).strip().upper(), list(row[1:])
        if not symbol:
            continue
        series = trim_padding(list(reversed(values)))  # reversed -> oldest first
        if not series:
            continue
        if len(series) < len(values):
            stats['q_trimmed'] += 1
        if len(series) < MIN_QUARTERS_FOR_VERDICT:
            stats['thin'].append(symbol)
        stats['q_symbols'] += 1
        offset = N_QUARTERS - len(series)   # keep newest pinned at Q048
        for i, val in enumerate(series):
            records.append((symbol, 'Q', f"Q{offset + i + 1:03d}", float(val)))

    for row in annual:
        symbol, values = str(row[0]).strip().upper(), list(row[1:])
        if not symbol:
            continue
        series = trim_padding(list(reversed(values)))
        if not series:
            continue
        stats['a_symbols'] += 1
        offset = N_YEARS - len(series)
        for i, val in enumerate(series):
            records.append((symbol, 'A', f"A{offset + i + 1:03d}", float(val)))

    return records, stats, check


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('duckdb_path', nargs='?', default='financial_data.duckdb')
    parser.add_argument('--dry-run', action='store_true', help='Parse and report, write nothing')
    parser.add_argument('--active-only', action='store_true',
                        help="Only import annual rows where TRADING STATUS = 'Active'")
    args = parser.parse_args()

    if not os.path.exists(args.duckdb_path):
        sys.exit(f"Not found: {args.duckdb_path}")

    records, stats, check = build_records(args.duckdb_path, args.active_only)
    if not records:
        sys.exit("No usable rows — check that the feed still has an 'NSE CODE' column.")

    total, ok = check
    pct = (100.0 * ok / total) if total else 0.0
    print(f"Ordering check: TTM == QL1+QL2+QL3+QL4 for {ok}/{total} "
          f"full-history companies ({pct:.1f}%).")
    if total and ok / total < 0.9:
        print("  *** WARNING: the newest-first assumption may no longer hold. ***")
        print("  *** Verify QL1 is still the latest quarter before trusting this. ***")

    print(f"\nParsed {len(records)} rows")
    print(f"  quarterly series: {stats['q_symbols']} symbols "
          f"({stats['q_trimmed']} had pre-listing zeros trimmed)")
    print(f"  annual series   : {stats['a_symbols']} symbols")
    if stats['thin']:
        sample = ', '.join(stats['thin'][:8])
        print(f"  ⚠ {len(stats['thin'])} symbols have <{MIN_QUARTERS_FOR_VERDICT} real quarters "
              f"— verdict will be N/A "
              f"({sample}{'...' if len(stats['thin']) > 8 else ''})")

    if args.dry_run:
        print("\n--dry-run: nothing written. Sample:")
        for r in records[:4]:
            print("   ", r)
        return

    init_profit_history(verbose=False)
    conn = get_db()
    try:
        # The feed is a positional snapshot whose columns shift each quarter,
        # so replace each symbol's series wholesale rather than upserting.
        symbols = sorted({r[0] for r in records})
        conn.executemany("DELETE FROM profit_history WHERE symbol = ?",
                         [(s,) for s in symbols])
        conn.executemany(
            '''INSERT INTO profit_history (symbol, period_type, period_end, net_profit)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(symbol, period_type, period_end)
               DO UPDATE SET net_profit = excluded.net_profit''',
            records)
        conn.commit()

        tracked = conn.execute('''
            SELECT COUNT(DISTINCT p.symbol) FROM profit_tracker p
            WHERE EXISTS (SELECT 1 FROM profit_history h WHERE h.symbol = p.symbol)
        ''').fetchone()[0]
        total_tracked = conn.execute(
            "SELECT COUNT(DISTINCT symbol) FROM profit_tracker").fetchone()[0]
    finally:
        conn.close()

    print(f"\nImported {len(records)} rows for {len(symbols)} symbols.")
    print(f"Coverage of your tracked universe: {tracked}/{total_tracked} symbols.")
    if tracked < total_tracked:
        print("  (uncovered symbols are usually renamed/demerged tickers — "
              "e.g. TATAMOTORS is now TMCV + TMPV in the feed)")


if __name__ == '__main__':
    main()
