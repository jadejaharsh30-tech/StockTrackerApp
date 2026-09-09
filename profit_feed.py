"""
Profit feed — fetch reported profit history straight from the two Google Apps
Script endpoints into `profit_history`, and apply the resulting Dual verdicts
to `profit_tracker.ath_profit`.

Replaces the build_db.py -> financial_data.duckdb -> import_profit_duckdb.py
chain for this app: the duckdb file was a staging post on the way to SQLite,
and nothing here reads its `classifications` table. import_profit_duckdb.py
still works and shares this module's transform, for when you already have a
duckdb file to load from.

Three things the transform has to get right, all verified against the real feed
(see WORKFLOWS.md 7.8) — do not "simplify" them away:

1. QL1/FYL1 are the NEWEST periods, not the oldest. Confirmed by the feed's own
   TTM column, which equals QL1+QL2+QL3+QL4. The series is reversed on import,
   and refresh() re-runs that check every time and warns if it ever flips.
2. Oldest-end zeros are pre-listing padding, not reported profits, and are
   trimmed. Left in, a TTM straddling the boundary mixes real quarters with
   fake zeros, and for a loss-making company 0 becomes the all-time peak.
3. period_end is a positional sequence ('Q001' oldest .. 'Q048' newest) because
   the feed carries no dates and its columns shift every quarter. Each symbol's
   series is replaced wholesale, so the shifting stays consistent.

Usage:
    python profit_feed.py                 # refresh profit_history from the APIs
    python profit_feed.py --preview       # refresh, then report flag changes
    python profit_feed.py --apply         # refresh, then write the flags
"""

import argparse
import os
import re
import sys

import pandas as pd
import requests

from init_profit_history import get_db, init_profit_history

# Endpoints, overridable without editing code (the Apps Script deployment URL
# changes whenever the script is redeployed).
API_QUARTERLY = os.environ.get(
    'PROFIT_API_QUARTERLY',
    'https://script.google.com/macros/s/AKfycbyS3U6Z7htU-L3gl7Eqvt81ykCyvruZkLrDSw75tJcjcBYxs33k8PAGNTSxSMLQ7KLo/exec'
)
API_YEARLY = os.environ.get(
    'PROFIT_API_YEARLY',
    'https://script.google.com/macros/s/AKfycbwCvDTWq7-3wYC7ac7zP9YwqdCb8CGV2wtftqs-vQWfsWQgYPzBDl9qKkM5wBnOjg55tw/exec'
)
FETCH_TIMEOUT = int(os.environ.get('PROFIT_API_TIMEOUT', '300'))

N_QUARTERS = 48
N_YEARS = 15
SYMBOL_COL = 'NSE CODE'


# ====================== TRANSFORM (shared with import_profit_duckdb) ==========

def clean_number(val):
    """Strip everything except digits, decimal point and minus. None if unusable."""
    if val is None:
        return None
    if isinstance(val, float) and pd.isna(val):
        return None
    text = str(val).strip()
    if text in ('', '-', '--', 'NA', 'N/A', 'nan', 'None'):
        return None
    scrubbed = re.sub(r'[^0-9.\-]', '', text)
    try:
        return float(scrubbed)
    except ValueError:
        return None


def trim_padding(values):
    """
    Drop leading (oldest) zeros/None — pre-listing padding.
    Interior zeros are kept: a genuinely break-even quarter is real data.
    """
    i = 0
    while i < len(values) and (values[i] is None or values[i] == 0):
        i += 1
    return values[i:]


def series_to_records(symbol, newest_first_values, period_type, n_slots):
    """
    Turn one company's newest-first series into oldest-first positional rows.
    The newest value is pinned to the highest slot so ascending sort == oldest
    to newest even when the history is short.
    """
    series = trim_padding(list(reversed(newest_first_values)))
    if not series:
        return [], 0
    offset = n_slots - len(series)
    prefix = 'Q' if period_type == 'Q' else 'A'
    return ([(symbol, period_type, f"{prefix}{offset + i + 1:03d}", float(v))
             for i, v in enumerate(series)], len(series))


def _normalise(df):
    """Upper-case/strip headers; scrub the symbol column."""
    df = df.rename(columns={c: str(c).strip().upper() for c in df.columns})
    if SYMBOL_COL in df.columns:
        df[SYMBOL_COL] = df[SYMBOL_COL].astype(str).str.strip().str.upper()
    return df


def build_records(df_quarterly, df_annual):
    """
    Build profit_history rows from the two feed tables.
    Returns (records, stats, ordering_check) where ordering_check is
    (n_checked, n_matching) for TTM == QL1+QL2+QL3+QL4.
    """
    df_q, df_a = _normalise(df_quarterly), _normalise(df_annual)
    for name, df in (('quarterly', df_q), ('yearly', df_a)):
        if SYMBOL_COL not in df.columns:
            raise ValueError(f"{name} feed has no '{SYMBOL_COL}' column — "
                             f"got: {', '.join(list(df.columns)[:8])}")

    qcols = [f'QL{i}' for i in range(1, N_QUARTERS + 1) if f'QL{i}' in df_q.columns]
    acols = [f'FYL{i}' for i in range(1, N_YEARS + 1) if f'FYL{i}' in df_a.columns]
    if not qcols or not acols:
        raise ValueError("feed is missing QL*/FYL* columns — has the sheet changed shape?")

    records = []
    stats = {'q_symbols': 0, 'a_symbols': 0, 'q_trimmed': 0, 'thin': []}

    for _, row in df_q.iterrows():
        symbol = str(row[SYMBOL_COL]).strip().upper()
        if not symbol or symbol in ('NAN', 'NONE'):
            continue
        values = [clean_number(row[c]) for c in qcols]
        rows, kept = series_to_records(symbol, values, 'Q', N_QUARTERS)
        if not rows:
            continue
        if kept < len(values):
            stats['q_trimmed'] += 1
        if kept < 4:                      # cannot sum a TTM from fewer than four
            stats['thin'].append(symbol)
        stats['q_symbols'] += 1
        records.extend(rows)

    for _, row in df_a.iterrows():
        symbol = str(row[SYMBOL_COL]).strip().upper()
        if not symbol or symbol in ('NAN', 'NONE'):
            continue
        values = [clean_number(row[c]) for c in acols]
        rows, kept = series_to_records(symbol, values, 'A', N_YEARS)
        if not rows:
            continue
        stats['a_symbols'] += 1
        records.extend(rows)

    # Ordering guard: TTM should equal the four newest quarters. Restricted to
    # companies with a full unpadded history — recently-listed names fail this
    # legitimately and would mask a real regression.
    checked = matched = 0
    if 'TTM' in df_a.columns and all(c in df_q.columns for c in ('QL1', 'QL2', 'QL3', 'QL4')):
        q_by_sym = {str(r[SYMBOL_COL]).strip().upper(): r for _, r in df_q.iterrows()}
        for _, arow in df_a.iterrows():
            sym = str(arow[SYMBOL_COL]).strip().upper()
            qrow = q_by_sym.get(sym)
            if qrow is None:
                continue
            ttm = clean_number(arow['TTM'])
            tail = [clean_number(qrow.get(f'QL{i}')) for i in (45, 46, 47, 48)]
            quad = [clean_number(qrow.get(f'QL{i}')) for i in (1, 2, 3, 4)]
            if ttm is None or any(v is None for v in quad) or any(v in (None, 0) for v in tail):
                continue
            checked += 1
            if abs(ttm - sum(quad)) < 1:
                matched += 1

    return records, stats, (checked, matched)


# ====================== WRITE ======================

def write_profit_history(records):
    """Replace each symbol's series wholesale — the feed is a positional snapshot."""
    if not records:
        return 0
    init_profit_history(verbose=False)
    conn = get_db()
    try:
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
        return len(symbols)
    finally:
        conn.close()


# ====================== FETCH + ORCHESTRATE ======================

def fetch_table(url, name, timeout=FETCH_TIMEOUT):
    """GET one endpoint and return a DataFrame. Raises with a usable message."""
    try:
        response = requests.get(url, timeout=timeout)
    except requests.exceptions.Timeout:
        raise RuntimeError(f"{name} feed timed out after {timeout}s")
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"{name} feed unreachable: {e}")

    if response.status_code != 200:
        raise RuntimeError(f"{name} feed returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError:
        # Apps Script serves an HTML login/error page when a deployment has
        # expired or lost its "anyone can access" setting.
        snippet = response.text[:120].replace('\n', ' ')
        raise RuntimeError(
            f"{name} feed returned non-JSON — the Apps Script deployment may have "
            f"expired or need re-authorising. First bytes: {snippet!r}")

    df = pd.DataFrame(payload)
    if df.empty:
        raise RuntimeError(f"{name} feed returned no rows")
    return df


def refresh(progress_callback=None):
    """
    Fetch both endpoints and rewrite profit_history. Returns a summary dict.
    progress_callback(percent, message) is optional.
    """
    def report(pct, msg):
        if progress_callback:
            progress_callback(pct, msg)

    report(5, "Fetching quarterly profit feed...")
    df_q = fetch_table(API_QUARTERLY, "Quarterly")
    report(40, f"Quarterly: {len(df_q)} companies. Fetching yearly feed...")
    df_a = fetch_table(API_YEARLY, "Yearly")
    report(70, f"Yearly: {len(df_a)} companies. Transforming...")

    records, stats, (checked, matched) = build_records(df_q, df_a)
    report(85, f"Writing {len(records)} rows...")
    symbols = write_profit_history(records)

    pct_ok = (100.0 * matched / checked) if checked else 0.0
    ordering_ok = (not checked) or (matched / checked >= 0.9)
    report(100, f"Profit data refreshed: {symbols} symbols, {len(records)} rows.")

    return {
        'rows': len(records),
        'symbols': symbols,
        'quarterly_symbols': stats['q_symbols'],
        'annual_symbols': stats['a_symbols'],
        'trimmed': stats['q_trimmed'],
        'thin': stats['thin'],
        'ordering_checked': checked,
        'ordering_matched': matched,
        'ordering_pct': round(pct_ok, 1),
        'ordering_ok': ordering_ok,
    }


# ====================== FLAG PREVIEW / APPLY ======================

def _computed_flag(verdict):
    """
    ath_profit tracks DUAL only — it gates the FUND category, which is reserved
    for the strict condition. Growth does not set it. None means 'no opinion'.
    """
    flag = verdict.get('profit_flag')
    if flag == 'D':
        return 'Y'
    if flag in ('G', 'N'):
        return 'N'
    return None          # 'N/A' — no usable history, leave the manual flag alone


def preview_flag_changes(user_id, tolerance_pct=0.0):
    """
    What would change in profit_tracker.ath_profit, WITHOUT writing anything.
    Symbols with no usable profit history are reported separately and skipped —
    the feed has no opinion on them, so overwriting would invent one.
    """
    from profit_scanner import classify_many

    conn = get_db()
    try:
        current = {r['symbol']: r['ath_profit'] for r in conn.execute(
            "SELECT symbol, ath_profit FROM profit_tracker WHERE user_id = ?", (user_id,))}
    finally:
        conn.close()

    if not current:
        return {'total': 0, 'changes': [], 'to_y': 0, 'to_n': 0,
                'unchanged': 0, 'no_data': [], 'no_data_count': 0}

    verdicts = classify_many(list(current), tolerance_pct=tolerance_pct)
    changes, no_data, unchanged = [], [], 0
    for symbol, manual in current.items():
        computed = _computed_flag(verdicts.get(symbol, {}))
        if computed is None:
            no_data.append(symbol)
        elif computed == manual:
            unchanged += 1
        else:
            changes.append({'symbol': symbol, 'from': manual, 'to': computed})

    changes.sort(key=lambda c: (c['to'], c['symbol']))
    return {
        'total': len(current),
        'changes': changes,
        'to_y': sum(1 for c in changes if c['to'] == 'Y'),
        'to_n': sum(1 for c in changes if c['to'] == 'N'),
        'unchanged': unchanged,
        'no_data': sorted(no_data),
        'no_data_count': len(no_data),
    }


def apply_flag_changes(user_id, symbols=None, tolerance_pct=0.0):
    """
    Write the computed Dual verdict into profit_tracker.ath_profit.
    symbols=None applies every change the preview found. Symbols with no usable
    profit history are never written.
    """
    preview = preview_flag_changes(user_id, tolerance_pct=tolerance_pct)
    wanted = preview['changes']
    if symbols is not None:
        allow = {str(s).upper() for s in symbols}
        wanted = [c for c in wanted if c['symbol'] in allow]

    if not wanted:
        return {'updated': 0, 'to_y': 0, 'to_n': 0,
                'skipped_no_data': preview['no_data_count']}

    conn = get_db()
    try:
        conn.executemany(
            "UPDATE profit_tracker SET ath_profit = ? WHERE symbol = ? AND user_id = ?",
            [(c['to'], c['symbol'], user_id) for c in wanted])
        conn.commit()
    finally:
        conn.close()

    return {
        'updated': len(wanted),
        'to_y': sum(1 for c in wanted if c['to'] == 'Y'),
        'to_n': sum(1 for c in wanted if c['to'] == 'N'),
        'skipped_no_data': preview['no_data_count'],
    }


# ====================== CLI ======================

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--preview', action='store_true',
                    help='After refreshing, report which ath_profit flags would change')
    ap.add_argument('--apply', action='store_true',
                    help='After refreshing, WRITE the changed ath_profit flags')
    ap.add_argument('--user-id', type=int, default=1,
                    help='User whose profit_tracker flags to preview/apply (default 1)')
    args = ap.parse_args()

    try:
        summary = refresh(progress_callback=lambda p, m: print(f"[{p:3}%] {m}"))
    except RuntimeError as e:
        sys.exit(f"Refresh failed: {e}")

    print(f"\n  quarterly series : {summary['quarterly_symbols']} symbols "
          f"({summary['trimmed']} had pre-listing zeros trimmed)")
    print(f"  annual series    : {summary['annual_symbols']} symbols")
    if summary['ordering_checked']:
        print(f"  ordering check   : TTM == QL1+QL2+QL3+QL4 for "
              f"{summary['ordering_matched']}/{summary['ordering_checked']} "
              f"({summary['ordering_pct']}%)")
        if not summary['ordering_ok']:
            print("  *** WARNING: QL1 may no longer be the newest quarter. ***")
            print("  *** Verify the sheet's column order before trusting these verdicts. ***")
    if summary['thin']:
        print(f"  {len(summary['thin'])} symbols have <4 quarters, so no TTM can be summed")

    if not (args.preview or args.apply):
        return

    pv = preview_flag_changes(args.user_id)
    print(f"\nath_profit changes for user {args.user_id} "
          f"({pv['total']} tracked symbols):")
    print(f"  would change : {len(pv['changes'])}  "
          f"({pv['to_y']} -> Y, {pv['to_n']} -> N)")
    print(f"  already agree: {pv['unchanged']}")
    print(f"  no feed data : {pv['no_data_count']} (left untouched)")

    if args.apply:
        res = apply_flag_changes(args.user_id)
        print(f"\nApplied: {res['updated']} flags updated "
              f"({res['to_y']} -> Y, {res['to_n']} -> N).")
    else:
        print("\n--preview only: nothing written. Re-run with --apply to write.")


if __name__ == '__main__':
    main()
