"""
Live signal engine — the TUNED backtest strategy, evaluated on the latest bar.

This is the bridge between `theta_backtest.py` (which the strategy was tuned on) and the
live Discord bot. It reuses the parity-verified pieces so live signals match the backtest:
  - `strategy_core.compute_signals_series` with the `trend_clean` weights (incl. ADX + VWAP)
  - `ThetaBacktest.pick_contract` / option quotes / OI / IV for contract selection
  - the same entry gates (ADX>30, symmetric PUT) and premium-based exits (+40% / -50%)

It does NOT re-run history each tick beyond the warmup window — it fetches today's 5-min
bars (+ a few warmup days) and reads the latest CLOSED bar. Session rules (trade the open,
no new entries after 12:00 ET, flatten by EOD) live in `session.py`.
"""
from __future__ import annotations
import math
from datetime import datetime
from zoneinfo import ZoneInfo

import os

import pandas as pd

import strategy_core as sc
import theta_backtest as tb
from thetadata_client import ThetaClient, strike_to_dollars

ET = ZoneInfo("America/New_York")
WARMUP_CALENDAR_DAYS = tb.WARMUP_CALENDAR_DAYS

# Theta-aware loss-cut (validated +2.4% cur-DTE / +5% short-DTE vs the flat stop): the premium
# stop ratchets TIGHTER as the trade ages (scaled by DTE), cutting decaying losers faster while
# leaving the +TP untouched. Env-tunable; THETA_EXIT=0 reverts to the flat STOP_LOSS_PREMIUM_PCT.
THETA_EXIT_ON = os.getenv("THETA_EXIT", "1") == "1"
THETA_EXIT_INIT = float(os.getenv("THETA_EXIT_INIT", "35"))    # starting stop %% (early in the trade)
THETA_EXIT_FLOOR = float(os.getenv("THETA_EXIT_FLOOR", "20"))  # tightest stop %% (after the trade ages)
MIN_BARS = 40
# Hist OHLC lags ~15min intraday even on PRO; splice the real-time snapshot as the current bar.
USE_SNAPSHOT = os.getenv("LIVE_SNAPSHOT", "1") != "0"


def today_et() -> int:
    return int(datetime.now(ET).strftime("%Y%m%d"))


class LiveEngine:
    """Stateless evaluator: given a ticker, returns the latest-bar signal + contract pick.
    Position state and exit bookkeeping live in the bot; this just mirrors the backtest math."""

    def __init__(self, client: ThetaClient | None = None, signals: str = "trend_clean",
                 adx_gate: float = 30.0, moneyness: float = tb.OTM_TARGET_PCT,
                 max_spread: float = tb.MAX_SPREAD_PCT, min_premium: float = tb.MIN_PREMIUM,
                 min_oi: int = tb.MIN_OPEN_INTEREST, vol_mode: str = "current"):
        self.bt = tb.ThetaBacktest(client or ThetaClient())
        self.weights = tb.SIGNAL_PRESETS[signals]
        self.vol_mode = vol_mode
        n_active = sum(1 for w in self.weights.values() if w > 0)
        self.required = math.ceil(sc.MIN_BULLISH_INDICATORS / 6 * n_active)
        self.adx_gate = adx_gate
        self.moneyness = moneyness
        self.max_spread = max_spread
        self.min_premium = min_premium
        self.min_oi = min_oi
        self._iv_rank_memo: dict = {}   # (ticker, day) -> IV rank (computed once/day per ticker)

    # ── signal ────────────────────────────────────────────────────────────────
    def latest(self, ticker: str, date_i: int | None = None) -> dict | None:
        """Latest-bar signal state for `ticker` (today's bars + warmup). None if insufficient
        data. `date_i` overrides "today" for testing against a past session."""
        day = date_i or today_et()
        warm = int((pd.Timestamp(str(day)) - pd.Timedelta(days=WARMUP_CALENDAR_DAYS)).strftime("%Y%m%d"))
        bars = self.bt.c.stock_ohlc(ticker, warm, day)
        if bars.empty or len(bars) < MIN_BARS:
            return None
        # Splice the real-time snapshot as the current bar (live only; backtests pass date_i).
        # Captures the move since the last (lagged) hist bar with a sane single-bar range.
        if USE_SNAPSHOT and date_i is None:
            snap = self.bt.c.stock_snapshot(ticker)
            if snap and float(snap.get("close") or 0) > 0 and int(snap.get("date") or 0) == day:
                cur = float(snap["close"])
                last_close = float(bars["Close"].iloc[-1])
                cur_t = pd.Timestamp(datetime.now(ET).replace(tzinfo=None)).floor("5min")
                if cur_t <= bars.index[-1]:
                    cur_t = bars.index[-1] + pd.Timedelta(minutes=5)
                bars = pd.concat([bars, pd.DataFrame([{
                    "Open": last_close, "High": max(last_close, cur), "Low": min(last_close, cur),
                    "Close": cur, "Volume": float(bars["Volume"].iloc[-1]),
                }], index=[cur_t])])
        scores, bull, bear, adx, vwap = sc.compute_signals_series(bars, self.weights, self.vol_mode)
        i = len(bars) - 1
        sc_i = scores[i]
        if sc_i is None or sc_i != sc_i:        # NaN-safe
            return None
        spot = float(bars["Close"].iloc[i])
        # broad-market regime hint (matches theta_backtest.compute_market_regime): +1 bullish
        # (close>vwap AND ema_fast>ema_slow), -1 bearish (both down), 0 neutral/chop.
        ema_f, ema_s = sc.compute_ema_cross(bars["Close"])
        ef, es = float(ema_f.iloc[i]), float(ema_s.iloc[i])
        vw_i = float(vwap[i]) if vwap[i] == vwap[i] else None
        regime = 0
        if vw_i is not None:
            if spot > vw_i and ef > es:
                regime = 1
            elif spot < vw_i and ef < es:
                regime = -1
        # intraday return-since-open (for cross-sectional relative-strength ranking)
        last = bars.index[i]
        same_day = bars.index.strftime("%Y%m%d") == last.strftime("%Y%m%d")
        day_open = float(bars["Open"][same_day].iloc[0])
        ret_open = (spot - day_open) / day_open if day_open else 0.0
        return {
            "ticker": ticker, "time": last, "spot": spot,
            "score": float(sc_i), "bullish": int(bull[i]), "bearish": int(bear[i]),
            "adx": float(adx[i]) if adx[i] == adx[i] else 0.0,
            "vwap": (float(vwap[i]) if vwap[i] == vwap[i] else None),
            "ret_open": ret_open, "regime": regime,
        }

    def entry_direction(self, st: dict) -> str | None:
        """CALL / PUT / None for the latest bar, applying the tuned entry gates.
        (Session window / 12:00 cutoff are enforced by the caller, not here.)"""
        if self.adx_gate > 0 and st["adx"] < self.adx_gate:
            return None
        if st["score"] >= sc.BUY_THRESHOLD and st["bullish"] >= self.required:
            return "CALL"
        if st["score"] <= -sc.BUY_THRESHOLD and st["bearish"] >= self.required:
            return "PUT"
        return None

    def exit_reason(self, pos: dict, st: dict, is_eod: bool, now=None) -> str | None:
        """Mirror theta_backtest exits for an open position given the latest bid + signal.
        `pos` carries entry premium and direction; `st` is latest() for the same ticker.
        `now` (ET datetime) enables the theta-aware loss-cut — the stop ratchets tighter as the
        trade ages (scaled by DTE); falls back to the flat stop if timing data is unavailable."""
        bid = self.current_bid(pos["ticker"], pos["exp"], pos["strike"], pos["right"])
        if bid is not None and pos["entry"] > 0:
            pnl = (bid - pos["entry"]) / pos["entry"] * 100
            if pnl >= sc.TAKE_PROFIT_PREMIUM_PCT:
                return f"take profit (+{pnl:.0f}% prem)"
            # theta-aware loss-cut: eff_stop tightens from -INIT toward -FLOOR over dte*12 bars
            # (short-DTE tightens fast). Fail-safe: any bad/missing timing -> flat stop.
            eff_stop = sc.STOP_LOSS_PREMIUM_PCT
            if THETA_EXIT_ON and now is not None and pos.get("entry_time") is not None and pos.get("exp_date"):
                try:
                    held = max(0.0, (now - pos["entry_time"]).total_seconds() / 300.0)
                    dte = max(1, (pd.Timestamp(pos["exp_date"]).date() - now.date()).days)
                    progress = min(1.0, held / (dte * 12.0))
                    eff_stop = THETA_EXIT_INIT - (THETA_EXIT_INIT - THETA_EXIT_FLOOR) * progress
                except Exception:
                    eff_stop = sc.STOP_LOSS_PREMIUM_PCT
            if pnl <= -eff_stop:
                tag = "theta stop" if eff_stop != sc.STOP_LOSS_PREMIUM_PCT else "stop loss"
                return f"{tag} ({pnl:.0f}% prem)"
        if st is not None and st["score"] is not None:
            if pos["dir"] == "CALL" and st["score"] <= sc.SELL_THRESHOLD:
                return "opposite signal"
            if pos["dir"] == "PUT" and st["score"] >= sc.BUY_THRESHOLD and st["bullish"] >= self.required:
                return "opposite signal"
        if is_eod:
            return "end of day"
        return None

    # ── contracts ───────────────────────────────────────────────────────────────
    def pick_and_quote(self, ticker: str, direction: str, spot: float, t, date_i: int | None = None) -> dict | None:
        """Pick the contract (nearest 3-14 DTE, ~2% OTM) and its current ask/bid, applying the
        liquidity gates (premium ≥ $1, spread < 6%, OI ≥ 250). None if nothing qualifies."""
        day = date_i or today_et()
        pick = self.bt.pick_contract(ticker, t, spot, direction, self.moneyness)
        if not pick:
            return None
        exp, strike, right, otm = pick
        q = self.bt.c.option_quote(ticker, exp, strike, right, day, day)
        if q.empty:
            return None
        last = q.iloc[-1]
        ask, bid = float(last["ask"]), float(last["bid"])
        if ask < self.min_premium:
            return None
        mid = (ask + bid) / 2
        spread = (ask - bid) / mid * 100 if mid > 0 else 999.0
        if spread > self.max_spread:
            return None
        if self.min_oi > 0 and self.bt._oi(ticker, exp, strike, right, day) < self.min_oi:
            return None
        iv = self.bt._iv(ticker, exp, strike, right, day)
        return {
            "exp": exp, "strike": strike, "right": right, "otm": otm,
            "strike_d": strike_to_dollars(strike), "ask": ask, "bid": bid, "spread": spread,
            "iv": iv, "exp_date": tb.exp_to_date(exp).strftime("%Y-%m-%d"),
            "dte": (tb.exp_to_date(exp) - pd.Timestamp(str(day))).days,
        }

    def current_bid(self, ticker: str, exp: int, strike: int, right: str, date_i: int | None = None):
        day = date_i or today_et()
        q = self.bt.c.option_quote(ticker, exp, strike, right, day, day)
        if q.empty:
            return None
        b = float(q.iloc[-1]["bid"])
        return b if b > 0 else None

    # ── conviction (IV rank) ─────────────────────────────────────────────────────
    def iv_rank(self, ticker: str, day: int | None = None, lookback: int = 20) -> float | None:
        """IV rank (0..1) of today's ATM IV within the trailing `lookback` trading days; low =
        cheap vol = higher-conviction call/put (see vol_research / SIGNALS.md). Memoized per
        (ticker, day) — computed at most once per ticker per day. None on insufficient data;
        the caller treats None as 'no conviction tag', never an error."""
        day = day or today_et()
        key = (ticker, day)
        if key in self._iv_rank_memo:
            return self._iv_rank_memo[key]
        try:
            rank = self._compute_iv_rank(ticker, day, lookback)
        except Exception:
            rank = None
        self._iv_rank_memo[key] = rank
        return rank

    def _compute_iv_rank(self, ticker: str, day: int, lookback: int) -> float | None:
        d_ts = pd.Timestamp(str(day))
        start = int((d_ts - pd.Timedelta(days=lookback * 2 + 10)).strftime("%Y%m%d"))
        bars = self.bt.c.stock_ohlc(ticker, start, day)
        if bars.empty:
            return None
        day_spot: dict = {}
        for ts, row in bars.iterrows():
            day_spot.setdefault(int(ts.strftime("%Y%m%d")), float(row["Open"]))
        all_exps = self.bt.expirations(ticker)
        series: dict = {}
        for dd, spot in day_spot.items():
            dts = pd.Timestamp(str(dd))
            exps = [e for e in all_exps if 3 <= (tb.exp_to_date(e) - dts).days <= 35]
            if not exps:
                continue
            exp = min(exps, key=lambda e: (tb.exp_to_date(e) - dts).days)
            strikes = [(s, strike_to_dollars(s)) for s in self.bt.strikes(ticker, exp)]
            if not strikes:
                continue
            sk, _ = min(strikes, key=lambda x: abs(x[1] - spot))
            iv = self.bt._iv(ticker, exp, sk, "C", dd)
            if iv and iv > 0:
                series[dd] = iv
        days = sorted(k for k in series if k <= day)
        if day not in series or len(days) < 10:       # need enough trailing history to rank
            return None
        window = days[-lookback:]
        vals = [series[k] for k in window]
        return sum(1 for v in vals if v <= series[day]) / len(vals)


if __name__ == "__main__":
    # Smoke test against a recent CLOSED session (market closed today). Needs the Terminal.
    import sys
    day = int(sys.argv[1]) if len(sys.argv) > 1 else 20260529
    eng = LiveEngine()
    for tk in ["PLTR", "ORCL", "HOOD", "NVDA"]:
        st = eng.latest(tk, date_i=day)
        if not st:
            print(f"{tk:5} no data"); continue
        d = eng.entry_direction(st)
        line = f"{tk:5} score={st['score']:+.2f} adx={st['adx']:.0f} bull={st['bullish']} bear={st['bearish']} -> {d or '—'}"
        if d:
            c = eng.pick_and_quote(tk, d, st["spot"], st["time"], date_i=day)
            line += f"  | {d} ${c['strike_d']:.0f} {c['exp_date']} ask=${c['ask']:.2f} spr={c['spread']:.1f}% dte={c['dte']}" if c else "  | (no contract)"
        print(line)
