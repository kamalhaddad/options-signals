# Signal Research — fund-caliber add-ons (Tier-1)

> For future sessions. Status of the "what signals are we missing?" investigation.
> **Discipline: backtest-first.** Every signal is evaluated on the 215-trade MONTH (the
> anti-overfit bar) before it touches the live bot. Adding a signal ≠ edge — GEX *failed*.
> Validation sample so far is ONE favorable, all-CALL month (2026-04-28..05-29) — treat
> "passes" as directional, not proven; re-validate across regimes (esp. Feb) before trusting.

## Status

| Signal | Verdict | Live? | Where |
|---|---|---|---|
| **Relative strength (cross-sectional)** | ✅ passes (free lunch) | **LIVE** (`RS_QUANTILE=0.5`) | `bot.py`, `theta_backtest.py --rs`, `rs_research.py` |
| **IV rank (vol surface)** | ✅ passes (quality↑, volume↓) | banked (not live) | `vol_research.py` |
| **Dealer gamma (GEX)** | ❌ rejected (negative selectivity) | off (opt-in flags) | `gex.py`, `gex_sweep.py`, STRATEGY.md |
| skew / term-structure / VRP | untested | — | (next) |

## 1. Relative strength — LIVE ✅

Rank the watchlist each bar by **intraday return-since-open**; only enter the top quantile
(leaders). A trend-following call-scalper does better on the day's strongest names; it quietly
culls laggard entries. Clean of look-ahead (bar-t closes only).

Month (online): baseline 132 tr / 56% / +11.9%/tr / +1565%  →  **RS top-50%: 117 / 58% / +13.8% / +1618%**.
A *free lunch*: win rate, expectancy, AND total all up (cuts only ~15 weak trades).
Top-33% over-filters (total < baseline). **Live at top-50%** — the bot computes RS each scan
in `scan()` (pass-1 collects `latest()`; leaders = top `RS_QUANTILE`).

## 2. IV rank (vol surface) — researched, BANKED ⏸️

"Don't overpay for rich vol." Per ticker, percentile of today's ATM IV vs its ~20-day range
(low = cheap). Gate entries to low IV-rank. **Positive selectivity** (the opposite of GEX) —
the trades it removes are systematically worse.

Month (post-filter on the trades):
| cap | trades | win% | avg/tr | total% |
|---|---|---|---|---|
| baseline | 131 | 56% | +11.6% | +1526% |
| IV≤70% | 88 | 60% | +14.9% | +1308% |
| IV≤50% | 65 | 62% | +15.0% | +977% |

**Stacks with RS** (largely independent signals):
| config | trades | win% | avg/tr | total% |
|---|---|---|---|---|
| RS only | 117 | 58% | +13.8% | +1618% |
| RS + IV≤70% | 77 | 65% | +18.0% | +1390% |
| RS + IV≤50% | 54 | 69% | +19.6% | +1058% |

**Why banked, not live:** it's **quality-for-volume**, not a free lunch — `total%` slides
because it cuts a lot of trades (incl. some rich-vol *winners*, e.g. MSFT +39.5% @ IV 85%).
Great for a *signal feed* (69% hit rate, ~54 high-conviction trades/mo); worse for total
compounding. Also **heavier to run live**: needs each ticker's ATM-IV history pulled+ranked —
do it **once per day** (cache the daily IV rank), not per scan.

Reproduce: dump trades (`theta_backtest … --rs 0.5 --dump x.json`), then
`vol_research.py x.json [cap]` (post-filters by IV rank; `cap` prints trade-by-trade detail).

## 3. GEX — rejected ❌

Dealer gamma (timing gate / sizing / walls): every form lost to baseline; the flip gate has
*negative* selectivity (looser threshold → higher expectancy). Kept off, opt-in only. See
STRATEGY.md "What DIDN'T".

## Next (untested vol-surface)
- **Skew** (OTM put vs call IV) as direction/regime; **term structure** (front vs back month);
  **VRP** (IV vs realized vol). All need chain/greeks pulls.
- Also pending: earnings/event filter (risk — avoid IV-crush) and portfolio-level risk
  (correlation/sector caps). See the "fund-caliber" chat thread.
