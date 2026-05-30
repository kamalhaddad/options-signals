"""
Options contract picker — selects optimal call/put contracts based on signals.

For BUY (bullish) signals → picks CALL options
For SELL (bearish) signals → picks PUT options

Selection criteria:
- Nearest weekly/monthly expiry (3-10 days out for scalping)
- Slightly OTM strike (best risk/reward for momentum plays)
- Delta ~0.35-0.55 sweet spot (good leverage without excessive theta)
- Reasonable bid-ask spread and open interest
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

import yfinance as yf


@dataclass
class OptionPick:
    ticker: str
    contract_symbol: str
    option_type: str  # "CALL" or "PUT"
    strike: float
    expiry: str
    days_to_expiry: int
    premium: float  # last price of the contract
    bid: float
    ask: float
    spread_pct: float  # bid-ask spread as % of mid
    delta: float | None
    implied_vol: float | None
    open_interest: int
    volume: int
    stock_price: float
    otm_pct: float  # how far OTM as % of stock price


def get_options_chain(ticker: str) -> dict | None:
    """Fetch the full options chain for a ticker."""
    try:
        stock = yf.Ticker(ticker)
        expirations = stock.options
        if not expirations:
            return None
        return {"stock": stock, "expirations": expirations}
    except Exception:
        return None


def pick_expiry(expirations: list[str], min_days: int = 3, max_days: int = 14) -> str | None:
    """Pick the best expiry date — nearest one that's 3-14 days out."""
    today = datetime.now().date()
    best = None
    best_days = float("inf")

    for exp_str in expirations:
        exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
        days = (exp_date - today).days
        if min_days <= days <= max_days and days < best_days:
            best = exp_str
            best_days = days

    # If nothing in range, take the nearest available
    if best is None:
        for exp_str in expirations:
            exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
            days = (exp_date - today).days
            if days > 0 and days < best_days:
                best = exp_str
                best_days = days

    return best


def pick_strike(chain_df, stock_price: float, option_type: str, target_otm_pct: float = 2.0):
    """
    Pick the optimal strike price.

    For CALLS: slightly OTM (1-3% above current price)
    For PUTS: slightly OTM (1-3% below current price)

    Returns the best row from the chain dataframe.
    """
    if chain_df.empty:
        return None

    if option_type == "CALL":
        # Target strike 1-3% above stock price
        target_strike = stock_price * (1 + target_otm_pct / 100)
        # Filter to strikes near target, must have volume/OI
        candidates = chain_df[
            (chain_df["strike"] >= stock_price * 0.99) &
            (chain_df["strike"] <= stock_price * 1.05) &
            (chain_df["openInterest"] > 10) &
            (chain_df["lastPrice"] > 0.10)
        ].copy()
    else:
        # Target strike 1-3% below stock price
        target_strike = stock_price * (1 - target_otm_pct / 100)
        candidates = chain_df[
            (chain_df["strike"] >= stock_price * 0.95) &
            (chain_df["strike"] <= stock_price * 1.01) &
            (chain_df["openInterest"] > 10) &
            (chain_df["lastPrice"] > 0.10)
        ].copy()

    if candidates.empty:
        # Fallback: just get nearest ATM with any liquidity
        candidates = chain_df[
            (chain_df["strike"] >= stock_price * 0.97) &
            (chain_df["strike"] <= stock_price * 1.03) &
            (chain_df["lastPrice"] > 0.05)
        ].copy()

    if candidates.empty:
        return None

    # Score each candidate with composite ranking
    candidates["dist"] = abs(candidates["strike"] - target_strike)
    candidates["spread"] = candidates["ask"] - candidates["bid"]
    candidates["mid"] = (candidates["ask"] + candidates["bid"]) / 2
    candidates["spread_pct"] = candidates["spread"] / candidates["mid"].replace(0, 1) * 100

    # IV filter — skip contracts with IV > 150% (way overpriced)
    if "impliedVolatility" in candidates.columns:
        iv_filtered = candidates[candidates["impliedVolatility"] < 1.5]
        if not iv_filtered.empty:
            candidates = iv_filtered

    # Prefer tight spreads (< 15%) and good open interest
    tight = candidates[candidates["spread_pct"] < 15]
    if not tight.empty:
        candidates = tight

    # Composite score: distance to target (40%), spread (30%), OI (30%)
    candidates["dist_rank"] = candidates["dist"].rank(pct=True)
    candidates["spread_rank"] = candidates["spread_pct"].rank(pct=True)
    candidates["oi_rank"] = 1 - candidates["openInterest"].rank(pct=True)  # higher OI = better
    candidates["composite"] = (
        candidates["dist_rank"] * 0.4
        + candidates["spread_rank"] * 0.3
        + candidates["oi_rank"] * 0.3
    )

    candidates = candidates.sort_values("composite")
    return candidates.iloc[0]


def estimate_delta(stock_price: float, strike: float, option_type: str, days_to_expiry: int) -> float:
    """
    Rough delta estimate without Black-Scholes.
    Uses moneyness and time as a proxy.
    """
    moneyness = stock_price / strike if option_type == "CALL" else strike / stock_price

    if moneyness >= 1.05:
        delta = 0.75
    elif moneyness >= 1.02:
        delta = 0.60
    elif moneyness >= 0.99:
        delta = 0.50
    elif moneyness >= 0.97:
        delta = 0.40
    elif moneyness >= 0.95:
        delta = 0.30
    else:
        delta = 0.20

    # Adjust for time — shorter expiry = more extreme deltas
    if days_to_expiry <= 3:
        if moneyness < 0.99:
            delta *= 0.7  # OTM deltas shrink near expiry
        else:
            delta = min(delta * 1.2, 0.90)  # ITM deltas grow

    return round(delta, 2)


def pick_option(ticker: str, signal: str, stock_price: float) -> OptionPick | None:
    """
    Pick the optimal options contract for a signal.

    signal: "BUY" → pick a CALL
    signal: "SELL" → pick a PUT
    """
    chain_data = get_options_chain(ticker)
    if not chain_data:
        return None

    stock = chain_data["stock"]
    expirations = chain_data["expirations"]

    option_type = "CALL" if signal == "BUY" else "PUT"

    # Pick expiry
    expiry = pick_expiry(expirations)
    if not expiry:
        return None

    # Get the chain for that expiry
    try:
        opt_chain = stock.option_chain(expiry)
        chain_df = opt_chain.calls if option_type == "CALL" else opt_chain.puts
    except Exception:
        return None

    if chain_df.empty:
        return None

    # Pick the best strike
    best = pick_strike(chain_df, stock_price, option_type)
    if best is None:
        return None

    strike = float(best["strike"])
    expiry_date = datetime.strptime(expiry, "%Y-%m-%d").date()
    days_to_expiry = (expiry_date - datetime.now().date()).days

    # Calculate spread
    bid = float(best.get("bid", 0))
    ask = float(best.get("ask", 0))
    mid = (bid + ask) / 2 if (bid + ask) > 0 else float(best["lastPrice"])
    spread_pct = ((ask - bid) / mid * 100) if mid > 0 else 0

    # OTM percentage
    if option_type == "CALL":
        otm_pct = (strike - stock_price) / stock_price * 100
    else:
        otm_pct = (stock_price - strike) / stock_price * 100

    # Delta estimate
    delta = estimate_delta(stock_price, strike, option_type, days_to_expiry)

    # IV from chain if available
    iv = float(best.get("impliedVolatility", 0)) if "impliedVolatility" in best.index else None

    return OptionPick(
        ticker=ticker,
        contract_symbol=str(best.get("contractSymbol", "")),
        option_type=option_type,
        strike=strike,
        expiry=expiry,
        days_to_expiry=days_to_expiry,
        premium=float(best["lastPrice"]),
        bid=bid,
        ask=ask,
        spread_pct=round(spread_pct, 1),
        delta=delta,
        implied_vol=round(iv * 100, 1) if iv else None,
        open_interest=int(best.get("openInterest", 0)),
        volume=int(best.get("volume", 0)) if best.get("volume") and str(best.get("volume")) != "nan" else 0,
        stock_price=round(stock_price, 2),
        otm_pct=round(otm_pct, 2),
    )


@dataclass
class OptionsSignals:
    """Options-specific signals derived from the options chain."""
    iv_rank_score: float       # -1 to +1: low IV = cheap options = good
    put_call_score: float      # -1 to +1: low P/C = bullish flow
    unusual_vol_score: float   # -1 to +1: high vol/OI = smart money entering
    iv_rank: float | None      # raw IV percentile (0-100)
    put_call_ratio: float | None
    vol_oi_ratio: float | None


def compute_options_signals(ticker: str) -> OptionsSignals | None:
    """
    Compute options-specific signals from the chain data.
    Returns scores for IV Rank, Put/Call Ratio, and Unusual Volume.
    """
    try:
        stock = yf.Ticker(ticker)
        expirations = stock.options
        if not expirations:
            return None

        # Pick nearest expiry for most relevant data
        expiry = expirations[0]
        chain = stock.option_chain(expiry)
        calls = chain.calls
        puts = chain.puts

        if calls.empty or puts.empty:
            return None

        # ── 1. IV Rank — is IV high or low vs the chain? ──
        all_ivs = []
        if "impliedVolatility" in calls.columns:
            all_ivs.extend(calls["impliedVolatility"].dropna().tolist())
        if "impliedVolatility" in puts.columns:
            all_ivs.extend(puts["impliedVolatility"].dropna().tolist())

        if all_ivs:
            avg_iv = sum(all_ivs) / len(all_ivs)
            # Simple rank: where does current avg IV sit in the range?
            iv_min = min(all_ivs)
            iv_max = max(all_ivs)
            if iv_max > iv_min:
                iv_rank = (avg_iv - iv_min) / (iv_max - iv_min) * 100
            else:
                iv_rank = 50.0
        else:
            iv_rank = None

        # Score IV: low IV = cheap options = good for buying
        if iv_rank is not None:
            if iv_rank <= 20:
                iv_score = 1.0    # Very cheap options
            elif iv_rank <= 35:
                iv_score = 0.7
            elif iv_rank <= 50:
                iv_score = 0.3
            elif iv_rank <= 70:
                iv_score = 0.0    # Normal
            elif iv_rank <= 85:
                iv_score = -0.5   # Expensive
            else:
                iv_score = -1.0   # Very expensive — avoid buying
        else:
            iv_score = 0.0

        # ── 2. Put/Call Volume Ratio ──
        call_vol = calls["volume"].sum() if "volume" in calls.columns else 0
        put_vol = puts["volume"].sum() if "volume" in puts.columns else 0

        # Handle NaN
        if str(call_vol) == "nan":
            call_vol = 0
        if str(put_vol) == "nan":
            put_vol = 0

        call_vol = float(call_vol)
        put_vol = float(put_vol)

        if call_vol > 0:
            pc_ratio = put_vol / call_vol
        else:
            pc_ratio = None

        # Score: low P/C = bullish (more calls being bought)
        if pc_ratio is not None:
            if pc_ratio <= 0.4:
                pc_score = 1.0    # Very bullish flow
            elif pc_ratio <= 0.6:
                pc_score = 0.7    # Bullish
            elif pc_ratio <= 0.8:
                pc_score = 0.3    # Slightly bullish
            elif pc_ratio <= 1.2:
                pc_score = 0.0    # Neutral
            elif pc_ratio <= 1.5:
                pc_score = -0.5   # Bearish flow
            else:
                pc_score = -1.0   # Very bearish
        else:
            pc_score = 0.0

        # ── 3. Unusual Volume vs Open Interest ──
        # If total option volume >> total OI, big money is opening new positions
        call_oi = calls["openInterest"].sum() if "openInterest" in calls.columns else 0
        put_oi = puts["openInterest"].sum() if "openInterest" in puts.columns else 0

        if str(call_oi) == "nan":
            call_oi = 0
        if str(put_oi) == "nan":
            put_oi = 0

        total_vol = call_vol + put_vol
        total_oi = float(call_oi) + float(put_oi)

        if total_oi > 0:
            vol_oi_ratio = total_vol / total_oi
        else:
            vol_oi_ratio = None

        # Score: high vol/OI = unusual activity = smart money entering
        if vol_oi_ratio is not None:
            if vol_oi_ratio >= 1.5:
                uv_score = 1.0    # Massive unusual activity
            elif vol_oi_ratio >= 1.0:
                uv_score = 0.7
            elif vol_oi_ratio >= 0.5:
                uv_score = 0.3
            elif vol_oi_ratio >= 0.2:
                uv_score = 0.0
            else:
                uv_score = -0.3   # Dead options chain — no interest
        else:
            uv_score = 0.0

        return OptionsSignals(
            iv_rank_score=iv_score,
            put_call_score=pc_score,
            unusual_vol_score=uv_score,
            iv_rank=round(iv_rank, 1) if iv_rank is not None else None,
            put_call_ratio=round(pc_ratio, 2) if pc_ratio is not None else None,
            vol_oi_ratio=round(vol_oi_ratio, 2) if vol_oi_ratio is not None else None,
        )
    except Exception:
        return None


def estimate_option_pnl(pick: OptionPick, stock_pnl_pct: float) -> float:
    """
    Estimate option contract P&L based on stock price movement.
    Uses delta as the leverage multiplier.

    For a CALL with delta 0.50 and stock moves +2%:
        option moves ~ (delta * stock_move / premium) * 100
    """
    if not pick.delta or pick.premium <= 0:
        return 0.0

    stock_move = pick.stock_price * (stock_pnl_pct / 100)
    option_move = stock_move * pick.delta
    option_pnl_pct = (option_move / pick.premium) * 100

    return round(option_pnl_pct, 2)
