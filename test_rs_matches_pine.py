"""
Check calculate_rs_outperformance against the TradingView indicator's formula.

The Pine source ("Anchored & ATH RS - Harshraj") anchors like this:

    rs          = close / comp * 100
    rs_213_back = that rs at bar_index == last_bar_index - barsBackInput   # 212
    anchoredRS  = rs / rs_213_back * 100
    ath_value   = running max of anchoredRS from the anchor bar onward

so the anchor sits 212 BARS BACK from the latest bar and the window holds 213
bars. Pandas slices by row count, so LOOKBACK must be 213 to land there. This
test exists because getting that off by two shifts every RS number on the page
by a few percent while each value still looks entirely ordinary.

Run directly:  python test_rs_matches_pine.py
"""
import numpy as np
import pandas as pd

import scanner_engine as se

BARS_BACK = 212          # Pine `barsBackInput` default


def pine_anchored_rs(stock, index, bars_back=BARS_BACK):
    """Straight transcription of the Pine anchoring, for comparison."""
    raw = (stock / index) * 100.0
    anchor_pos = len(raw) - 1 - bars_back
    line = raw.iloc[anchor_pos:] / raw.iloc[anchor_pos] * 100.0
    return round(float(line.iloc[-1]), 2), round(float(line.max()), 2)


def synthetic(n, seed):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range('2024-01-02', periods=n)
    index = pd.Series(20000 * np.cumprod(1 + rng.normal(0.0004, 0.006, n)), index=dates)
    stock = pd.Series(500 * np.cumprod(1 + rng.normal(0.0011, 0.017, n)), index=dates)
    return stock, index


def main():
    assert se.RS_BARS_BACK == BARS_BACK, (
        f"RS_BARS_BACK is {se.RS_BARS_BACK}, the indicator uses {BARS_BACK}")
    assert se.LOOKBACK == BARS_BACK + 1, (
        f"LOOKBACK is {se.LOOKBACK}; slicing by row count needs {BARS_BACK + 1} "
        f"to anchor {BARS_BACK} bars back")

    failures = 0
    for n, seed in ((248, 5), (260, 12), (300, 99), (213, 4), (400, 77)):
        stock, index = synthetic(n, seed)
        want_curr, want_ath = pine_anchored_rs(stock, index)

        today = pd.to_datetime((stock.index[-1] + pd.Timedelta(days=1)).date()).normalize()
        got = se.calculate_rs_outperformance(
            stock[stock.index < today], float(stock.iloc[-1]), today, index)

        ok = (got['current_rs'], got['ath_rs']) == (want_curr, want_ath)
        failures += not ok
        print(f"n={n:<4} pine curr={want_curr:<8} ath={want_ath:<8} | "
              f"ours curr={got['current_rs']:<8} ath={got['ath_rs']:<8} "
              f"window={got['rs_window']}  {'ok' if ok else 'MISMATCH'}")

    # The anchor must land exactly BARS_BACK rows before the last one.
    stock, index = synthetic(300, 1)
    today = pd.to_datetime((stock.index[-1] + pd.Timedelta(days=1)).date()).normalize()
    got = se.calculate_rs_outperformance(
        stock[stock.index < today], float(stock.iloc[-1]), today, index)
    assert got['rs_window'] - 1 == BARS_BACK, got['rs_window']
    print(f"\nanchor offset: {got['rs_window'] - 1} bars back (indicator: {BARS_BACK})")

    if failures:
        raise SystemExit(f"{failures} mismatch(es) against the Pine formula")
    print("RS matches the TradingView indicator's anchoring.")


if __name__ == '__main__':
    main()
