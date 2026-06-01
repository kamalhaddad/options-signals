"""
Tier-1 signal #2 research: VOLATILITY SURFACE — IV rank (don't overpay for rich vol).

Hypothesis: a call-buyer suffers when implied vol is RICH (expensive premium, worse theta /
IV-crush). So gating entries to LOW IV-rank (cheap vol vs the name's own recent range) should
lift expectancy. (We earlier found an ABSOLUTE iv-max cap neutral; IV *rank* is relative.)

Method (cheap): IV rank is a per-(ticker, day) measure, so it's a clean POST-FILTER on the
baseline trades — no backtest re-run. Only new data = daily ATM IV per TRADED ticker over the
window + a lookback. Reads the baseline trade dump; prints expectancy at several IV-rank caps.

Run inside the droplet container (reaches the Terminal at 127.0.0.1):
  docker compose exec -T options-bot python vol_research.py base_trades.json
"""
from __future__ import annotations
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

import theta_backtest as tb
from thetadata_client import ThetaClient, strike_to_dollars

LOOKBACK_START = 20260331        # ~20 trading days before the 04-28 window start
END = 20260529
MIN_HISTORY = 10                 # need this many trailing IV obs to rank
CAPS = [1.01, 0.70, 0.50, 0.30]  # IV-rank caps to test (1.01 = baseline/all)


def atm_iv_series(ticker: str) -> dict:
    """{date_int: ATM IV} for `ticker` over [LOOKBACK_START, END]: nearest 3-35 DTE, ATM call."""
    c = ThetaClient()
    bt = tb.ThetaBacktest(c)
    bars = c.stock_ohlc(ticker, LOOKBACK_START, END)
    if bars.empty:
        return {}
    day_spot: dict = {}
    for ts, row in bars.iterrows():
        day_spot.setdefault(int(ts.strftime("%Y%m%d")), float(row["Open"]))
    out: dict = {}
    for d, spot in day_spot.items():
        dts = pd.Timestamp(str(d))
        exps = [e for e in bt.expirations(ticker) if 3 <= (tb.exp_to_date(e) - dts).days <= 35]
        if not exps:
            continue
        exp = min(exps, key=lambda e: (tb.exp_to_date(e) - dts).days)
        strikes = [(s, strike_to_dollars(s)) for s in bt.strikes(ticker, exp)]
        if not strikes:
            continue
        sk, _ = min(strikes, key=lambda x: abs(x[1] - spot))
        iv = bt._iv(ticker, exp, sk, "C", d)
        if iv and iv > 0:
            out[d] = iv
    return out


def iv_rank(series: dict, day: int, lookback: int = 20) -> float | None:
    """Percentile (0..1) of `day`'s IV within the trailing `lookback` trading days. Low = cheap."""
    days = sorted(k for k in series if k <= day)
    if day not in series or len(days) < MIN_HISTORY:
        return None
    window = days[-lookback:]
    vals = [series[k] for k in window]
    return sum(1 for v in vals if v <= series[day]) / len(vals)


def summarize(trades: list) -> dict:
    n = len(trades)
    if not n:
        return dict(n=0, win=0.0, exp=0.0, tot=0.0)
    wins = [t["pnl_pct"] for t in trades if t["pnl_pct"] > 0]
    tot = sum(t["pnl_pct"] for t in trades)
    return dict(n=n, win=len(wins) / n * 100, exp=tot / n, tot=tot)


def main():
    trades = json.load(open(sys.argv[1] if len(sys.argv) > 1 else "base_trades.json"))
    tickers = sorted({t["ticker"] for t in trades})
    print(f"  pulling ATM IV history for {len(tickers)} tickers ({LOOKBACK_START}..{END}) …")
    iv_by_tk: dict = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(atm_iv_series, tk): tk for tk in tickers}
        for f in as_completed(futs):
            tk = futs[f]
            try:
                iv_by_tk[tk] = f.result()
            except Exception as e:
                print(f"    [{tk}] IV pull failed: {e}"); iv_by_tk[tk] = {}

    # attach IV rank to each trade
    ranked = 0
    for t in trades:
        d = int(t["entry_time"][:10].replace("-", ""))
        t["iv_rank"] = iv_rank(iv_by_tk.get(t["ticker"], {}), d)
        ranked += t["iv_rank"] is not None
    print(f"  IV rank computed for {ranked}/{len(trades)} trades\n")

    print(f"  {'IV-rank cap':<14}{'trades':>8}{'win%':>8}{'avg/trade':>11}{'total%':>10}")
    print("  " + "-" * 51)
    for cap in CAPS:
        sub = [t for t in trades if t.get("iv_rank") is not None and t["iv_rank"] <= cap]
        s = summarize(sub)
        label = "baseline" if cap >= 1.0 else f"IV rank ≤ {int(cap*100)}%"
        print(f"  {label:<14}{s['n']:>8}{s['win']:>7.0f}%{s['exp']:>+10.1f}%{s['tot']:>+9.0f}%")
    print("\n  Cheap-vol gate helps only if a lower IV-rank cap lifts avg/trade without gutting count.")

    # trade-by-trade detail at a chosen cap (pass it as 2nd arg, e.g. 0.5)
    if len(sys.argv) > 2:
        cap = float(sys.argv[2])
        kept = [t for t in trades if t.get("iv_rank") is not None and t["iv_rank"] <= cap]
        s = summarize(kept)
        print(f"\n  ── trades (✓ kept at IV rank ≤ {int(cap*100)}%, ✗ filtered out) ──")
        print(f"  {'':2}{'Tkr':<6}{'Type':<5}{'Strike':>7} {'Entry':<16} {'Exit':<16} {'P&L%':>7} {'IVrk':>6}  Reason")
        for t in sorted(trades, key=lambda x: x["entry_time"]):
            r = t.get("iv_rank")
            mark = "✓" if (r is not None and r <= cap) else "✗"
            rs = f"{r*100:.0f}%" if r is not None else "n/a"
            print(f"  {mark} {t['ticker']:<5}{t['type']:<5}${t['strike']:>5.0f} {t['entry_time']:<16} "
                  f"{t['exit_time']:<16} {t['pnl_pct']:>+6.1f}% {rs:>5}  {t['reason']}")
        print(f"  → kept {s['n']} trades · win {s['win']:.0f}% · avg {s['exp']:+.1f}% · total {s['tot']:+.0f}%")


if __name__ == "__main__":
    main()
