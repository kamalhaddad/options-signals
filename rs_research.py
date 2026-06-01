"""
Tier-1 signal research: cross-sectional RELATIVE STRENGTH.

Hypothesis: a trend-following call-scalper should do better on the day's LEADERS (strongest
names) than on the whole universe. We rank every watchlist ticker each bar by intraday
return-since-open, mark the top quantile as "leaders", and only allow entries on leader bars.

Anti-overfit: evaluated on 24h / 72h / MONTH at once — trust a change only if it lifts the
MONTH. Runs OFFLINE (cached bars), so it doesn't touch the live droplet's ThetaData session.

  .venv/bin/python rs_research.py
"""
from __future__ import annotations
import math
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

import strategy_core as sc
import theta_backtest as tb
from thetadata_client import ThetaClient

WINDOWS = [("24h", 20260529, 20260529), ("72h", 20260527, 20260529), ("month", 20260428, 20260529)]
QUANTILES = [None, 0.50, 0.33, 0.20]      # None = baseline (no RS gate); else top-X% leaders
CUTOFF_MIN = 12 * 60                       # morning-session config (current default)


def leader_sets(client: ThetaClient, tickers: list, start: int, end: int, quantile: float) -> dict:
    """{ticker: set('YYYYMMDDHHMM' where it's a top-`quantile` leader by return-since-open)}."""
    rets: dict = {}                        # ts_str -> {ticker: return_since_open}
    for tk in tickers:
        b = client.stock_ohlc(tk, start, end)
        if b.empty:
            continue
        day = b.index.strftime("%Y%m%d")
        day_open = b["Open"].groupby(day).transform("first")
        ret = (b["Close"] - day_open) / day_open
        for ts, r in ret.items():
            if r == r:                     # NaN-safe
                rets.setdefault(ts.strftime("%Y%m%d%H%M"), {})[tk] = float(r)
    leaders: dict = {tk: set() for tk in tickers}
    for ts_str, d in rets.items():
        if len(d) < 5:
            continue
        ranked = sorted(d.items(), key=lambda x: -x[1])
        k = max(1, int(len(ranked) * quantile))
        for tk, _ in ranked[:k]:
            leaders[tk].add(ts_str)
    return leaders


def summarize(trades: list) -> dict:
    n = len(trades)
    if not n:
        return dict(n=0, win=0.0, exp=0.0, tot=0.0)
    wins = [t["pnl_pct"] for t in trades if t["pnl_pct"] > 0]
    tot = sum(t["pnl_pct"] for t in trades)
    return dict(n=n, win=len(wins) / n * 100, exp=tot / n, tot=tot)


def run_window(start: int, end: int, tickers: list, weights, required, quantile, workers=12) -> dict:
    client = ThetaClient(offline=True)
    # pull the SAME warmup-extended range run_ticker uses, so the offline cache hits.
    warm = int((pd.Timestamp(str(start)) - pd.Timedelta(days=tb.WARMUP_CALENDAR_DAYS)).strftime("%Y%m%d"))
    rs = leader_sets(client, tickers, warm, end, quantile) if quantile is not None else None

    def one(tk):
        return tb.ThetaBacktest(ThetaClient(offline=True)).run_ticker(
            tk, start, end, weights=weights, required=required, adx_gate=30.0,
            risk=200.0, slippage=1.5, max_spread=6.0, min_premium=tb.MIN_PREMIUM,
            min_oi=tb.MIN_OPEN_INTEREST, entry_cutoff_min=CUTOFF_MIN,
            rs_bars=(rs.get(tk) if rs is not None else None))

    trades = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(one, tk) for tk in tickers]
        for f in as_completed(futs):
            try:
                trades.extend(f.result())
            except Exception:
                pass
    return summarize(trades)


def main():
    import config
    tickers = list(config.WATCHLIST)
    sc.SKIP_OPEN_MINUTES = 0
    weights = tb.SIGNAL_PRESETS["trend_clean"]
    n_active = sum(1 for w in weights.values() if w > 0)
    required = math.ceil(sc.MIN_BULLISH_INDICATORS / 6 * n_active)

    hdr = f"  {'RS gate':<16}" + "".join(f"| {w[0]:^26} " for w in WINDOWS)
    print(hdr); print("  " + "-" * (len(hdr) - 2))
    print(f"  {'':<16}" + "".join(f"| {'tr  win%  avg/t  total%':^26} " for _ in WINDOWS))
    print("  " + "-" * (len(hdr) - 2))
    for q in QUANTILES:
        label = "baseline" if q is None else f"top {int(q*100)}%"
        cells = []
        for _, s, e in WINDOWS:
            r = run_window(s, e, tickers, weights, required, q)
            cells.append(f"{r['n']:>3} {r['win']:>4.0f}% {r['exp']:>+5.1f}% {r['tot']:>+6.0f}%")
        print(f"  {label:<16}" + "".join(f"|  {c:^25}" for c in cells))
    print("\n  Trust a lift in the MONTH column. RS helps only if leaders beat the full universe there.")


if __name__ == "__main__":
    main()
