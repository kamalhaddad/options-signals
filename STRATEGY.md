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
| **Session window** | **trade the open, NO new entries after 12:00 ET** (`SKIP_OPEN_MINUTES=0`, `ENTRY_CUTOFF="12:00"`) | Morning-session-only: capture open-momentum trends, cut the afternoon bleed. See below — lifts win rate AND total. |
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
- **Cross-sectional relative strength (top-50% leaders).** First fund-caliber "Tier-1" signal
  to pass — a free lunch (win rate, expectancy, AND total up). **Now live in the bot.** IV-rank
  (vol surface) also passes but is a quality/volume tradeoff — researched & banked, not live.
  Full status + numbers in **[[SIGNALS.md]]**.
- **Morning-session-only (trade the open + no new entries after 12:00 ET).** The single
  biggest win since the original tuning. Validated on the **215-trade month** (not just the
  recent windows), it lifts *both* metrics at once because the two halves are synergistic:
  trading the open *adds* strong open-momentum trades (↑ total), the noon cutoff *removes*
  afternoon chop/reversals/theta-bleed (↑ win rate). Month: **42%→56% win, +2.6%→+11.9%/trade,
  +480%→+1565% total**. The cutoff sweep peaks at ~12:00 (11:00 too tight, 13:00 slightly worse).
  Adding ADX>40 on top pushes win rate to ~61% but cuts total (fewer trades) — left out by choice.
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
- **Dealer Gamma (GEX), all three forms — timing gate, conviction sizing, gamma-wall strike/TP.**
  Fully built (`gex.py`, opt-in via `--gex-*` flags, off by default) and tested on the 215-trade
  month. **Every form lost to baseline.** The per-bar SPY flip gate has *negative selectivity*:
  loosening the threshold raises expectancy monotonically (+1.3→+2.2→+3.8→+5.4% as you let more
  trades through), i.e. the trades it removes are *better* than the ones it keeps — the optimum is
  no gate. Gamma walls actively hurt: the wall take-profit caps winners (avg win +28%→+16%) and
  even wall-strike-only pulls strikes ATM-ward (smaller % moves). The 3-day "100% win" that first
  looked promising was small-sample luck. Code kept (off) so this isn't re-litigated. (Resolves
  the "dealer gamma (GEX)" next-step below — answer: doesn't help here.)
- **Raising entry conviction generally (BUY_THRESHOLD, min-conv, ADX>50)** — same negative-
  selectivity trap as GEX: removes good trades. The current bars are near-optimal.

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

The table above is the **pre-morning-session** config. Morning-session-only lifted the recent
month to **+11.9%/trade (56% win)** and the 24h/72h to +30%/trade — but those are a strongly
bullish, all-CALL **favorable-regime** stretch, and the cutoff has **not yet been re-validated on
Jan–May / Feb** (next-step #3). Until it is, keep **~+3%/trade** as the conservative cross-regime
expectation and treat the month/24h/72h figures as best-case. **Feb is the known failure regime**
(sharp/choppy) — understand it before trusting live.

⚠️ At **0% slippage the strategy looks like +6–9%/trade** — that's an illusion. Always assume ≥1.5%
slippage; the edge halves at 1% and goes negative at 5%.

## Architecture / how it works

- `theta_backtest.py` — event-driven backtest on ThetaData. Parallel per ticker; **disk-cached**
  (`.cache/thetadata/`) so re-runs/sweeps are near-instant. All knobs are CLI flags.
- `thetadata_client.py` — REST client for the local ThetaData Terminal (`127.0.0.1:25510`).
- `strategy_core.py` — the signal engine (parity-verified against the original via `test_parity.py`).
- `tune.py` — **anti-overfit sweep harness**: runs each config across 24h / 72h / **month** at once.
  Rule: trust a change only if it lifts the *month* column; the short windows are confirmation.
- `gex.py` / `gex_sweep.py` — dealer-gamma module + sweep (opt-in, rejected — see above).
- `snapshot.py` — warm the cache for a window so later runs use `--offline`.

**Speed / offline (so tuning iterates in seconds, not minutes):**
- The bottleneck is ThetaData REST round-trips, NOT compute. `--offline` serves only from the disk
  cache (Terminal-free, ~10× faster; the month sweep drops from ~18 min to seconds). Workflow:
  run once online to warm the cache (or `snapshot.py`), then sweep with `--offline`.
- Per-contract option pulls beat bulk here — the backtest reads ~1 contract per expiration-day, so
  whole-chain bulk quotes are ~25× *slower* (measured). Bulk is only for the GEX profile (whole chain);
  its OI is reused by the liquidity gate when GEX is on (`ThetaClient.has_bulk`).
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
3. **Re-validate morning-session-only across the Jan–May windows** (esp. Feb) — it's validated on the
   recent month; confirm it holds out-of-sample before trusting it live.
4. ~~Dealer gamma (GEX)~~ — done, rejected (see "What DIDN'T"). Remaining untested signal ideas:
   real IV-percentile, relative-strength vs index. Priority is robustness, not more signals.
