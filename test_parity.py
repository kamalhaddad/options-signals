"""
Parity test: verify the portable `strategy_core` produces byte-for-byte the same
per-bar signal_score / bullish_count as the existing yfinance backtest
(`backtest.compute_signals_over_time`).

Run from the repo root:
    .venv/bin/python test_parity.py [TICKER ...]

This validates that strategy_core (used by theta_backtest.py) matches the
original signal engine. It needs network access (yfinance) but not ThetaData.
"""

import sys
import os

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import numpy as np
import pandas as pd

from backtest import fetch_intraday_data, compute_signals_over_time
import strategy_core


def run(ticker: str) -> bool:
    df = fetch_intraday_data(ticker)
    if df.empty or len(df) < 50:
        print(f"  {ticker}: insufficient data, skipped")
        return True

    # Reference: the existing backtest computes every bar at once.
    ref = compute_signals_over_time(df)

    # Candidate: walk the frame bar-by-bar, feeding strategy_core a growing
    # window exactly as the LEAN algorithm will at runtime.
    max_score_diff = 0.0
    bullish_mismatches = 0
    compared = 0
    for i in range(1, len(df)):
        window = df.iloc[: i + 1]
        out = strategy_core.compute_latest_signal(window)
        ref_score = ref["signal_score"].iloc[i]
        ref_bull = int(ref["bullish_count"].iloc[i])

        if pd.isna(ref_score):
            continue
        compared += 1
        score_diff = abs(out["score"] - ref_score)
        max_score_diff = max(max_score_diff, score_diff)
        if out["bullish_count"] != ref_bull:
            bullish_mismatches += 1

    ok = max_score_diff < 1e-9 and bullish_mismatches == 0
    status = "PASS" if ok else "FAIL"
    print(f"  {ticker}: {status}  bars={compared}  "
          f"max_score_diff={max_score_diff:.2e}  bullish_mismatches={bullish_mismatches}")
    return ok


def main():
    tickers = sys.argv[1:] or ["NVDA", "AAPL", "SPY"]
    print(f"Parity check: strategy_core vs backtest.compute_signals_over_time")
    print(f"{'='*72}")
    results = [run(t) for t in tickers]
    print(f"{'='*72}")
    if all(results):
        print("ALL PASS — ported signal core matches the existing backtest exactly.")
        sys.exit(0)
    else:
        print("FAIL — divergence detected; do not trust LEAN P&L until resolved.")
        sys.exit(1)


if __name__ == "__main__":
    main()
