"""
Tuning sweep evaluated on THREE windows at once: 24h, 72h, and the full month.

The point is anti-overfit: a knob only counts if it improves the 215-trade MONTH (robust),
not just the tiny recent windows. Each config is run across all tickers for all three
windows and summarized side by side.

  .venv/bin/python tune.py            # online (pulls any new contracts; cached = fast)
  .venv/bin/python tune.py --offline  # cache-only (safe for contract-preserving knobs)
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

import strategy_core as sc
import theta_backtest as tb
from thetadata_client import ThetaClient

WINDOWS = [("24h", 20260529, 20260529), ("72h", 20260527, 20260529), ("month", 20260428, 20260529)]

# Principled levers (all contract-preserving -> offline-safe). Each is baseline + one change,
# plus a combo. Keys map to run_ticker args / sc globals.
CONFIGS = [
    ("baseline (skip20,no cut)",       {}),
    ("cutoff12 (skip20)",              {"cutoff": 12 * 60}),
    ("skip0 (trade open, no cut)",     {"skipopen": 0}),
    ("skip0 + cutoff12  [AM only]",    {"skipopen": 0, "cutoff": 12 * 60}),
    ("skip0 + cutoff11:30",            {"skipopen": 0, "cutoff": 11 * 60 + 30}),
    ("skip0 + cutoff13",               {"skipopen": 0, "cutoff": 13 * 60}),
    ("skip10 + cutoff12",              {"skipopen": 10, "cutoff": 12 * 60}),
    ("skip0 + cutoff12 + adx40",       {"skipopen": 0, "cutoff": 12 * 60, "adx": 40.0}),
]


def summarize(trades):
    n = len(trades)
    if n == 0:
        return dict(n=0, win=0.0, exp=0.0, tot=0.0, usd=0.0)
    wins = [t["pnl_pct"] for t in trades if t["pnl_pct"] > 0]
    tot = sum(t["pnl_pct"] for t in trades)
    return dict(n=n, win=len(wins) / n * 100, exp=tot / n, tot=tot,
                usd=sum(t["pnl_usd"] for t in trades))


def run_window(tickers, start, end, cfg, weights, offline, workers):
    # apply config to globals (per-call; single-threaded across configs)
    sc.BUY_THRESHOLD = cfg.get("buy", 0.46)
    sc.TAKE_PROFIT_PREMIUM_PCT = cfg.get("tp", 40.0)
    sc.SKIP_OPEN_MINUTES = cfg.get("skipopen", 20)
    n_active = sum(1 for w in weights.values() if w > 0)
    required = cfg.get("minconv") or __import__("math").ceil(sc.MIN_BULLISH_INDICATORS / 6 * n_active)
    kw = dict(weights=weights, required=required, adx_gate=cfg.get("adx", 30.0),
              vwap_gate=cfg.get("vwap", False), trail=cfg.get("trail", 0.0),
              risk=200.0, slippage=1.5, max_spread=6.0, min_premium=tb.MIN_PREMIUM,
              min_oi=tb.MIN_OPEN_INTEREST, entry_cutoff_min=cfg.get("cutoff"))

    def one(tk):
        return tb.ThetaBacktest(ThetaClient(offline=offline)).run_ticker(tk, start, end, **kw)

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
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--workers", type=int, default=12)
    args = ap.parse_args()
    import config
    tickers = list(config.WATCHLIST)
    weights = tb.SIGNAL_PRESETS["trend_clean"]

    hdr = f"  {'config':<30}" + "".join(f"| {w[0]:^28} " for w in WINDOWS)
    print(hdr); print("  " + "-" * (len(hdr) - 2))
    print(f"  {'':<30}" + "".join(f"| {'tr  win%  avg/t  total%':^28} " for _ in WINDOWS))
    print("  " + "-" * (len(hdr) - 2))
    for label, cfg in CONFIGS:
        cells = []
        for _, s, e in WINDOWS:
            r = run_window(tickers, s, e, cfg, weights, args.offline, args.workers)
            cells.append(f"{r['n']:>3} {r['win']:>4.0f}% {r['exp']:>+5.1f}% {r['tot']:>+6.0f}%")
        print(f"  {label:<30}" + "".join(f"|  {c:^27}" for c in cells))
    print("\n  Trust a change only if it lifts the MONTH column; the 24h/72h are confirmation, not the target.")


if __name__ == "__main__":
    main()
