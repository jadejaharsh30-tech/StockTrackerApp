"""
Profit Scanner — Dual/Growth classification from reported net-profit history.

Runs as Phase 4 of the ATH scan, over the symbols that hit a price ATH today.
Reads exclusively from the local `profit_history` table (no network), so it is
fast enough to run on every scan and testable offline.

Definitions:

    Dual   (D) = latest QUARTER at ATH  AND  TTM at ATH
    Growth (G) = TTM at ATH             AND  latest quarter > year-ago quarter

Both buckets require TTM to be at an ATH. They differ only in the quarterly
leg, and D's condition is strictly stronger than G's (a quarter at ATH
necessarily beats the year-ago quarter), so D is tested first.

TTM-at-ATH — the important subtlety
-----------------------------------
TTM is computed ONCE from the four most recent quarters (QL1+QL2+QL3+QL4) and
compared against the REPORTED financial-year series. The comparison run is:

    [TTM, FY1, FY2, ... FY15]

TTM is at ATH when it is >= every FY in that run AND the peak FY is positive
(a loss-making peak is not a record).

This is deliberately NOT a rolling 4-quarter maximum. A rolling window like
QL3..QL6 spans two part-years — Sep-24 through Jun-25, say — which is not a
period the company ever reported. A "record" inside such a window is an
artifact of where the window sits, not a result the business actually posted.

Quarter-at-ATH is separate and unchanged: QL1 against max(QL1..QL48).

Worked example
--------------
    QL1=120 QL2=110 QL3=100 QL4=95 QL5=118 QL6=90 QL7=85 QL8=80
    FY1=423 FY2=380 FY3=410 FY4=300

    TTM = 120+110+100+95 = 425   (computed once, not rolled)
    425 >= max(FY) = 423         -> TTM at ATH
    QL1 120 >= max(QL) = 120     -> quarter at ATH
    => Dual

    With FY3 = 450 instead: 425 < 450 -> TTM not at ATH -> overall False,
    even though the quarter is still a record on its own.

Edge case: when QL1 is the March quarter, QL1..QL4 spans exactly one financial
year, so TTM equals FY1. The test uses >=, so equality passes — it only fails
if an EARLIER FY beat it.
"""

import os
import sqlite3

DATABASE = os.environ.get(
    'DATABASE_PATH',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tracker.db')
)

# Minimum series lengths before a verdict is meaningful.
QUARTERS_PER_YEAR = 4
MIN_ANNUAL_POINTS = 3    # reported FYs needed before "all-time" means anything
MIN_QUARTERS_FOR_ATH = 8 # quarters needed before quarter-at-ATH is meaningful

# Retained only because import_profit_duckdb.py derives its "thin history"
# warning from it. TTM itself is no longer a rolling series.
MIN_TTM_POINTS = QUARTERS_PER_YEAR

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
    """
    Trailing twelve months from the FOUR MOST RECENT quarters, computed once.

    Deliberately NOT a rolling series. A rolling window such as QL3..QL6 spans
    two part-years (e.g. Sep-24 to Jun-25) — a period the company never
    reported to anyone — so a "record" found inside one is an artifact of where
    the window happens to sit rather than a result the business actually posted.

    Input is oldest->newest, so the newest four are the tail.
    """
    if len(quarterly_values) < QUARTERS_PER_YEAR:
        return None
    return sum(quarterly_values[-QUARTERS_PER_YEAR:])


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
        'profit_basis': None,     # 'FY' once the TTM-vs-FY comparison ran
        'profit_points': 0,       # number of reported FYs TTM was judged against
        'profit_ttm': None,       # the single TTM figure (QL1+QL2+QL3+QL4)
        'profit_peak_fy': None,   # highest reported FY it had to beat
    }

    quarterly = get_series(conn, symbol, 'Q')
    annual = get_series(conn, symbol, 'A')
    q_values = [v for _, v in quarterly]
    a_values = [v for _, v in annual]

    # --- TTM / yearly leg -------------------------------------------------
    # TTM is computed once from the latest four quarters and compared against
    # the REPORTED financial-year series: the run is [TTM, FY1 .. FY15].
    # TTM is at ATH when it matches or beats every reported FY, and the peak FY
    # must itself be positive — a loss-making peak is not a record to beat.
    ttm = compute_ttm(q_values)
    result['profit_ttm'] = ttm
    if ttm is not None and len(a_values) >= MIN_ANNUAL_POINTS:
        peak_fy = max(a_values)
        threshold = peak_fy - abs(peak_fy) * (tolerance_pct / 100.0)
        result['profit_ttm_ath'] = 'Y' if (peak_fy > 0 and ttm >= threshold) else 'N'
        result['profit_basis'] = 'FY'
        result['profit_points'] = len(a_values)
        result['profit_peak_fy'] = peak_fy

    # --- quarterly ATH leg ------------------------------------------------
    # Latest quarter (QL1) against the whole quarterly history (QL1..QL48).
    if len(q_values) >= MIN_QUARTERS_FOR_ATH:
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
    elif result['profit_qtr_ath'] == NA and result['profit_yoy'] == NA:
        # TTM qualifies but there is not enough quarterly history to judge
        # either quarterly leg — that is unknown, not a failure.
        result['profit_flag'] = NA
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
