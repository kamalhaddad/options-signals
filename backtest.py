"""
Utility functions for intraday data fetching and signal computation.
Used by show_trades.py for options backtesting.
"""

import pandas as pd
import numpy as np
import yfinance as yf

import config
from signals import (
    compute_rsi,
    compute_macd,
    compute_ema_cross,
    compute_bollinger_bands,
    compute_stoch_rsi,
    score_rsi,
    score_macd,
    score_ema_cross,
    score_bollinger,
    score_stoch_rsi,
    score_volume,
)


def fetch_intraday_data(ticker: str) -> pd.DataFrame:
    """Fetch 5-day intraday data at 5-min intervals (need history for indicator warmup)."""
    df = yf.download(ticker, period="5d", interval="5m", progress=False)
    if df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def compute_signals_over_time(df: pd.DataFrame) -> pd.DataFrame:
    """Compute signal scores for every bar in the dataframe."""
    close = df["Close"]
    volume = df["Volume"]

    rsi = compute_rsi(close)
    macd_line, signal_line, histogram = compute_macd(close)
    ema_fast, ema_slow = compute_ema_cross(close)
    bb_upper, bb_middle, bb_lower = compute_bollinger_bands(close)
    stoch_rsi = compute_stoch_rsi(close)
    avg_volume = volume.rolling(window=20).mean()

    scores = []
    bullish_counts = []
    for i in range(1, len(df)):
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

            ind_scores = [rsi_score, macd_score, ema_score, boll_score, stoch_score, vol_score]

            combined = (
                rsi_score * config.WEIGHTS["rsi"]
                + macd_score * config.WEIGHTS["macd"]
                + ema_score * config.WEIGHTS["ema_cross"]
                + boll_score * config.WEIGHTS["bollinger"]
                + stoch_score * config.WEIGHTS["stoch_rsi"]
                + vol_score * config.WEIGHTS["volume"]
            )

            bullish_count = sum(1 for s in ind_scores if s > 0)
            scores.append(combined)
            bullish_counts.append(bullish_count)
        except (ValueError, IndexError):
            scores.append(0.0)
            bullish_counts.append(0)

    score_series = pd.Series([np.nan] + scores, index=df.index)
    bullish_series = pd.Series([0] + bullish_counts, index=df.index)
    df = df.copy()
    df["signal_score"] = score_series
    df["bullish_count"] = bullish_series
    return df
