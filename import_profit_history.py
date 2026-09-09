"""
Import reported net-profit history into the `profit_history` table.

Accepts CSV or Excel in either layout:

  LONG  — one row per (symbol, period):
          SYMBOL | PERIOD_TYPE | PERIOD_END  | NET_PROFIT
          SBIN   | Q           | 2024-06-30  | 17035
          (PERIOD_TYPE: Q = quarterly, A = annual)

  WIDE  — one row per symbol, one column per period:
          SYMBOL | 2013-06-30 | 2013-09-30 | ...
          SBIN   | 3241       | 2375       | ...
          Pass --type Q or --type A to say which series the columns are.

Usage:
    python import_profit_history.py quarterly.xlsx --type Q
    python import_profit_history.py annual.csv      --type A
    python import_profit_history.py combined.xlsx              # long format
    python import_profit_history.py data.xlsx --type Q --dry-run

Re-importing is safe: rows are upserted on (symbol, period_type, period_end).
"""

import argparse
import os
import sys

import pandas as pd

from init_profit_history import get_db, init_profit_history

LONG_REQUIRED = {'SYMBOL', 'PERIOD_TYPE', 'PERIOD_END', 'NET_PROFIT'}


def _read(path):
    if not os.path.exists(path):
        sys.exit(f"File not found: {path}")
    if path.lower().endswith(('.xlsx', '.xlsm', '.xls')):
        return pd.read_excel(path)
    return pd.read_csv(path)


def _norm_period(value):
    """Normalise a period label to a sortable ISO-ish string."""
    if isinstance(value, str):
        text = value.strip()
        # Already ISO-ish? keep as-is so sorting stays lexicographic.
        if len(text) >= 7 and text[:4].isdigit() and text[4] in '-/':
            return text.replace('/', '-')[:10]
    try:
        return pd.to_datetime(value).strftime('%Y-%m-%d')
    except Exception:
        return str(value).strip()


def _clean_profit(value):
    """Parse a profit cell, tolerating commas, currency symbols and blanks."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, str):
        text = value.strip().replace(',', '').replace('₹', '').replace('%', '')
        if text in ('', '-', '--', 'NA', 'N/A'):
            return None
        # Accounting-style negatives: (123) -> -123
        if text.startswith('(') and text.endswith(')'):
            text = '-' + text[1:-1]
        try:
            return float(text)
        except ValueError:
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_records(df, forced_type=None):
    """Normalise either layout into [(symbol, period_type, period_end, profit)]."""
    df = df.rename(columns={c: str(c).strip().upper() for c in df.columns})

    if LONG_REQUIRED.issubset(set(df.columns)):
        records = []
        for _, row in df.iterrows():
            symbol = str(row['SYMBOL']).strip().upper()
            ptype = str(row['PERIOD_TYPE']).strip().upper()[:1]
            if not symbol or ptype not in ('Q', 'A'):
                continue
            profit = _clean_profit(row['NET_PROFIT'])
            if profit is None:
                continue
            records.append((symbol, ptype, _norm_period(row['PERIOD_END']), profit))
        return records, 'long'

    # Wide layout
    if forced_type not in ('Q', 'A'):
        sys.exit(
            "Wide layout detected but --type not given.\n"
            "Re-run with --type Q (quarterly) or --type A (annual), or supply a\n"
            f"long-format file with columns: {', '.join(sorted(LONG_REQUIRED))}"
        )

    symbol_col = df.columns[0]
    period_cols = [c for c in df.columns[1:]]
    records = []
    for _, row in df.iterrows():
        symbol = str(row[symbol_col]).strip().upper()
        if not symbol or symbol == 'NAN':
            continue
        for col in period_cols:
            profit = _clean_profit(row[col])
            if profit is None:
                continue
            records.append((symbol, forced_type, _norm_period(col), profit))
    return records, 'wide'


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('path', help='CSV or Excel file to import')
    parser.add_argument('--type', dest='ptype', choices=['Q', 'A'],
                        help='Series type for WIDE files (Q=quarterly, A=annual)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Parse and report, but write nothing')
    args = parser.parse_args()

    df = _read(args.path)
    records, layout = to_records(df, args.ptype)

    if not records:
        sys.exit("No usable rows parsed — check the column headers and values.")

    symbols = sorted({r[0] for r in records})
    per_symbol = {}
    for sym, ptype, _, _ in records:
        per_symbol.setdefault(sym, {'Q': 0, 'A': 0})[ptype] += 1
    depths = [v['Q'] for v in per_symbol.values() if v['Q']]

    print(f"Parsed {len(records)} rows ({layout} layout) across {len(symbols)} symbols.")
    if depths:
        print(f"  quarterly depth: min {min(depths)}, max {max(depths)}, "
              f"median {sorted(depths)[len(depths) // 2]}")
    thin = [s for s, v in per_symbol.items() if v['Q'] and v['Q'] < 12]
    if thin:
        print(f"  ⚠ {len(thin)} symbol(s) have <12 quarters — their ATH verdict "
              f"will be N/A: {', '.join(thin[:8])}{'...' if len(thin) > 8 else ''}")

    if args.dry_run:
        print("\n--dry-run: nothing written. Sample:")
        for r in records[:5]:
            print("   ", r)
        return

    init_profit_history(verbose=False)
    conn = get_db()
    try:
        conn.executemany(
            '''INSERT INTO profit_history (symbol, period_type, period_end, net_profit)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(symbol, period_type, period_end)
               DO UPDATE SET net_profit = excluded.net_profit''',
            records
        )
        conn.commit()
    finally:
        conn.close()

    print(f"\nImported {len(records)} rows into profit_history.")


if __name__ == '__main__':
    main()
