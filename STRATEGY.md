# Options Scalping Strategy — Findings & Config

> For future Claude sessions. This documents the **real-options backtest** (`theta_backtest.py`)
> and the strategy we tuned on it. Read this before changing the strategy or "improving" results.

## TL;DR

A trend-following intraday options scalper, backtested on **real ThetaData option prices**
(real bid/ask fills, real greeks). After a long, disciplined tuning + stress-test process, the
honest conclusion is:

> **A modest, regime- and liquidity-dependent edge: ~+3%/trade on premium averaged across
> diverse 2026 windows, positive in ~3 of 5 two-week windows, with realistic (1.5%) slippage.**
> NOT a money-printer. It works in trending, liquid conditions and loses in choppy ones (e.g. Feb 2026).

The winning config is now the **default** (a no-flag run reproduces it).

## The winning configuration (current defaults)

| Component | Setting | Why |
|---|---|---|
| Signals | `trend_clean`: MACD .30 / VWAP .25 / EMA .15 / RSI .15 / Volume .15 | Trend-following; **no StochRSI/Bollinger** |
| Direction | CALL on bullish, PUT on bearish (symmetric: `score≤−0.46` + 4-of-5 bearish) | |
| Exits | **−50% stop / +40% TP** on premium, fixed from entry; + opposite-signal + EOD | Wide stop survives spread noise; let winners run |
| Regime gate | **ADX > 30** | Skip chop — the single biggest expectancy lever |
| Liquidity | **spread < 6%**, **OI ≥ 250**, premium ≥ $1 | Execution is THE lever — tight fills keep the edge |
| Fills | enter @ ask, exit @ bid, **+1.5% slippage**, $0.65/contract | Realistic; do not run at 0% slippage |
| Sizing | `--risk 200` (fixed-$ risk/trade) optional | For total-$; %-expectancy is unchanged by sizing |

Run it (ThetaData Terminal must be running — see `lean/`… no, see below):
```bash
# Terminal first (host, keep running):  java -jar ~/ThetaTerminal.jar <email> <pwd>
.venv/bin/python theta_backtest.py --tickers all --start 2026-05-27 --end 2026-05-29
# add --risk 200 for $-sizing; override any default knob (--adx-gate, --max-spread, --signals, ...)
```

## What we learned (don't re-discover these the hard way)

**What WORKED:**
- **Cutting the mean-reversion oscillators (StochRSI + Bollinger).** The original 6-signal mix
  was 40% RSI-family oscillators that bet *against* the move → all-PUTs on up days, big losses.
- **VWAP** as a weighted trend/bias signal.
- **ADX > 30 regime gate** — tripled expectancy by skipping non-trending conditions.
- **Wide stop (−50%) + +40% TP** — the edge is the asymmetry (avg win ~+25–33% vs avg loss ~−12%),
  NOT win rate (~40–52%). Let winners run.
- **Tight liquidity (spread<6% + OI≥250)** — the biggest lever for surviving realistic slippage.
  Lifted cross-regime avg from +0.8% → +3.0%/trade.
- **Symmetric PUT entry** (require `score≤−0.46` + conviction), not the loose SELL_THRESHOLD.

**What DIDN'T (tested and rejected — don't re-add):**
- **ITM / higher-delta contracts** — *hurt* expectancy (smaller % moves), despite higher win rate.
- **Trailing stops** — cut winners short; the strategy needs the full +40% runs.
- **Raising the entry threshold above 0.46** — *lowered* expectancy (fires later in the move).
- **Market-regime (SPY) gate** — slightly hurt on average; didn't rescue weak months.
- **IV-max / cheap-options proxy, VWAP hard-gate** — neutral.

## Brutally honest performance (real ThetaData, 1.5% slippage)

Cross-regime, winning config, 2-week windows in 2026 (spread<6%+OI≥250):
| Window | Exp/trade |
|---|---|
| Jan | +5.2% |
| Feb | **−2.6%** |
| Mar | +3.6% |
| Apr | +9.2% |
| May (full 2wk) | −0.2% |
| **Average** | **~+3.0%** |

The headline 24h/72h numbers (+6.5% / +12.3% per trade on 2026-05-27..29) are **small-sample,
favorable-regime** views — do NOT plan around them. Use **~+3%/trade** as the realistic expectation.
**Feb is the known failure regime** (sharp/choppy) — understand it before trusting live.

⚠️ At **0% slippage the strategy looks like +6–9%/trade** — that's an illusion. Always assume ≥1.5%
slippage; the edge halves at 1% and goes negative at 5%.

## Architecture / how it works

- `theta_backtest.py` — event-driven backtest on ThetaData. Parallel per ticker; **disk-cached**
  (`.cache/thetadata/`) so re-runs/sweeps are near-instant. All knobs are CLI flags (see `--help`,
  though `--help` is broken by a click bug — read the `argparse` block).
- `thetadata_client.py` — REST client for the local ThetaData Terminal (`127.0.0.1:25510`).
- `strategy_core.py` — the signal engine (parity-verified against the original via `test_parity.py`).
- Data: real underlying 5-min bars + real option chain + real per-bar bid/ask + real greeks.
- **Run host-only** (never Docker) to avoid ThetaData's 476 WRONG_IP single-client-IP lock.
- See [[thetadata-backtest]] memory for the operational gotchas.

## Methodology guardrails (so we don't fool ourselves)

- **Always report % expectancy per trade** (`Avg/trade`), not just win rate. Win rate ~45% is fine
  when W:L is ~2×.
- **Validate out-of-sample** on untouched windows before believing any config.
- **Assume ≥1.5% slippage** in every backtest.
- **Beware small samples** (a 7-trade 24h window is meaningless) and **post-hoc selection** (picking
  the best of N configs on the same days = overfitting).
- More knobs ≠ better. We stopped here deliberately to avoid curve-fitting.

## Honest next steps (not yet done)

1. **Investigate the Feb failure regime** — why does it lose? (Likely a sharp-selloff/high-vol whipsaw.)
2. **Forward paper-trade** before any real capital — backtest ≠ live fills.
3. Possible signal adds (untested): real IV-percentile, relative-strength vs index, dealer gamma (GEX).
   But the priority is robustness, not more signals.
