"""
Compare calculate_rs_outperformance against the TradingView indicator's formula.

The Pine source ("Anchored & ATH RS - Harshraj") anchors like this:

    rs          = close / comp * 100
    rs_213_back = that rs at bar_index == last_bar_index - barsBackInput   # 212
                  ... or, when the listing is SHORTER than barsBackInput and that
                  bar index is negative, at bar_index == 0 (the else-branch)
    anchoredRS  = rs / rs_213_back * 100
    ath_value   = running max of anchoredRS from the anchor bar onward

Two things this checks:

  1. MECHANISM (hard assert). Our anchor must land exactly RS_BARS_BACK bars
     before the latest bar, and the numbers must equal the Pine formula fed the
     same offset. Slicing by row count puts the anchor one row further in than
     the offset suggests, which is easy to get wrong by one or two.

  2. OFFSET (reported, not asserted). scanner_engine.LOOKBACK is currently 211
     rows = 210 bars back, while the indicator's barsBackInput default is 212.
     That gap is unresolved and deliberately left alone pending a LIVE-MARKET
     comparison against the chart — a post-market run cannot settle it, because
     yfinance's after-hours bars are the other suspect. This prints the gap
     loudly rather than failing, so the decision stays visible.

  3. SHORT LISTINGS (hard assert). Pine falls back to anchoring at the first
     bar; so do we. A starred row IS comparable to the chart.

Run directly:  python test_rs_matches_pine.py
"""
import numpy as np
import pandas as pd

import scanner_engine as se


def pine_anchored_rs(stock, index, bars_back):
    """Straight transcription of the Pine anchoring, for comparison."""
    raw = (stock / index) * 100.0
    anchor_pos = len(raw) - 1 - bars_back
    if anchor_pos < 0:              # the `else if bar_index == 0` branch
        anchor_pos = 0
    line = raw.iloc[anchor_pos:] / raw.iloc[anchor_pos] * 100.0
    return round(float(line.iloc[-1]), 2), round(float(line.max()), 2)


def synthetic(n, seed):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range('2024-01-02', periods=n)
    index = pd.Series(20000 * np.cumprod(1 + rng.normal(0.0004, 0.006, n)), index=dates)
    stock = pd.Series(500 * np.cumprod(1 + rng.normal(0.0011, 0.017, n)), index=dates)
    return stock, index


def ours(stock, index):
    today = pd.to_datetime((stock.index[-1] + pd.Timedelta(days=1)).date()).normalize()
    return se.calculate_rs_outperformance(
        stock[stock.index < today], float(stock.iloc[-1]), today, index)


def main():
    assert se.RS_BARS_BACK == se.LOOKBACK - 1, "RS_BARS_BACK must stay derived from LOOKBACK"
    offset = se.RS_BARS_BACK
    print(f"configured: LOOKBACK={se.LOOKBACK} rows -> anchor {offset} bars back")
    print(f"indicator : barsBackInput={se.PINE_BARS_BACK} -> anchor {se.PINE_BARS_BACK} bars back\n")

    failures = 0
    for n, seed in ((248, 5), (260, 12), (300, 99), (400, 77)):
        stock, index = synthetic(n, seed)
        want = pine_anchored_rs(stock, index, offset)
        got = ours(stock, index)
        ok = (got['current_rs'], got['ath_rs']) == want
        failures += not ok
        print(f"n={n:<4} pine@{offset} curr={want[0]:<8} ath={want[1]:<8} | "
              f"ours curr={got['current_rs']:<8} ath={got['ath_rs']:<8} "
              f"window={got['rs_window']}  {'ok' if ok else 'MISMATCH'}")

    stock, index = synthetic(300, 1)
    got = ours(stock, index)
    assert got['rs_window'] - 1 == offset, got['rs_window']

    # Short listing: Pine anchors at bar 0, and so must we.
    stock, index = synthetic(120, 21)
    want = pine_anchored_rs(stock, index, offset)      # anchor_pos clamps to 0
    got = ours(stock, index)
    ok = (got['current_rs'], got['ath_rs']) == want
    failures += not ok
    print(f"\nshort listing (120 bars, anchored at bar 0): "
          f"pine curr={want[0]} ath={want[1]} | ours curr={got['current_rs']} "
          f"ath={got['ath_rs']} window={got['rs_window']}  {'ok' if ok else 'MISMATCH'}")

    if failures:
        raise SystemExit(f"{failures} mismatch(es) against the Pine formula")

    print("\nRS matches the Pine formula at the configured offset.")
    if offset != se.PINE_BARS_BACK:
        print(f"NOTE: offset is {offset} bars, the indicator's default is "
              f"{se.PINE_BARS_BACK}. Unresolved on purpose — verify against the "
              f"chart during LIVE market hours before changing it.")


if __name__ == '__main__':
    main()
