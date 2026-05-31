"""
GEX-gate tuning sweep.

The GEX gate is per-DAY (it turns a whole day's entries on/off) and positions are
intraday, so a gated run == the unfiltered run with that day's trades removed.
That means we only need ONE full backtest (all days tradable) + ONE GEX-per-day
computation; every threshold is then a cheap offline post-filter. This is exactly
equivalent to re-running theta_backtest with each --gex-gate, at 1/N the ThetaData cost.

  --verify  : before trusting any GEX number, pull one SPY expiration's OI + quote
              bulk live and print the raw format + a parsed sample (the gex.py header
              warns the bulk parsing was never confirmed against the live Terminal).

  (no flag) : run the full unfiltered backtest, then print a comparison table of
              expectancy at each GEX threshold vs the no-gate baseline.

Usage (Terminal must be running):
  .venv/bin/python gex_sweep.py --verify --start 2026-05-27 --end 2026-05-29
  .venv/bin/python gex_sweep.py --tickers all --start 2026-05-27 --end 2026-05-29
"""
from __future__ import annotations
import argparse
import math
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

import strategy_core as sc
import gex as gexmod
from thetadata_client import ThetaClient, strike_to_dollars
import theta_backtest as tb


def verify(client: ThetaClient, root: str, date_i: int) -> None:
    """Live sanity check of the bulk OI + quote parsing for the nearest expiration."""
    exps = client.expirations(root)
    date_ts = gexmod._exp_to_ts(date_i)
    fut = [e for e in exps if (gexmod._exp_to_ts(e) - date_ts).days >= 0]
    if not fut:
        print(f"  VERIFY: no expirations >= {date_i} for {root}")
        return
    exp = min(fut, key=lambda e: (gexmod._exp_to_ts(e) - date_ts).days)
    print(f"  VERIFY {root} exp={exp} on {date_i}")

    oi_fmt, oi_rows = client.bulk_hist("open_interest", root, exp, date_i)
    print(f"  open_interest fmt = {oi_fmt}")
    print(f"  open_interest rows = {len(oi_rows)}")
    if oi_rows:
        print(f"  sample contract dict = {oi_rows[0].get('contract')}")
        print(f"  sample ticks[-1]     = {oi_rows[0].get('ticks', [['?']])[-1]}")
    oi = gexmod._parse_bulk(oi_fmt, oi_rows, "open_interest")
    nz = {k: v for k, v in oi.items() if v > 0}
    print(f"  parsed OI entries = {len(oi)} ({len(nz)} with OI>0)")
    for k in list(sorted(nz, key=lambda k: -nz[k]))[:5]:
        print(f"    strike={k[0]} (${strike_to_dollars(k[0]):.0f}) {k[1]}  OI={nz[k]:.0f}")

    q_fmt, q_rows = client.bulk_hist("quote", root, exp, date_i, ivl_ms=3600000)
    print(f"  quote fmt = {q_fmt}")
    mids = gexmod._parse_bulk_mid(q_fmt, q_rows)
    print(f"  parsed quote mids = {len(mids)}")
    pos_mid = {k: v for k, v in mids.items() if v > 0}
    for k in list(pos_mid)[:5]:
        print(f"    strike={k[0]} (${strike_to_dollars(k[0]):.0f}) {k[1]}  mid=${pos_mid[k]:.2f}")
    print("  --> Eyeball: OI counts plausible? mids look like real option prices?"
          " strikes bracket spot? If yes, GEX is trustworthy.")


def summarize(trades: list[dict]) -> dict:
    n = len(trades)
    if n == 0:
        return dict(n=0, win=0.0, exp=0.0, avg_win=0.0, avg_loss=0.0, wl=0.0, tot=0.0, usd=0.0)
    wins = [t["pnl_pct"] for t in trades if t["pnl_pct"] > 0]
    losses = [t["pnl_pct"] for t in trades if t["pnl_pct"] <= 0]
    tot = sum(t["pnl_pct"] for t in trades)
    usd = sum(t["pnl_usd"] for t in trades)
    aw = sum(wins) / len(wins) if wins else 0.0
    al = sum(losses) / len(losses) if losses else 0.0
    return dict(n=n, win=len(wins) / n * 100, exp=tot / n, avg_win=aw, avg_loss=al,
                wl=(aw / -al if al else 0.0), tot=tot, usd=usd)


def main():
    ap = argparse.ArgumentParser(description="GEX-gate tuning sweep")
    ap.add_argument("--tickers", default="all", help="comma-separated, or 'all' for the watchlist")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--verify", action="store_true", help="live-verify bulk parsing, then exit")
    # match the baseline backtest.log config so the no-gate row reproduces it
    ap.add_argument("--signals", default="trend_clean", choices=list(tb.SIGNAL_PRESETS))
    ap.add_argument("--adx-gate", type=float, default=30.0)
    ap.add_argument("--buy-threshold", type=float, default=sc.BUY_THRESHOLD)
    ap.add_argument("--stop", type=float, default=sc.STOP_LOSS_PREMIUM_PCT)
    ap.add_argument("--tp", type=float, default=sc.TAKE_PROFIT_PREMIUM_PCT)
    ap.add_argument("--max-spread", type=float, default=6.0)
    ap.add_argument("--min-oi", type=int, default=250)
    ap.add_argument("--slippage", type=float, default=1.5)
    ap.add_argument("--risk", type=float, default=200.0)
    ap.add_argument("--moneyness", type=float, default=tb.OTM_TARGET_PCT)
    ap.add_argument("--max-dte", type=int, default=14, help="GEX expiry window (days)")
    ap.add_argument("--thresholds", default="", help="comma-separated GEX cut points in $B "
                    "(e.g. '0,-1,-2'); default = derived from the data quartiles")
    args = ap.parse_args()

    start, end = tb.to_int_date(args.start), tb.to_int_date(args.end)
    client = ThetaClient()

    if args.verify:
        verify(client, tb.MARKET_INDEX, start)
        return

    sc.BUY_THRESHOLD = args.buy_threshold
    sc.STOP_LOSS_PREMIUM_PCT = args.stop
    sc.TAKE_PROFIT_PREMIUM_PCT = args.tp

    if args.tickers.lower() in ("all", "watchlist"):
        import config
        tickers = list(config.WATCHLIST)
    else:
        tickers = [t.strip().upper() for t in args.tickers.split(",")]

    weights = tb.SIGNAL_PRESETS[args.signals]
    n_active = sum(1 for w in weights.values() if w > 0)
    required = math.ceil(sc.MIN_BULLISH_INDICATORS / 6 * n_active)

    # 1) GEX per day (exact values, computed once).
    spy_bars = client.stock_ohlc(tb.MARKET_INDEX, start, end)
    day_to_spot: dict[int, float] = {}
    for ts, row in spy_bars.iterrows():
        day_to_spot.setdefault(int(ts.strftime("%Y%m%d")), float(row["Open"]))
    print(f"  computing SPY GEX for {len(day_to_spot)} day(s) (max_dte={args.max_dte}) ...")
    gex_by_day = gexmod.spy_gex_by_day(client, day_to_spot, max_dte=args.max_dte)
    for d in sorted(gex_by_day):
        print(f"    {d}: GEX = {gex_by_day[d]/1e9:+.2f}B  (spot {day_to_spot[d]:.2f})")

    # 2) Full unfiltered backtest (every day tradable).
    print(f"  running unfiltered backtest: {len(tickers)} tickers x {args.workers} workers ...")

    def run_one(tk):
        return ThetaBacktest_run(client_factory(), tk, start, end, weights, required, args)

    all_trades: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(run_one, tk): tk for tk in tickers}
        for fut in as_completed(futs):
            tk = futs[fut]
            try:
                all_trades.extend(fut.result())
            except Exception as e:
                print(f"    [{tk}] ERROR: {e}")
    print(f"  total unfiltered trades: {len(all_trades)}")

    def entry_day(tr) -> int:
        return int(tr["entry_time"].strftime("%Y%m%d"))

    # 3) Threshold grid.
    if args.thresholds.strip():
        cuts = [float(x) * 1e9 for x in args.thresholds.split(",")]
    else:
        vals = sorted(gex_by_day.values())
        # quartile-ish cut points across the observed GEX range
        cuts = sorted(set(round(v / 1e9, 2) * 1e9 for v in vals)) if vals else [0.0]
    cuts = sorted(set(cuts))

    print(f"\n  {'='*78}")
    print(f"  GEX-GATE SWEEP  ({args.start} -> {args.end})  baseline = no gate")
    print(f"  {'='*78}")
    print(f"  {'threshold':>12}  {'days':>5}  {'trades':>6}  {'win%':>5}  "
          f"{'avg/trade':>9}  {'total%':>7}  {'W:L':>5}")
    base = summarize(all_trades)
    print(f"  {'NO GATE':>12}  {len(gex_by_day):>5}  {base['n']:>6}  {base['win']:>4.0f}%  "
          f"{base['exp']:>+8.1f}%  {base['tot']:>+6.0f}%  {base['wl']:>4.2f}x")
    for cut in cuts:
        ok_days = {d for d, v in gex_by_day.items() if v <= cut}
        sub = [t for t in all_trades if entry_day(t) in ok_days]
        s = summarize(sub)
        print(f"  {'<= %+.1fB' % (cut/1e9):>12}  {len(ok_days):>5}  {s['n']:>6}  {s['win']:>4.0f}%  "
              f"{s['exp']:>+8.1f}%  {s['tot']:>+6.0f}%  {s['wl']:>4.2f}x")
    print(f"  {'='*78}")
    print("  Pick the threshold that lifts avg/trade meaningfully WITHOUT gutting trade count.")


# helpers to keep ThreadPool closures simple ---------------------------------
def client_factory() -> ThetaClient:
    return ThetaClient()


def ThetaBacktest_run(client, tk, start, end, weights, required, args) -> list[dict]:
    return tb.ThetaBacktest(client).run_ticker(
        tk, start, end, weights=weights, required=required, adx_gate=args.adx_gate,
        moneyness=args.moneyness, vwap_gate=False, trail=0.0, iv_max=tb.MAX_IV,
        risk=args.risk, slippage=args.slippage, market_regime=None,
        max_spread=args.max_spread, min_premium=tb.MIN_PREMIUM, min_oi=args.min_oi)


if __name__ == "__main__":
    main()
