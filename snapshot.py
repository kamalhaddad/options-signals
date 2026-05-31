"""
Warm the local cache for a date-range so later backtests run OFFLINE (Terminal-free, instant).

The backtest's slow part is network round-trips to the ThetaData Terminal, not compute.
This runs the strategy once per ticker ONLINE — which pulls exactly the per-contract
quotes/OI/greeks + stock bars (and, with --gex flags, the GEX bulk) that an OFFLINE
re-run of the same window will read back from cache. After this, iterate on tunings with
`--offline` and no Terminal.

Coverage note: warming uses one representative config (defaults, or the --gex flags you
pass). Nearby tunings reuse ~the same contracts, so offline sweeps mostly hit cache; any
miss is counted (an offline run reports if its snapshot was incomplete) and that entry is
simply skipped. Re-warm if you change moneyness/signals drastically.

Usage:
  .venv/bin/python snapshot.py --tickers all --start 2026-04-28 --end 2026-05-29 [--gex-flip --gex-size --gex-walls]
  .venv/bin/python theta_backtest.py --tickers all --start 2026-04-28 --end 2026-05-29 --offline ...
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

import strategy_core as sc
import theta_backtest as tb
from thetadata_client import ThetaClient


def main():
    ap = argparse.ArgumentParser(description="Warm the cache for offline backtests")
    ap.add_argument("--tickers", default="all", help="comma-separated, or 'all' for the watchlist")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--gex-size", action="store_true", help="also warm per-ticker GEX bulk (size)")
    ap.add_argument("--gex-walls", action="store_true", help="also warm per-ticker GEX bulk (walls)")
    ap.add_argument("--gex-max-dte", type=int, default=14)
    args = ap.parse_args()

    if args.tickers.lower() in ("all", "watchlist"):
        import config
        tickers = list(config.WATCHLIST)
    else:
        tickers = [t.strip().upper() for t in args.tickers.split(",")]
    if tb.MARKET_INDEX not in tickers:                 # needed for the SPY timing gate
        tickers = [tb.MARKET_INDEX] + tickers
    start, end = tb.to_int_date(args.start), tb.to_int_date(args.end)
    weights = tb.SIGNAL_PRESETS["trend_clean"]

    def warm(tk):
        # ONLINE run with the default config; trades discarded — we only want the cache fills.
        bt = tb.ThetaBacktest(ThetaClient())
        trades = bt.run_ticker(tk, start, end, weights=weights,
                               adx_gate=30.0, risk=200.0,
                               gex_size=args.gex_size, gex_walls=args.gex_walls,
                               gex_max_dte=args.gex_max_dte)
        return tk, len(trades)

    print(f"  warming cache: {len(tickers)} tickers  {args.start} -> {args.end}")
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(warm, tk): tk for tk in tickers}
        for fut in as_completed(futs):
            tk = futs[fut]
            try:
                _, n = fut.result()
            except Exception as e:
                print(f"    [{tk}] ERROR: {e}")
            done += 1
            if done % 20 == 0 or done == len(tickers):
                print(f"    [{done}/{len(tickers)}] warmed")
    print(f"  DONE. Now run backtests over this window with --offline (no Terminal needed).")


if __name__ == "__main__":
    main()
