"""
Event-driven options backtest on REAL ThetaData option prices.

Replaces the yfinance delta-approximation (`show_trades.py`) with a backtest that
uses real underlying 5-min bars, the real option chain, and real per-bar bid/ask
to model fills and the premium-based exits. Signal logic is the parity-verified
`strategy_core` (identical to the existing backtest engine).

Runs entirely on the host against the local ThetaData Terminal — no LEAN, no
Docker, no QuantConnect.

Strategy (matches the agreed design):
  - 5-min bars; skip first SKIP_OPEN_MINUTES / last SKIP_CLOSE_MINUTES.
  - BUY signal -> long CALL; bearish (score<=SELL_THRESHOLD) -> long PUT.
  - Contract: nearest expiry 3-14 DTE, ~2% OTM (1-3% band), IV<150%.
  - Fills: ENTER at ask, EXIT at bid (real spread cost) + $0.65/contract/side.
  - Exits: -25% / +40% on premium (vs entry ask), opposite signal, or EOD flatten.

Usage:
  .venv/bin/python theta_backtest.py --tickers NVDA --start 2024-03-04 --end 2024-03-08
"""

from __future__ import annotations
import argparse
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd

import strategy_core as sc
from thetadata_client import ThetaClient, strike_to_dollars

# Contract-selection / cost constants (mirror options.pick_option intent).
EXPIRY_MIN_DAYS = 3
EXPIRY_MAX_DAYS = 14
OTM_TARGET_PCT = 2.0
OTM_MIN_PCT = 1.0
OTM_MAX_PCT = 3.0
MAX_IV = 1.50
MAX_SPREAD_PCT = 6.0       # tight spread = better fills; the key execution lever (STRATEGY.md)
MIN_PREMIUM = 1.00         # skip dirt-cheap options (worst % spreads / slippage)
MIN_OPEN_INTEREST = 250    # liquidity floor
COMMISSION_PER_CONTRACT = 0.65
WARMUP_BARS = 40            # 5-min bars before signals are trusted
WARMUP_CALENDAR_DAYS = 4    # ~2 trading days back (>=150 bars) — plenty past ADX/vol-avg settle

# Signal presets for ablation/tuning. Weights are renormalized in strategy_core,
# so only relative magnitudes matter. Keys: rsi, macd, ema_cross, bollinger,
# stoch_rsi, volume, vwap.
SIGNAL_PRESETS = {
    "base":        {"rsi": .15, "macd": .25, "ema_cross": .10, "bollinger": .10, "stoch_rsi": .25, "volume": .15},
    "no_stoch":    {"rsi": .15, "macd": .25, "ema_cross": .10, "bollinger": .10, "volume": .15},
    "no_bb":       {"rsi": .15, "macd": .25, "ema_cross": .10, "stoch_rsi": .25, "volume": .15},
    "no_stoch_bb": {"rsi": .15, "macd": .25, "ema_cross": .10, "volume": .15},
    "plus_vwap":   {"rsi": .15, "macd": .25, "ema_cross": .10, "bollinger": .10, "stoch_rsi": .25, "volume": .15, "vwap": .20},
    "momentum":    {"macd": .35, "ema_cross": .20, "vwap": .30, "volume": .15},
    "trend_clean": {"macd": .30, "ema_cross": .15, "vwap": .25, "rsi": .15, "volume": .15},
}


def to_int_date(s: str) -> int:
    return int(s.replace("-", ""))


def exp_to_date(exp: int) -> pd.Timestamp:
    d = str(int(exp))
    return pd.Timestamp(year=int(d[:4]), month=int(d[4:6]), day=int(d[6:8]))


MARKET_INDEX = "SPY"


def compute_market_regime(index_bars: pd.DataFrame) -> dict:
    """Per-bar broad-market regime from the index (SPY): +1 bullish, -1 bearish,
    0 neutral/chop. Bullish = index above its intraday VWAP AND short-EMA above
    long-EMA (both agree); bearish = both down; otherwise neutral (stand down).

    Returns {timestamp.value: regime}. The neutral state is the key bit — it makes
    us sit out choppy days (the Feb/Mar regime that dragged the average)."""
    if index_bars.empty:
        return {}
    close = index_bars["Close"]
    ema_f, ema_s = sc.compute_ema_cross(close)
    vwap = sc.compute_vwap(index_bars)
    reg = {}
    for i, t in enumerate(index_bars.index):
        c = float(close.iloc[i]); v = float(vwap.iloc[i])
        ef = float(ema_f.iloc[i]); es = float(ema_s.iloc[i])
        if v == v and c > v and ef > es:
            reg[t.value] = 1
        elif v == v and c < v and ef < es:
            reg[t.value] = -1
        else:
            reg[t.value] = 0
    return reg


class ThetaBacktest:
    def __init__(self, client: ThetaClient):
        self.c = client
        self._exp_cache: dict[str, list[int]] = {}
        self._strike_cache: dict[tuple, list[int]] = {}
        self._optq_cache: dict[tuple, pd.DataFrame] = {}
        self._iv_cache: dict[tuple, float] = {}
        self._oi_cache: dict[tuple, int] = {}

    # ── chain helpers (cached) ────────────────────────────────────────────────
    def expirations(self, root: str) -> list[int]:
        if root not in self._exp_cache:
            self._exp_cache[root] = sorted(self.c.expirations(root))
        return self._exp_cache[root]

    def strikes(self, root: str, exp: int) -> list[int]:
        key = (root, exp)
        if key not in self._strike_cache:
            self._strike_cache[key] = sorted(self.c.strikes(root, exp))
        return self._strike_cache[key]

    def option_quotes(self, root: str, exp: int, strike: int, right: str, date_i: int) -> pd.DataFrame:
        key = (root, exp, strike, right, date_i)
        if key not in self._optq_cache:
            self._optq_cache[key] = self.c.option_quote(root, exp, strike, right, date_i, date_i)
        return self._optq_cache[key]

    def pick_contract(self, root: str, bar_time: pd.Timestamp, spot: float, direction: str,
                      moneyness: float = OTM_TARGET_PCT):
        """Return (exp, strike_int, right, otm_pct) for the nearest strike to the
        target moneyness at the nearest valid expiry, or None.

        `moneyness` is % away from spot: positive = OTM, negative = ITM. For a CALL
        the target strike is spot*(1+m/100); for a PUT spot*(1-m/100). ITM (m<0)
        gives higher-delta contracts (tighter % spread, less theta)."""
        right = "C" if direction == "CALL" else "P"
        bar_date = bar_time.normalize()
        candidates = [e for e in self.expirations(root)
                      if EXPIRY_MIN_DAYS <= (exp_to_date(e) - bar_date).days <= EXPIRY_MAX_DAYS]
        if not candidates:
            return None
        exp = min(candidates, key=lambda e: (exp_to_date(e) - bar_date).days)

        strikes_d = [(s, strike_to_dollars(s)) for s in self.strikes(root, exp)]
        if not strikes_d:
            return None
        if direction == "CALL":
            target = spot * (1 + moneyness / 100)
        else:
            target = spot * (1 - moneyness / 100)
        strike_int, strike_d = min(strikes_d, key=lambda sd: abs(sd[1] - target))
        otm_pct = (strike_d - spot) / spot * 100 if direction == "CALL" else (spot - strike_d) / spot * 100
        return exp, strike_int, right, otm_pct

    # ── per-ticker backtest ───────────────────────────────────────────────────
    def run_ticker(self, root: str, start: int, end: int,
                   weights=None, required=None, adx_gate=0.0,
                   moneyness=OTM_TARGET_PCT, vwap_gate=False, trail=0.0, iv_max=MAX_IV,
                   risk=0.0, slippage=0.0, market_regime=None,
                   max_spread=MAX_SPREAD_PCT, min_premium=MIN_PREMIUM, min_oi=0) -> list[dict]:
        warm_start = int((pd.Timestamp(str(start)) - pd.Timedelta(days=WARMUP_CALENDAR_DAYS)).strftime("%Y%m%d"))
        bars = self.c.stock_ohlc(root, warm_start, end)
        if bars.empty:
            return []

        trades: list[dict] = []
        in_trade = False
        pos = {}

        # Compute every bar's signal once (vectorized) instead of per-bar rebuilds.
        scores, bull_counts, bear_counts, adx_vals, vwap_vals = sc.compute_signals_series(bars, weights)
        if required is None:
            required = sc.MIN_BULLISH_INDICATORS
        closes = bars["Close"].to_numpy(dtype=float)
        idx = list(bars.index)
        for i, t in enumerate(idx):
            spot = float(closes[i])
            is_last_of_day = (i == len(idx) - 1) or (idx[i + 1].normalize() != t.normalize())
            in_request_window = int(t.strftime("%Y%m%d")) >= start

            score = scores[i]
            bullish = bull_counts[i]
            bearish = bear_counts[i]

            # ── manage open position ──────────────────────────────────────────
            if in_trade:
                bid = self._quote_at(pos, t, "bid")
                exit_reason = None
                if bid is not None and pos["entry"] > 0:
                    pos["peak"] = max(pos.get("peak", pos["entry"]), bid)
                    pnl = (bid - pos["entry"]) / pos["entry"] * 100
                    if pnl >= sc.TAKE_PROFIT_PREMIUM_PCT:
                        exit_reason = f"take profit (+{pnl:.0f}% prem)"
                    elif trail > 0:
                        drop = (pos["peak"] - bid) / pos["peak"] * 100 if pos["peak"] else 0.0
                        if drop >= trail:
                            exit_reason = f"trailing stop ({pnl:.0f}% prem)"
                    elif pnl <= -sc.STOP_LOSS_PREMIUM_PCT:
                        exit_reason = f"stop loss ({pnl:.0f}% prem)"
                if exit_reason is None and score is not None:
                    if pos["dir"] == "CALL" and score <= sc.SELL_THRESHOLD:
                        exit_reason = "opposite signal"
                    elif pos["dir"] == "PUT" and score >= sc.BUY_THRESHOLD and bullish >= required:
                        exit_reason = "opposite signal"
                if exit_reason is None and is_last_of_day:
                    exit_reason = "end of day"
                if exit_reason is not None:
                    raw = bid if bid is not None else pos["entry"]
                    exit_fill = raw * (1 - slippage / 100)   # fill worse than bid
                    trades.append(self._close(pos, t, exit_fill, exit_reason, spot, i - pos["entry_i"]))
                    in_trade = False
                    pos = {}
                    continue

            # ── entries ───────────────────────────────────────────────────────
            if not in_trade and in_request_window and score is not None and self._within_window(t) and not is_last_of_day:
                adx_ok = adx_gate <= 0 or (adx_vals[i] == adx_vals[i] and adx_vals[i] >= adx_gate)
                vw = vwap_vals[i]
                above_vwap = (vw == vw) and spot > vw   # NaN-safe
                below_vwap = (vw == vw) and spot < vw
                # broad-market regime gate (None = off): only trade with the market
                reg = market_regime.get(t.value, 0) if market_regime is not None else None
                mkt_call = reg is None or reg > 0
                mkt_put = reg is None or reg < 0
                direction = None
                if adx_ok and mkt_call and score >= sc.BUY_THRESHOLD and bullish >= required and (not vwap_gate or above_vwap):
                    direction = "CALL"
                elif adx_ok and mkt_put and score <= -sc.BUY_THRESHOLD and bearish >= required and (not vwap_gate or below_vwap):
                    direction = "PUT"
                if direction:
                    opened = self._try_open(root, t, spot, direction, score, i, moneyness, iv_max, risk, slippage,
                                            max_spread, min_premium, min_oi)
                    if opened:
                        pos = opened
                        in_trade = True
        return trades

    def _within_window(self, t: pd.Timestamp) -> bool:
        minutes_since_open = (t.hour * 60 + t.minute) - (9 * 60 + 30)
        minutes_to_close = (16 * 60) - (t.hour * 60 + t.minute)
        if 0 <= minutes_since_open < sc.SKIP_OPEN_MINUTES:
            return False
        if 0 < minutes_to_close <= sc.SKIP_CLOSE_MINUTES:
            return False
        return True

    def _quote_at(self, pos: dict, t: pd.Timestamp, field: str):
        df = pos["quotes"]
        if df is None or df.empty:
            return None
        sub = df[df.index <= t]
        if sub.empty:
            return None
        val = float(sub.iloc[-1][field])
        return val if val > 0 else None

    def _try_open(self, root, t, spot, direction, score, i, moneyness=OTM_TARGET_PCT, iv_max=MAX_IV, risk=0.0,
                  slippage=0.0, max_spread=MAX_SPREAD_PCT, min_premium=MIN_PREMIUM, min_oi=0):
        pick = self.pick_contract(root, t, spot, direction, moneyness)
        if pick is None:
            return None
        exp, strike, right, otm_pct = pick
        date_i = int(t.strftime("%Y%m%d"))
        quotes = self.option_quotes(root, exp, strike, right, date_i)
        if quotes.empty:
            return None
        # entry at the ask available at/just before this bar
        sub = quotes[quotes.index <= t]
        if sub.empty:
            return None
        entry_ask = float(sub.iloc[-1]["ask"])
        entry_bid = float(sub.iloc[-1]["bid"])
        if entry_ask <= 0:
            return None
        # Liquidity gate (restored from options.pick_option): skip cheap options and
        # wide spreads — the structural killers identified in the loss autopsy.
        if entry_ask < min_premium:
            return None
        mid = (entry_ask + entry_bid) / 2
        spread_pct = (entry_ask - entry_bid) / mid * 100 if mid > 0 else 999
        if spread_pct > max_spread:
            return None
        # Open-interest floor (one OI call cached per contract-day).
        if min_oi > 0 and self._oi(root, exp, strike, right, date_i) < min_oi:
            return None
        # IV filter (one greeks call cached per contract-day). iv_max tunable to
        # gate out expensive options (cheap-options / IV proxy for Phase 5).
        iv = self._iv(root, exp, strike, right, date_i)
        if iv is not None and iv > iv_max:
            return None
        # Fill worse than the ask by `slippage` % (execution realism).
        entry_fill = entry_ask * (1 + slippage / 100)
        # Position sizing: risk a fixed $ per trade (equalizes risk across cheap vs
        # expensive options). Risk/contract ≈ stop% × premium × 100. risk=0 -> 1 contract.
        if risk > 0:
            risk_per_ct = (sc.STOP_LOSS_PREMIUM_PCT / 100) * entry_fill * 100
            qty = max(1, round(risk / risk_per_ct)) if risk_per_ct > 0 else 1
        else:
            qty = 1
        return {
            "root": root, "dir": direction, "exp": exp, "strike": strike, "right": right,
            "otm_pct": otm_pct, "entry": entry_fill, "entry_bid": entry_bid, "qty": qty,
            "entry_time": t, "entry_spot": spot, "entry_i": i,
            "score": round(score, 3), "quotes": quotes,
        }

    def _oi(self, root, exp, strike, right, date_i):
        key = (root, exp, strike, right, date_i)
        if key not in self._oi_cache:
            self._oi_cache[key] = self.c.option_oi(root, exp, strike, right, date_i)
        return self._oi_cache[key]

    def _iv(self, root, exp, strike, right, date_i):
        key = (root, exp, strike, right, date_i)
        if key not in self._iv_cache:
            g = self.c.option_greeks(root, exp, strike, right, date_i, date_i)
            ivs = g["implied_vol"][g["implied_vol"] > 0] if not g.empty else []
            self._iv_cache[key] = float(ivs.iloc[0]) if len(ivs) else None
        return self._iv_cache[key]

    def _close(self, pos, t, exit_bid, reason, exit_spot, bars_held):
        qty = pos.get("qty", 1)
        gross_pct = (exit_bid - pos["entry"]) / pos["entry"] * 100 if pos["entry"] else 0.0
        pnl_usd = (exit_bid - pos["entry"]) * 100 * qty - 2 * COMMISSION_PER_CONTRACT * qty
        # entry spread cost: how far below the ask we paid the bid already was
        entry_spread_pct = ((pos["entry"] - pos["entry_bid"]) / pos["entry"] * 100) if pos["entry"] else 0.0
        # underlying move over the hold (signed in the trade's favor: + = good)
        raw_move = (exit_spot - pos["entry_spot"]) / pos["entry_spot"] * 100 if pos["entry_spot"] else 0.0
        under_move_pct = raw_move if pos["dir"] == "CALL" else -raw_move
        return {
            "ticker": pos["root"], "type": pos["dir"], "strike": strike_to_dollars(pos["strike"]),
            "exp": pos["exp"], "otm_pct": pos["otm_pct"],
            "entry_time": pos["entry_time"], "entry": pos["entry"], "entry_bid": pos["entry_bid"],
            "exit_time": t, "exit": exit_bid, "pnl_pct": gross_pct, "pnl_usd": pnl_usd,
            "reason": reason, "score": pos["score"], "qty": qty,
            "entry_spread_pct": entry_spread_pct, "bars_held": bars_held,
            "under_move_pct": under_move_pct,
        }


def main():
    ap = argparse.ArgumentParser(description="Real-options backtest on ThetaData")
    ap.add_argument("--tickers", default="NVDA", help="comma-separated, or 'all' for the watchlist")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--workers", type=int, default=8, help="concurrent tickers (default 8)")
    ap.add_argument("--dump", default=None, help="write structured trades to this JSON path")
    ap.add_argument("--stop", type=float, default=sc.STOP_LOSS_PREMIUM_PCT, help="stop-loss %% on premium")
    ap.add_argument("--tp", type=float, default=sc.TAKE_PROFIT_PREMIUM_PCT, help="take-profit %% on premium")
    ap.add_argument("--signals", default="trend_clean", choices=list(SIGNAL_PRESETS), help="signal preset")
    ap.add_argument("--adx-gate", type=float, default=30.0, help="skip entries when ADX below this (0=off)")
    ap.add_argument("--moneyness", type=float, default=OTM_TARGET_PCT, help="strike %% from spot: +OTM / -ITM")
    ap.add_argument("--vwap-gate", action="store_true", help="only CALL above VWAP / PUT below VWAP")
    ap.add_argument("--trail", type=float, default=0.0, help="trailing stop %% off premium peak (0=fixed stop)")
    ap.add_argument("--iv-max", type=float, default=MAX_IV, help="skip options with IV above this (e.g. 0.6)")
    ap.add_argument("--buy-threshold", type=float, default=sc.BUY_THRESHOLD, help="entry score bar (|score|), higher=pickier")
    ap.add_argument("--min-conv", type=int, default=0, help="min signals that must agree (0=auto 4-of-6 proportion)")
    ap.add_argument("--risk", type=float, default=0.0, help="$ risked per trade for sizing (0=flat 1 contract)")
    ap.add_argument("--slippage", type=float, default=1.5, help="fill %% worse than bid/ask (default 1.5 = realistic)")
    ap.add_argument("--market-gate", action="store_true", help="only trade with the broad-market (SPY) regime")
    ap.add_argument("--max-spread", type=float, default=MAX_SPREAD_PCT, help="max bid/ask spread %% (tighter=better fills)")
    ap.add_argument("--min-premium", type=float, default=MIN_PREMIUM, help="min option premium $")
    ap.add_argument("--min-oi", type=int, default=MIN_OPEN_INTEREST, help="min open interest (0=off)")
    args = ap.parse_args()
    sc.STOP_LOSS_PREMIUM_PCT = args.stop
    sc.TAKE_PROFIT_PREMIUM_PCT = args.tp
    sc.BUY_THRESHOLD = args.buy_threshold

    weights = SIGNAL_PRESETS[args.signals]
    n_active = sum(1 for w in weights.values() if w > 0)
    required = args.min_conv or math.ceil(sc.MIN_BULLISH_INDICATORS / 6 * n_active)

    if args.tickers.lower() in ("all", "watchlist"):
        import config
        tickers = list(config.WATCHLIST)
    else:
        tickers = [t.strip().upper() for t in args.tickers.split(",")]
    start, end = to_int_date(args.start), to_int_date(args.end)

    market_regime = None
    if args.market_gate:
        ws = int((pd.Timestamp(str(start)) - pd.Timedelta(days=WARMUP_CALENDAR_DAYS)).strftime("%Y%m%d"))
        spy = ThetaClient().stock_ohlc(MARKET_INDEX, ws, end)
        market_regime = compute_market_regime(spy)
        b = sum(1 for v in market_regime.values() if v > 0)
        s_ = sum(1 for v in market_regime.values() if v < 0)
        nu = sum(1 for v in market_regime.values() if v == 0)
        print(f"  market-gate ON ({MARKET_INDEX}): {b} bull / {s_} bear / {nu} neutral bars")

    all_trades: list[dict] = []
    print(f"{'='*118}")
    print(f"  REAL-OPTIONS BACKTEST (ThetaData)  {args.start} -> {args.end}  ({len(tickers)} tickers, {args.workers} workers)")
    print(f"  Buy>={sc.BUY_THRESHOLD} Sell<={sc.SELL_THRESHOLD} | SL -{sc.STOP_LOSS_PREMIUM_PCT}% / "
          f"TP +{sc.TAKE_PROFIT_PREMIUM_PCT}% on premium | enter@ask exit@bid +${COMMISSION_PER_CONTRACT}/ct")
    print(f"{'='*118}")

    def run_one(tk):
        # Each ticker gets its own client/caches (thread-safe isolation).
        return tk, ThetaBacktest(ThetaClient()).run_ticker(
            tk, start, end, weights=weights, required=required, adx_gate=args.adx_gate,
            moneyness=args.moneyness, vwap_gate=args.vwap_gate, trail=args.trail, iv_max=args.iv_max,
            risk=args.risk, slippage=args.slippage, market_regime=market_regime,
            max_spread=args.max_spread, min_premium=args.min_premium, min_oi=args.min_oi)

    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(run_one, tk): tk for tk in tickers}
        for fut in as_completed(futures):
            tk = futures[fut]
            try:
                _, trades = fut.result()
            except Exception as e:
                trades = []
                print(f"  [{tk}] ERROR: {e}")
            done += 1
            all_trades.extend(trades)
            if trades:
                print(f"  [{done}/{len(tickers)}] {tk}: {len(trades)} trades")

    all_trades.sort(key=lambda x: x["entry_time"])

    if args.dump:
        import json
        serializable = []
        for tr in all_trades:
            d = {k: v for k, v in tr.items()}
            d["entry_time"] = tr["entry_time"].strftime("%Y-%m-%d %H:%M")
            d["exit_time"] = tr["exit_time"].strftime("%Y-%m-%d %H:%M")
            d["exp"] = exp_to_date(tr["exp"]).strftime("%Y-%m-%d")
            serializable.append(d)
        with open(args.dump, "w") as f:
            json.dump(serializable, f, indent=2)
        print(f"  (dumped {len(serializable)} trades -> {args.dump})")
    print(f"\n  {'#':<3} {'Tkr':<5} {'Type':<4} {'Strike':<8} {'Exp':<10} {'Entry@ask':<18} "
          f"{'Exit@bid':<18} {'P&L%':<8} {'$P&L':<8} {'Reason'}")
    print(f"  {'-'*3} {'-'*5} {'-'*4} {'-'*8} {'-'*10} {'-'*18} {'-'*18} {'-'*8} {'-'*8} {'-'*20}")
    win_pcts, loss_pcts = [], []
    tot_pct = 0.0
    tot_usd = 0.0
    for i, tr in enumerate(all_trades, 1):
        tot_pct += tr["pnl_pct"]; tot_usd += tr["pnl_usd"]
        (win_pcts if tr["pnl_pct"] > 0 else loss_pcts).append(tr["pnl_pct"])
        exp_s = exp_to_date(tr["exp"]).strftime("%Y-%m-%d")
        print(f"  {i:<3} {tr['ticker']:<5} {tr['type']:<4} ${tr['strike']:<7.0f} {exp_s:<10} "
              f"${tr['entry']:<6.2f} {tr['entry_time'].strftime('%m/%d %H:%M'):<11} "
              f"${tr['exit']:<6.2f} {tr['exit_time'].strftime('%m/%d %H:%M'):<11} "
              f"{tr['pnl_pct']:+.1f}%{'':<2} ${tr['pnl_usd']:+.0f}{'':<3} {tr['reason']}")
    n = len(all_trades)
    avg_win = sum(win_pcts) / len(win_pcts) if win_pcts else 0.0
    avg_loss = sum(loss_pcts) / len(loss_pcts) if loss_pcts else 0.0
    wl = (avg_win / -avg_loss) if avg_loss else 0.0
    expectancy = tot_pct / n if n else 0.0
    print(f"\n  {'='*64}")
    print(f"  SUMMARY  (per-trade % return on premium — the edge metric)")
    print(f"  {'='*64}")
    print(f"  Trades:      {n}   |   Win rate: {(len(win_pcts)/n*100 if n else 0):.0f}%")
    print(f"  Avg/trade:   {expectancy:+.1f}%   <-- expectancy per trade")
    print(f"  Avg win:     {avg_win:+.1f}%   |   Avg loss: {avg_loss:+.1f}%   |   W:L {wl:.2f}x")
    print(f"  Total:       {tot_pct:+.0f}% (sum of per-trade %, 1-ct equal-weight)   |   ${tot_usd:+.0f} net")
    print(f"  {'='*64}")


if __name__ == "__main__":
    main()
