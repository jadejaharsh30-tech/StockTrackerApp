"""
Profit Scanner — Dual/Growth classification from reported net-profit history.

Runs as Phase 4 of the ATH scan, over the symbols that hit a price ATH today.
Reads exclusively from the local `profit_history` table (no network), so it is
fast enough to run on every scan and testable offline.

Definitions (as specified by the user):

    Dual   (D) = latest QUARTER profit at ATH  AND  TTM/yearly profit at ATH
    Growth (G) = TTM/yearly profit at ATH      AND  latest quarter > year-ago quarter

Both buckets require TTM/yearly to be at an ATH. They differ only in the
quarterly leg, and D's condition is strictly stronger than G's (a quarter at
ATH necessarily beats the year-ago quarter), so classification is ordered:
test D first, then G.
"""

import os
import sqlite3

DATABASE = os.environ.get(
    'DATABASE_PATH',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tracker.db')
)

# Minimum series lengths before a verdict is meaningful.
MIN_TTM_POINTS = 8      # rolling-4 TTM values needed to call an "all-time" high
MIN_ANNUAL_POINTS = 3   # annual fallback when quarterly history is too short
QUARTERS_PER_YEAR = 4

NA = 'N/A'


def get_db():
    conn = sqlite3.connect(DATABASE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    return conn


# ====================== SERIES HELPERS ======================

def get_series(conn, symbol, period_type):
    """Return [(period_end, net_profit), ...] sorted oldest -> newest."""
    rows = conn.execute(
        '''SELECT period_end, net_profit FROM profit_history
           WHERE symbol = ? AND period_type = ? AND net_profit IS NOT NULL
           ORDER BY period_end ASC''',
        (symbol.upper(), period_type)
    ).fetchall()
    return [(r['period_end'], float(r['net_profit'])) for r in rows]


def compute_ttm(quarterly_values):
    """Rolling 4-quarter sum. Input oldest->newest; output oldest->newest."""
    if len(quarterly_values) < QUARTERS_PER_YEAR:
        return []
    return [
        sum(quarterly_values[i - QUARTERS_PER_YEAR + 1:i + 1])
        for i in range(QUARTERS_PER_YEAR - 1, len(quarterly_values))
    ]


def _at_ath(series, tolerance_pct=0.0):
    """
    Is the latest value of `series` at an all-time high?

    Sign-safe: the threshold is stepped DOWN from the max by a fraction of its
    magnitude, so this behaves correctly for negative (loss-making) maxima —
    unlike `max * 0.999`, which raises the bar above the max when max < 0.
    """
    if not series:
        return None
    current = series[-1]
    peak = max(series)
    threshold = peak - abs(peak) * (tolerance_pct / 100.0)
    return current >= threshold


# ====================== CLASSIFICATION ======================

def classify_symbol(conn, symbol, tolerance_pct=0.0):
    """
    Classify one symbol. Returns a dict with the three component verdicts,
    the combined flag, and diagnostics describing what the verdict was based on.

    profit_flag:  'D' | 'G' | 'N' | 'N/A'
    """
    result = {
        'symbol': symbol.upper(),
        'profit_ttm_ath': NA,
        'profit_qtr_ath': NA,
        'profit_yoy': NA,
        'profit_flag': NA,
        'profit_basis': None,     # 'TTM' | 'ANNUAL' | None
        'profit_points': 0,       # size of the window the ATH was judged against
    }

    quarterly = get_series(conn, symbol, 'Q')
    annual = get_series(conn, symbol, 'A')
    q_values = [v for _, v in quarterly]
    a_values = [v for _, v in annual]

    # --- TTM / yearly leg -------------------------------------------------
    ttm = compute_ttm(q_values)
    if len(ttm) >= MIN_TTM_POINTS:
        result['profit_ttm_ath'] = 'Y' if _at_ath(ttm, tolerance_pct) else 'N'
        result['profit_basis'] = 'TTM'
        result['profit_points'] = len(ttm)
    elif len(a_values) >= MIN_ANNUAL_POINTS:
        # Quarterly history too shallow for a trustworthy TTM ATH — fall back
        # to the reported annual series.
        result['profit_ttm_ath'] = 'Y' if _at_ath(a_values, tolerance_pct) else 'N'
        result['profit_basis'] = 'ANNUAL'
        result['profit_points'] = len(a_values)

    # --- quarterly ATH leg ------------------------------------------------
    if len(q_values) >= MIN_TTM_POINTS:
        result['profit_qtr_ath'] = 'Y' if _at_ath(q_values, tolerance_pct) else 'N'

    # --- quarterly YoY leg ------------------------------------------------
    if len(q_values) >= QUARTERS_PER_YEAR + 1:
        result['profit_yoy'] = 'Y' if q_values[-1] > q_values[-1 - QUARTERS_PER_YEAR] else 'N'

    # --- combine ----------------------------------------------------------
    if result['profit_ttm_ath'] == NA:
        result['profit_flag'] = NA           # no usable history at all
    elif result['profit_ttm_ath'] != 'Y':
        result['profit_flag'] = 'N'          # TTM/yearly gate failed
    elif result['profit_qtr_ath'] == 'Y':
        result['profit_flag'] = 'D'          # both legs at ATH
    elif result['profit_yoy'] == 'Y':
        result['profit_flag'] = 'G'          # TTM at ATH + quarter beats YoY
    else:
        result['profit_flag'] = 'N'

    return result


def classify_many(symbols, tolerance_pct=0.0, conn=None):
    """Classify a list of symbols. Returns {symbol: result_dict}."""
    own_conn = conn is None
    conn = conn or get_db()
    try:
        return {
            s.upper(): classify_symbol(conn, s, tolerance_pct)
            for s in symbols
        }
    finally:
        if own_conn:
            conn.close()


def get_coverage(conn=None):
    """How many symbols have usable profit history — for UI warnings."""
    own_conn = conn is None
    conn = conn or get_db()
    try:
        row = conn.execute('''
            SELECT
              COUNT(DISTINCT CASE WHEN period_type='Q' THEN symbol END) AS q_symbols,
              COUNT(DISTINCT CASE WHEN period_type='A' THEN symbol END) AS a_symbols,
              COUNT(DISTINCT symbol) AS symbols
            FROM profit_history
        ''').fetchone()
        return dict(row) if row else {'q_symbols': 0, 'a_symbols': 0, 'symbols': 0}
    except sqlite3.OperationalError:
        return {'q_symbols': 0, 'a_symbols': 0, 'symbols': 0}
    finally:
        if own_conn:
            conn.close()
