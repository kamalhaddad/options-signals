import numpy as np
import pandas as pd
import yfinance as yf
from dataclasses import dataclass

import config


@dataclass
class SignalResult:
    ticker: str
    signal: str  # "BUY", "SELL", or "NEUTRAL"
    score: float
    price: float
    details: dict


def fetch_data(ticker: str, period: str = "3mo", interval: str = "1d") -> pd.DataFrame:
    """Fetch historical price data from yfinance."""
    df = yf.download(ticker, period=period, interval=interval, progress=False)
    if df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


# ── Core Indicators ──────────────────────────────────────────────────────────

def compute_rsi(close: pd.Series, period: int = config.RSI_PERIOD) -> pd.Series:
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_macd(close: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    ema_fast = close.ewm(span=config.MACD_FAST, adjust=False).mean()
    ema_slow = close.ewm(span=config.MACD_SLOW, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=config.MACD_SIGNAL, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def compute_ema_cross(close: pd.Series) -> tuple[pd.Series, pd.Series]:
    ema_fast = close.ewm(span=config.EMA_FAST, adjust=False).mean()
    ema_slow = close.ewm(span=config.EMA_SLOW, adjust=False).mean()
    return ema_fast, ema_slow


def compute_bollinger_bands(close: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    middle = close.rolling(window=config.BOLLINGER_PERIOD).mean()
    std = close.rolling(window=config.BOLLINGER_PERIOD).std()
    upper = middle + config.BOLLINGER_STD * std
    lower = middle - config.BOLLINGER_STD * std
    return upper, middle, lower


def compute_stoch_rsi(close: pd.Series, period: int = config.STOCH_RSI_PERIOD) -> pd.Series:
    rsi = compute_rsi(close, period)
    rsi_min = rsi.rolling(window=period).min()
    rsi_max = rsi.rolling(window=period).max()
    stoch_rsi = (rsi - rsi_min) / (rsi_max - rsi_min) * 100
    return stoch_rsi


# ── New Indicators ───────────────────────────────────────────────────────────

def compute_vwap(df: pd.DataFrame) -> pd.Series:
    """Volume Weighted Average Price — key institutional intraday level."""
    typical_price = (df["High"] + df["Low"] + df["Close"]) / 3
    cum_vol = df["Volume"].cumsum()
    cum_tp_vol = (typical_price * df["Volume"]).cumsum()
    vwap = cum_tp_vol / cum_vol
    return vwap


def compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average Directional Index — measures trend strength (>20 = trending)."""
    high = df["High"]
    low = df["Low"]
    close = df["Close"]

    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs()
    ], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1/period, min_periods=period).mean()
    plus_di = 100 * (plus_dm.ewm(alpha=1/period, min_periods=period).mean() / atr)
    minus_di = 100 * (minus_dm.ewm(alpha=1/period, min_periods=period).mean() / atr)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1)
    adx = dx.ewm(alpha=1/period, min_periods=period).mean()
    return adx


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range — measures volatility for dynamic stops."""
    high = df["High"]
    low = df["Low"]
    close = df["Close"]

    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs()
    ], axis=1).max(axis=1)

    return tr.ewm(alpha=1/period, min_periods=period).mean()


# ── Scoring Functions ────────────────────────────────────────────────────────

def score_rsi(rsi_value: float) -> float:
    """Graduated RSI scoring — more levels = wider score spread."""
    if rsi_value <= 20:
        return 1.0    # Deeply oversold
    elif rsi_value <= config.RSI_OVERSOLD:
        return 0.8
    elif rsi_value <= 40:
        return 0.5
    elif rsi_value <= 45:
        return 0.2
    elif rsi_value >= 80:
        return -1.0   # Deeply overbought
    elif rsi_value >= config.RSI_OVERBOUGHT:
        return -0.8
    elif rsi_value >= 60:
        return -0.5
    elif rsi_value >= 55:
        return -0.2
    return 0.0


def score_macd(macd_line: float, signal_line: float, prev_macd: float, prev_signal: float) -> float:
    """MACD with magnitude — wider gap = stronger score."""
    bullish_cross = prev_macd <= prev_signal and macd_line > signal_line
    bearish_cross = prev_macd >= prev_signal and macd_line < signal_line

    if bullish_cross:
        return 1.0
    elif bearish_cross:
        return -1.0
    elif macd_line > signal_line:
        gap = macd_line - signal_line
        # Scale: larger gap above signal = stronger bullish
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
    """EMA cross with spread magnitude."""
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
    """Continuous Bollinger scoring based on band position."""
    band_width = upper - lower
    if band_width == 0:
        return 0.0

    position = (price - lower) / band_width  # 0 = lower, 1 = upper

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
    """Graduated StochRSI with turning point detection."""
    if stoch_value <= 10:
        if stoch_value > prev_value:
            return 1.0   # Deeply oversold + turning up
        return 0.7
    elif stoch_value <= config.STOCH_RSI_OVERSOLD:
        if stoch_value > prev_value:
            return 0.8
        return 0.4
    elif stoch_value <= 35:
        if stoch_value > prev_value:
            return 0.3
        return 0.0
    elif stoch_value >= 90:
        if stoch_value < prev_value:
            return -1.0  # Deeply overbought + turning down
        return -0.7
    elif stoch_value >= config.STOCH_RSI_OVERBOUGHT:
        if stoch_value < prev_value:
            return -0.8
        return -0.4
    elif stoch_value >= 65:
        if stoch_value < prev_value:
            return -0.3
        return 0.0
    return 0.0


def score_volume(current_vol: float, avg_vol: float) -> float:
    """Graduated volume scoring — higher spikes = stronger conviction."""
    if avg_vol == 0:
        return 0.0
    ratio = current_vol / avg_vol
    if ratio >= 2.5:
        return 1.0    # Massive volume spike
    elif ratio >= 2.0:
        return 0.8
    elif ratio >= config.VOLUME_SPIKE_MULTIPLIER:
        return 0.6
    elif ratio >= 1.2:
        return 0.3
    elif ratio >= 1.0:
        return 0.1
    return 0.0


def score_vwap(price: float, vwap: float) -> float:
    """Score price relative to VWAP. Above = bullish, below = bearish.
    Asymmetric: rewards being above more than it penalizes being below,
    since stocks can bounce off VWAP support."""
    if vwap == 0:
        return 0.0
    pct_from_vwap = (price - vwap) / vwap * 100

    if pct_from_vwap > 0.5:
        return 1.0   # Solidly above VWAP — strong bullish
    elif pct_from_vwap > 0.1:
        return 0.5   # Slightly above — bullish
    elif pct_from_vwap > -0.2:
        return 0.0   # Near VWAP — neutral (could bounce)
    elif pct_from_vwap > -0.8:
        return -0.3  # Below VWAP — mild bearish
    return -0.7      # Well below VWAP — bearish but not max penalty


def score_adx(adx_value: float) -> float:
    """Score trend strength. Penalizes choppy markets, neutral/positive in trends."""
    if adx_value >= 30:
        return 0.5   # Strong trend — mild boost
    elif adx_value >= 20:
        return 0.3   # Trend present — slight positive
    elif adx_value >= 15:
        return 0.0   # Borderline — neutral
    return -1.0      # No trend at all — strong penalty to block entry


# ── Analysis ─────────────────────────────────────────────────────────────────

def analyze_ticker(ticker: str) -> SignalResult | None:
    """Run full analysis on a single ticker and return signal."""
    df = fetch_data(ticker)
    if df.empty or len(df) < 30:
        return None

    close = df["Close"]
    volume = df["Volume"]
    current_price = float(close.iloc[-1])

    # Compute all indicators (original 6)
    rsi = compute_rsi(close)
    macd_line, signal_line, histogram = compute_macd(close)
    ema_fast, ema_slow = compute_ema_cross(close)
    bb_upper, bb_middle, bb_lower = compute_bollinger_bands(close)
    stoch_rsi = compute_stoch_rsi(close)
    avg_volume = volume.rolling(window=20).mean()

    # Get current and previous values
    rsi_val = float(rsi.iloc[-1])
    macd_val = float(macd_line.iloc[-1])
    signal_val = float(signal_line.iloc[-1])
    prev_macd = float(macd_line.iloc[-2])
    prev_signal = float(signal_line.iloc[-2])
    ema_fast_val = float(ema_fast.iloc[-1])
    ema_slow_val = float(ema_slow.iloc[-1])
    prev_ema_fast = float(ema_fast.iloc[-2])
    prev_ema_slow = float(ema_slow.iloc[-2])
    bb_upper_val = float(bb_upper.iloc[-1])
    bb_lower_val = float(bb_lower.iloc[-1])
    bb_middle_val = float(bb_middle.iloc[-1])
    stoch_val = float(stoch_rsi.iloc[-1])
    prev_stoch = float(stoch_rsi.iloc[-2])
    vol_val = float(volume.iloc[-1])
    avg_vol_val = float(avg_volume.iloc[-1])

    # Score each indicator
    scores = {
        "rsi": score_rsi(rsi_val),
        "macd": score_macd(macd_val, signal_val, prev_macd, prev_signal),
        "ema_cross": score_ema_cross(ema_fast_val, ema_slow_val, prev_ema_fast, prev_ema_slow),
        "bollinger": score_bollinger(current_price, bb_upper_val, bb_lower_val, bb_middle_val),
        "stoch_rsi": score_stoch_rsi(stoch_val, prev_stoch),
        "volume": score_volume(vol_val, avg_vol_val),
    }

    # Compute weighted combined score
    combined_score = sum(scores[k] * config.WEIGHTS[k] for k in scores)

    # Determine signal
    bullish_count = sum(1 for v in scores.values() if v > 0)
    min_bullish = getattr(config, "MIN_BULLISH_INDICATORS", 0)

    if combined_score >= config.BUY_THRESHOLD and bullish_count >= min_bullish:
        signal = "BUY"
    elif combined_score <= config.SELL_THRESHOLD:
        signal = "SELL"
    else:
        signal = "NEUTRAL"

    details = {
        "rsi": rsi_val,
        "macd_histogram": float(histogram.iloc[-1]),
        "ema_trend": "BULLISH" if ema_fast_val > ema_slow_val else "BEARISH",
        "bb_position": round((current_price - bb_lower_val) / (bb_upper_val - bb_lower_val) * 100, 1) if bb_upper_val != bb_lower_val else 50.0,
        "stoch_rsi": stoch_val,
        "volume_ratio": round(vol_val / avg_vol_val, 2) if avg_vol_val > 0 else 0,
        "indicator_scores": scores,
    }

    return SignalResult(
        ticker=ticker,
        signal=signal,
        score=round(combined_score, 3),
        price=round(current_price, 2),
        details=details,
    )


def scan_watchlist() -> list[SignalResult]:
    results = []
    for ticker in config.WATCHLIST:
        result = analyze_ticker(ticker)
        if result and result.signal != "NEUTRAL":
            results.append(result)
    return results
