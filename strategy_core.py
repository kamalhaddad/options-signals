"""
Portable signal core for the Options Scalper strategy.

This module is a self-contained copy of the technical-signal math from the
yfinance backtest (`signals.py` + `backtest.compute_signals_over_time`), with NO
dependency on yfinance, dotenv, or the project's `config` module — so it can be
imported unchanged inside a QuantConnect LEAN algorithm.

The indicator computations and scoring functions are copied VERBATIM from
`signals.py` to guarantee signal parity with the existing backtest. The only
addition is `compute_latest_signal()`, which replicates one iteration of
`backtest.compute_signals_over_time()` for the most recent bar of a rolling
window.

Parity is verified by `test_parity.py`, which diffs this module's per-bar output
against `backtest.compute_signals_over_time()` on real intraday data.
"""

import numpy as np
import pandas as pd

# ── Strategy parameters (inlined from config.py — keep in sync) ────────────────

# Signal thresholds (score ranges from -1.0 to +1.0)
BUY_THRESHOLD = 0.46
SELL_THRESHOLD = -0.25
MIN_BULLISH_INDICATORS = 4

# Premium-based exits, applied to the REAL option premium, fixed from entry.
# Tuned via sweep (see STRATEGY.md): a wide stop survives option-spread noise and
# lets winners run to the +40% target — the asymmetry IS the edge.
STOP_LOSS_PREMIUM_PCT = 50.0      # exit if premium falls 50% below entry
TAKE_PROFIT_PREMIUM_PCT = 40.0    # exit if premium rises 40% above entry

# Time filters (minutes of the regular session to skip). Trade the open (skip 0).
# Entry cutoff DISABLED — entries allowed all session (was 12:00 ET morning-only edge,
# validated on the 215-trade month + 24h/72h; removed by request to trade afternoons too).
SKIP_OPEN_MINUTES = 0
SKIP_CLOSE_MINUTES = 15
ENTRY_CUTOFF_MINUTE = None         # None = no cutoff; entries allowed until skip-close

# Technical indicator weights (must sum to 1.0) — 6 indicators
WEIGHTS = {
    "rsi": 0.15,
    "macd": 0.25,
    "ema_cross": 0.10,
    "bollinger": 0.10,
    "stoch_rsi": 0.25,
    "volume": 0.15,
}

# Options-specific signal weights (bonus/penalty added on top of the technical score)
OPTIONS_SIGNAL_WEIGHTS = {
    "iv_rank": 0.03,
    "put_call": 0.03,
    "unusual_vol": 0.02,
}

# Indicator parameters — short periods for fast intraday signals
RSI_PERIOD = 7
RSI_OVERSOLD = 35
RSI_OVERBOUGHT = 65

MACD_FAST = 5
MACD_SLOW = 13
MACD_SIGNAL = 4

EMA_FAST = 3
EMA_SLOW = 8

BOLLINGER_PERIOD = 10
BOLLINGER_STD = 1.5

STOCH_RSI_PERIOD = 7
STOCH_RSI_OVERSOLD = 25
STOCH_RSI_OVERBOUGHT = 75

VOLUME_SPIKE_MULTIPLIER = 1.2

VOLUME_AVG_WINDOW = 20  # rolling window for average volume (backtest.py:46)


# ── Core Indicators (verbatim from signals.py) ─────────────────────────────────

def compute_rsi(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_macd(close: pd.Series):
    ema_fast = close.ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow = close.ewm(span=MACD_SLOW, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=MACD_SIGNAL, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def compute_ema_cross(close: pd.Series):
    ema_fast = close.ewm(span=EMA_FAST, adjust=False).mean()
    ema_slow = close.ewm(span=EMA_SLOW, adjust=False).mean()
    return ema_fast, ema_slow


def compute_bollinger_bands(close: pd.Series):
    middle = close.rolling(window=BOLLINGER_PERIOD).mean()
    std = close.rolling(window=BOLLINGER_PERIOD).std()
    upper = middle + BOLLINGER_STD * std
    lower = middle - BOLLINGER_STD * std
    return upper, middle, lower


def compute_stoch_rsi(close: pd.Series, period: int = STOCH_RSI_PERIOD) -> pd.Series:
    rsi = compute_rsi(close, period)
    rsi_min = rsi.rolling(window=period).min()
    rsi_max = rsi.rolling(window=period).max()
    stoch_rsi = (rsi - rsi_min) / (rsi_max - rsi_min) * 100
    return stoch_rsi


ADX_PERIOD = 14


def compute_vwap(df: pd.DataFrame) -> pd.Series:
    """Intraday VWAP that RESETS each session (correct for 5-min intraday bars).

    The original signals.compute_vwap used a single cumulative sum, which is wrong
    across multiple days — here we group by calendar date so VWAP restarts at each
    open, as institutions compute it.
    """
    typical = (df["High"] + df["Low"] + df["Close"]) / 3
    tpv = typical * df["Volume"]
    dates = pd.Index(df.index).normalize()
    cum_tpv = tpv.groupby(dates).cumsum()
    cum_vol = df["Volume"].groupby(dates).cumsum()
    return cum_tpv / cum_vol.replace(0, pd.NA)


def compute_adx(df: pd.DataFrame, period: int = ADX_PERIOD) -> pd.Series:
    """Average Directional Index — trend strength (>20 trending, <15 chop)."""
    high, low, close = df["High"], df["Low"], df["Close"]
    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, min_periods=period).mean()
    plus_di = 100 * (plus_dm.ewm(alpha=1 / period, min_periods=period).mean() / atr)
    minus_di = 100 * (minus_dm.ewm(alpha=1 / period, min_periods=period).mean() / atr)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1)
    return dx.ewm(alpha=1 / period, min_periods=period).mean()


# ── Scoring Functions (verbatim from signals.py) ───────────────────────────────

def score_rsi(rsi_value: float) -> float:
    if rsi_value <= 20:
        return 1.0
    elif rsi_value <= RSI_OVERSOLD:
        return 0.8
    elif rsi_value <= 40:
        return 0.5
    elif rsi_value <= 45:
        return 0.2
    elif rsi_value >= 80:
        return -1.0
    elif rsi_value >= RSI_OVERBOUGHT:
        return -0.8
    elif rsi_value >= 60:
        return -0.5
    elif rsi_value >= 55:
        return -0.2
    return 0.0


def score_macd(macd_line: float, signal_line: float, prev_macd: float, prev_signal: float) -> float:
    bullish_cross = prev_macd <= prev_signal and macd_line > signal_line
    bearish_cross = prev_macd >= prev_signal and macd_line < signal_line

    if bullish_cross:
        return 1.0
    elif bearish_cross:
        return -1.0
    elif macd_line > signal_line:
        gap = macd_line - signal_line
        if signal_line != 0:
            ratio = abs(gap / signal_line)
        else:
            ratio = abs(gap) * 10
        if ratio > 0.5:
            return 0.8
        elif ratio > 0.2:
            return 0.6
        return 0.3
    elif macd_line < signal_line:
        gap = signal_line - macd_line
        if signal_line != 0:
            ratio = abs(gap / signal_line)
        else:
            ratio = abs(gap) * 10
        if ratio > 0.5:
            return -0.8
        elif ratio > 0.2:
            return -0.6
        return -0.3
    return 0.0


def score_ema_cross(ema_fast: float, ema_slow: float, prev_fast: float, prev_slow: float) -> float:
    bullish_cross = prev_fast <= prev_slow and ema_fast > ema_slow
    bearish_cross = prev_fast >= prev_slow and ema_fast < ema_slow

    if bullish_cross:
        return 1.0
    elif bearish_cross:
        return -1.0
    elif ema_fast > ema_slow:
        spread_pct = (ema_fast - ema_slow) / ema_slow * 100
        if spread_pct > 0.5:
            return 0.7
        elif spread_pct > 0.2:
            return 0.4
        return 0.15
    elif ema_fast < ema_slow:
        spread_pct = (ema_slow - ema_fast) / ema_slow * 100
        if spread_pct > 0.5:
            return -0.7
        elif spread_pct > 0.2:
            return -0.4
        return -0.15
    return 0.0


def score_bollinger(price: float, upper: float, lower: float, middle: float) -> float:
    band_width = upper - lower
    if band_width == 0:
        return 0.0

    position = (price - lower) / band_width

    if position <= 0.05:
        return 1.0
    elif position <= 0.15:
        return 0.7
    elif position <= 0.3:
        return 0.3
    elif position >= 0.95:
        return -1.0
    elif position >= 0.85:
        return -0.7
    elif position >= 0.7:
        return -0.3
    return 0.0


def score_stoch_rsi(stoch_value: float, prev_value: float) -> float:
    if stoch_value <= 10:
        if stoch_value > prev_value:
            return 1.0
        return 0.7
    elif stoch_value <= STOCH_RSI_OVERSOLD:
        if stoch_value > prev_value:
            return 0.8
        return 0.4
    elif stoch_value <= 35:
        if stoch_value > prev_value:
            return 0.3
        return 0.0
    elif stoch_value >= 90:
        if stoch_value < prev_value:
            return -1.0
        return -0.7
    elif stoch_value >= STOCH_RSI_OVERBOUGHT:
        if stoch_value < prev_value:
            return -0.8
        return -0.4
    elif stoch_value >= 65:
        if stoch_value < prev_value:
            return -0.3
        return 0.0
    return 0.0


def score_volume(current_vol: float, avg_vol: float) -> float:
    if avg_vol == 0:
        return 0.0
    ratio = current_vol / avg_vol
    if ratio >= 2.5:
        return 1.0
    elif ratio >= 2.0:
        return 0.8
    elif ratio >= VOLUME_SPIKE_MULTIPLIER:
        return 0.6
    elif ratio >= 1.2:
        return 0.3
    elif ratio >= 1.0:
        return 0.1
    return 0.0


def score_vwap(price: float, vwap: float) -> float:
    """Price vs intraday VWAP — the institutional trend/bias reference.
    Above VWAP = bullish, below = bearish (graduated by distance)."""
    if not vwap or vwap != vwap:  # 0 or NaN
        return 0.0
    pct = (price - vwap) / vwap * 100
    if pct > 0.5:
        return 1.0
    elif pct > 0.1:
        return 0.5
    elif pct > -0.1:
        return 0.0
    elif pct > -0.5:
        return -0.5
    return -1.0


def score_adx(adx_value: float) -> float:
    """Trend-strength score (informational/weighted use). Stronger trend = boost."""
    if adx_value != adx_value:  # NaN
        return 0.0
    if adx_value >= 30:
        return 1.0
    elif adx_value >= 20:
        return 0.5
    elif adx_value >= 15:
        return 0.0
    return -1.0


# ── Aggregation ────────────────────────────────────────────────────────────────

def compute_latest_signal(df: pd.DataFrame) -> dict:
    """Replicate one iteration of backtest.compute_signals_over_time() for the
    LAST bar of `df`.

    `df` must be an OHLCV frame (columns: Open/High/Low/Close/Volume) ordered
    oldest→newest, with enough history for indicator warmup (>= ~30 bars; the
    yfinance backtest uses a 5-day 5-min window). Returns a dict with the
    combined `score`, `bullish_count`, per-indicator `scores`, and the raw
    indicator values used.

    Returns score=None when there is insufficient data to compute a bar (mirrors
    the NaN first-row behavior of compute_signals_over_time).
    """
    if len(df) < 2:
        return {"score": None, "bullish_count": 0, "scores": {}}

    close = df["Close"]
    volume = df["Volume"]

    rsi = compute_rsi(close)
    macd_line, signal_line, _ = compute_macd(close)
    ema_fast, ema_slow = compute_ema_cross(close)
    bb_upper, bb_middle, bb_lower = compute_bollinger_bands(close)
    stoch_rsi = compute_stoch_rsi(close)
    avg_volume = volume.rolling(window=VOLUME_AVG_WINDOW).mean()

    i = len(df) - 1
    try:
        rsi_score = score_rsi(float(rsi.iloc[i]))
        macd_score = score_macd(
            float(macd_line.iloc[i]), float(signal_line.iloc[i]),
            float(macd_line.iloc[i - 1]), float(signal_line.iloc[i - 1]),
        )
        ema_score = score_ema_cross(
            float(ema_fast.iloc[i]), float(ema_slow.iloc[i]),
            float(ema_fast.iloc[i - 1]), float(ema_slow.iloc[i - 1]),
        )
        bb_u = float(bb_upper.iloc[i])
        bb_l = float(bb_lower.iloc[i])
        bb_m = float(bb_middle.iloc[i])
        price = float(close.iloc[i])
        boll_score = score_bollinger(price, bb_u, bb_l, bb_m)

        stoch_score = score_stoch_rsi(float(stoch_rsi.iloc[i]), float(stoch_rsi.iloc[i - 1]))

        vol_val = float(volume.iloc[i])
        avg_vol = float(avg_volume.iloc[i])
        vol_score = score_volume(vol_val, avg_vol) if avg_vol > 0 else 0.0
    except (ValueError, IndexError):
        return {"score": 0.0, "bullish_count": 0, "scores": {}}

    scores = {
        "rsi": rsi_score,
        "macd": macd_score,
        "ema_cross": ema_score,
        "bollinger": boll_score,
        "stoch_rsi": stoch_score,
        "volume": vol_score,
    }
    combined = sum(scores[k] * WEIGHTS[k] for k in WEIGHTS)
    bullish_count = sum(1 for s in scores.values() if s > 0)

    return {
        "score": combined,
        "bullish_count": bullish_count,
        "scores": scores,
        "rsi": float(rsi.iloc[i]),
        "price": price,
    }


def compute_signals_series(df: pd.DataFrame, weights: dict | None = None, vol_mode: str = "current"):
    """Vectorized: compute (score, bullish, bearish, adx) for EVERY bar at once.

    vol_mode controls how the (inherently direction-blind) volume signal is treated —
    this is the long/short SYMMETRY knob (volume normally only ever votes bullish, which
    structurally starves the PUT breadth gate):
      "current"     — volume score is +only, counts toward bullish breadth (legacy; parity)
      "directional" — volume confirms the move: its magnitude is signed by the other active
                      signals' consensus, so a heavy red bar votes BEARISH (symmetric breadth)
      "conviction"  — volume still feeds the score, but is EXCLUDED from bull/bear breadth
                      counts (breadth = directional signals only; symmetric)

    Indicators are computed once over the whole series; only the cheap per-bar
    scoring runs in the loop. `weights` selects WHICH signals are active and their
    relative weight (keys: rsi, macd, ema_cross, bollinger, stoch_rsi, volume, vwap).
    Weights are renormalized to sum to 1.0 so BUY_THRESHOLD stays comparable across
    different signal subsets (enables ablation). bullish/bearish count only the
    ACTIVE (weighted) signals. The ADX series is returned for use as a regime gate.

    scores[0] is None (no prior bar), like the original engine.
    """
    if weights is None:
        weights = WEIGHTS
    active = {k: w for k, w in weights.items() if w > 0}
    total_w = sum(active.values()) or 1.0
    norm = {k: w / total_w for k, w in active.items()}

    n = len(df)
    if n < 2:
        return [None] * n, [0] * n, [0] * n, [float("nan")] * n

    close = df["Close"]
    volume = df["Volume"]
    rsi = compute_rsi(close)
    macd_line, signal_line, _ = compute_macd(close)
    ema_fast, ema_slow = compute_ema_cross(close)
    bb_upper, bb_middle, bb_lower = compute_bollinger_bands(close)
    stoch_rsi = compute_stoch_rsi(close)
    avg_volume = volume.rolling(window=VOLUME_AVG_WINDOW).mean()
    vwap = compute_vwap(df)   # always computed (needed for the VWAP directional gate)
    adx = compute_adx(df)
    adx_vals = [float(x) for x in adx.to_numpy()]
    vwap_vals = [float(x) for x in vwap.to_numpy()]

    scores: list = [None]
    bullish: list = [0]
    bearish: list = [0]
    for i in range(1, n):
        try:
            s = {
                "rsi": score_rsi(float(rsi.iloc[i])),
                "macd": score_macd(float(macd_line.iloc[i]), float(signal_line.iloc[i]),
                                   float(macd_line.iloc[i - 1]), float(signal_line.iloc[i - 1])),
                "ema_cross": score_ema_cross(float(ema_fast.iloc[i]), float(ema_slow.iloc[i]),
                                             float(ema_fast.iloc[i - 1]), float(ema_slow.iloc[i - 1])),
                "bollinger": score_bollinger(float(close.iloc[i]), float(bb_upper.iloc[i]),
                                             float(bb_lower.iloc[i]), float(bb_middle.iloc[i])),
                "stoch_rsi": score_stoch_rsi(float(stoch_rsi.iloc[i]), float(stoch_rsi.iloc[i - 1])),
            }
            av = float(avg_volume.iloc[i])
            s["volume"] = score_volume(float(volume.iloc[i]), av) if av > 0 else 0.0
            if "vwap" in norm:
                s["vwap"] = score_vwap(float(close.iloc[i]), float(vwap.iloc[i]))
            # symmetry knob: sign the volume vote by the directional consensus of the
            # OTHER active signals so a heavy red bar can vote bearish (not just bullish).
            if vol_mode == "directional" and "volume" in norm and s["volume"] > 0:
                consensus = sum(s[k] * norm[k] for k in norm if k != "volume")
                if consensus < 0:
                    s["volume"] = -s["volume"]
            scores.append(sum(s[k] * norm[k] for k in norm))
            # "conviction": volume feeds the score but is not a breadth vote (symmetric).
            count_keys = [k for k in norm if not (vol_mode == "conviction" and k == "volume")]
            bullish.append(sum(1 for k in count_keys if s[k] > 0))
            bearish.append(sum(1 for k in count_keys if s[k] < 0))
        except (ValueError, IndexError):
            scores.append(0.0)
            bullish.append(0)
            bearish.append(0)
    return scores, bullish, bearish, adx_vals, vwap_vals


def score_options_signals(iv_rank: float, put_call_ratio: float, vol_oi_ratio: float) -> float:
    """Options-specific bonus/penalty, ported from options.compute_options_signals.

    Returns the weighted bonus to ADD to the technical score (matches
    show_trades.py:42-46). `iv_rank` is a 0-100 percentile; put_call_ratio and
    vol_oi_ratio are raw ratios. With real greeks/IV from ThetaData these become
    accurate (vs the crude yfinance approximations in the original).
    """
    # IV rank score (low IV = cheap = boost)
    if iv_rank <= 20:
        iv_score = 1.0
    elif iv_rank <= 35:
        iv_score = 0.7
    elif iv_rank <= 50:
        iv_score = 0.3
    elif iv_rank <= 70:
        iv_score = 0.0
    elif iv_rank <= 85:
        iv_score = -0.5
    else:
        iv_score = -1.0

    # Put/Call volume ratio (low = bullish call flow)
    if put_call_ratio <= 0.4:
        pc_score = 1.0
    elif put_call_ratio <= 0.6:
        pc_score = 0.7
    elif put_call_ratio <= 0.8:
        pc_score = 0.3
    elif put_call_ratio <= 1.2:
        pc_score = 0.0
    elif put_call_ratio <= 1.5:
        pc_score = -0.5
    else:
        pc_score = -1.0

    # Unusual volume vs OI (high = smart money)
    if vol_oi_ratio >= 1.5:
        uv_score = 1.0
    elif vol_oi_ratio >= 1.0:
        uv_score = 0.7
    elif vol_oi_ratio >= 0.5:
        uv_score = 0.3
    elif vol_oi_ratio >= 0.2:
        uv_score = 0.0
    else:
        uv_score = -0.3

    return (
        iv_score * OPTIONS_SIGNAL_WEIGHTS["iv_rank"]
        + pc_score * OPTIONS_SIGNAL_WEIGHTS["put_call"]
        + uv_score * OPTIONS_SIGNAL_WEIGHTS["unusual_vol"]
    )
