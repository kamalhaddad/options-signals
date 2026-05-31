"""
Dealer Gamma Exposure (GEX) for SPY — a market-regime signal.

Idea (see STRATEGY.md): when dealers are net SHORT gamma (GEX < 0) they hedge WITH
the move, so days TREND — exactly when our trend-following scalper works. When net
LONG gamma (GEX > 0) they hedge AGAINST the move, pinning price → chop → stand down.

GEX is an ESTIMATE: it assumes the standard dealer-positioning sign convention
(dealers long calls / short puts → call gamma adds +, put gamma adds −). Gamma is
computed via Black-Scholes from the chain's IV (ThetaData's greeks response has no
gamma field), so no gamma-endpoint dependency.

Data: per expiration, two bulk calls — open_interest and greeks (for IV). Heavy, so
GEX is computed once per day and cached on disk via the client.

Bulk-response PARSING verified live (2026-05-30, SPY) via `gex_sweep.py --verify`:
  open_interest fmt = ['ms_of_day', 'open_interest', 'date']
  quote        fmt = ['ms_of_day','bid_size','bid_exchange','bid', ... ,'ask', ...]
contract dict = {root, expiration, strike (×1000), right}. OI counts and quote mids
were internally consistent (mids implied a single coherent spot). See _parse_bulk.
"""

from __future__ import annotations
import math
import pandas as pd
from thetadata_client import ThetaClient, strike_to_dollars

CONTRACT_MULTIPLIER = 100
RISK_FREE = 0.04
DEFAULT_MAX_DTE = 30
DEFAULT_STRIKE_PCT = 0.15   # only strikes within ±15% of spot (the gamma that matters)


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_gamma(spot: float, strike: float, t_years: float, iv: float, r: float = RISK_FREE) -> float:
    """Black-Scholes gamma (same for calls and puts)."""
    if spot <= 0 or strike <= 0 or t_years <= 0 or iv <= 0:
        return 0.0
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t_years) / (iv * math.sqrt(t_years))
    return _norm_pdf(d1) / (spot * iv * math.sqrt(t_years))


def bs_price(spot, strike, t, iv, right, r=RISK_FREE):
    if t <= 0 or iv <= 0:
        return max(0.0, (spot - strike) if right == "C" else (strike - spot))
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t) / (iv * math.sqrt(t))
    d2 = d1 - iv * math.sqrt(t)
    disc = math.exp(-r * t)
    if right == "C":
        return spot * _norm_cdf(d1) - strike * disc * _norm_cdf(d2)
    return strike * disc * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def implied_vol(price, spot, strike, t, right, r=RISK_FREE):
    """IV from option mid via bisection (MDDS-only path — no greeks/FPSS dependency)."""
    if price <= 0 or t <= 0:
        return 0.0
    intrinsic = max(0.0, (spot - strike) if right == "C" else (strike - spot))
    if price <= intrinsic + 1e-6:
        return 0.0
    lo, hi = 1e-3, 5.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if bs_price(spot, strike, t, mid, right, r) > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def _exp_to_ts(exp: int) -> pd.Timestamp:
    d = str(int(exp))
    return pd.Timestamp(year=int(d[:4]), month=int(d[4:6]), day=int(d[6:8]))


def _parse_bulk(fmt: list, rows: list, value_field: str) -> dict:
    """Bulk response -> {(strike_int, right): last_value} for the given tick field.
    `rows` items look like {"contract": {...}, "ticks": [[...], ...]}.

    Contract dict keys / tick field names per ThetaData; re-verify live."""
    out = {}
    if value_field not in (fmt or []):
        return out
    vi = fmt.index(value_field)
    for item in rows:
        c = item.get("contract", {}) if isinstance(item, dict) else {}
        ticks = item.get("ticks", []) if isinstance(item, dict) else []
        if not ticks:
            continue
        strike = c.get("strike")
        right = c.get("right")
        if strike is None or right is None:
            continue
        try:
            out[(int(strike), right)] = float(ticks[-1][vi])
        except (ValueError, IndexError, TypeError):
            continue
    return out


def _parse_bulk_mid(fmt: list, rows: list) -> dict:
    """Bulk quote -> {(strike_int, right): mid}. Quote fmt has 'bid' and 'ask'."""
    out = {}
    if not fmt or "bid" not in fmt or "ask" not in fmt:
        return out
    bi, ai = fmt.index("bid"), fmt.index("ask")
    for item in rows:
        c = item.get("contract", {}) if isinstance(item, dict) else {}
        ticks = item.get("ticks", []) if isinstance(item, dict) else []
        if not ticks or c.get("strike") is None or c.get("right") is None:
            continue
        try:
            bid, ask = float(ticks[-1][bi]), float(ticks[-1][ai])
        except (ValueError, IndexError, TypeError):
            continue
        if bid > 0 and ask > 0:
            out[(int(c["strike"]), c["right"])] = (bid + ask) / 2
    return out


def build_day_profile(client: ThetaClient, date_i: int, ref_spot: float,
                      root: str = "SPY", max_dte: int = DEFAULT_MAX_DTE,
                      strike_pct: float = DEFAULT_STRIKE_PCT) -> list:
    """The day's STATIC gamma inputs, pulled once: [(k_dollars, right, oi, iv, t_years), ...].

    OI updates only once/day (OCC publishes prior-day OI), and IV is snapshotted from the
    day's quote mids, so this profile is fixed intraday — only `spot` moves through it. That
    is what makes a per-bar GEX gate nearly free: build this once, then call net_gex_at() per
    bar. Strike band and IV reference are taken at `ref_spot` (use the day's open)."""
    date_ts = _exp_to_ts(date_i)
    exps = [e for e in client.expirations(root)
            if 0 <= (_exp_to_ts(e) - date_ts).days <= max_dte]
    lo, hi = ref_spot * (1 - strike_pct), ref_spot * (1 + strike_pct)
    profile = []
    for exp in exps:
        t_years = max((_exp_to_ts(exp) - date_ts).days, 0) / 365.0
        if t_years <= 0:
            t_years = 1 / 365.0
        # OI shares the client's bulk memo with the backtest's liquidity gates (one pull
        # per root/exp/day). IV is inverted from hourly quote mids (small, FPSS-free).
        oi = client.bulk_option_oi(root, exp, date_i)
        qframes = client.bulk_option_quotes(root, exp, date_i, ivl_ms=3600000)  # hourly -> last ~EOD
        for (strike, right), oi_val in oi.items():
            k = strike_to_dollars(strike)
            if not (lo <= k <= hi) or oi_val <= 0:
                continue
            df = qframes.get((strike, right))
            mid = float(df["mid"].iloc[-1]) if df is not None and not df.empty and "mid" in df.columns else 0.0
            sigma = implied_vol(mid, ref_spot, k, t_years, right)
            if sigma <= 0:
                continue
            profile.append((k, right, oi_val, sigma, t_years))
    return profile


def net_gex_at(profile: list, spot: float) -> float:
    """Net dealer GEX at `spot` over a fixed (intraday-static) profile (sticky-strike IV).
    Positive = dealers long gamma (pinned/chop); negative = short gamma (trending)."""
    net = 0.0
    for k, right, oi_val, sigma, t_years in profile:
        g = bs_gamma(spot, k, t_years, sigma)
        if g <= 0:
            continue
        contribution = g * oi_val * CONTRACT_MULTIPLIER * (spot ** 2) * 0.01
        net += contribution if right == "C" else -contribution
    return net


def gross_gex_at(profile: list, spot: float) -> float:
    """Total (unsigned) dealer gamma$ at `spot` — the ticker's own gamma scale. Used to
    normalize net GEX into a dimensionless conviction (-net/gross in [0,1]) so sizing is
    comparable across tickers with very different OI and price levels."""
    gross = 0.0
    for k, right, oi_val, sigma, t_years in profile:
        g = bs_gamma(spot, k, t_years, sigma)
        if g <= 0:
            continue
        gross += g * oi_val * CONTRACT_MULTIPLIER * (spot ** 2) * 0.01
    return gross


def flip_level(profile: list, lo: float, hi: float, steps: int = 120) -> float:
    """Spot where net GEX crosses 0 as spot rises (short-gamma below, long-gamma above).
    Returns +inf if short-gamma across the whole [lo,hi] band (always trend-on),
    -inf if long-gamma across it (always chop). For reporting; the gate uses net_gex_at
    directly so it needs no monotonicity assumption."""
    if not profile:
        return float("-inf")
    px = lo
    pv = net_gex_at(profile, px)
    step = (hi - lo) / steps
    for _ in range(steps):
        x = px + step
        v = net_gex_at(profile, x)
        if pv < 0 <= v:                      # short -> long crossing
            return px + step * (0 - pv) / (v - pv)
        px, pv = x, v
    return float("inf") if pv < 0 else float("-inf")


def spy_gex_for_date(client: ThetaClient, date_i: int, spot: float,
                     root: str = "SPY", max_dte: int = DEFAULT_MAX_DTE,
                     strike_pct: float = DEFAULT_STRIKE_PCT) -> float:
    """Net dealer GEX for `root` on `date_i`, evaluated at `spot` (the day-level value)."""
    profile = build_day_profile(client, date_i, spot, root=root, max_dte=max_dte, strike_pct=strike_pct)
    return net_gex_at(profile, spot)


def spy_gex_by_day(client: ThetaClient, day_to_spot: dict, **kw) -> dict:
    """{date_int: net_gex} for each trading day, given {date_int: spot}."""
    return {d: spy_gex_for_date(client, d, spot, **kw) for d, spot in day_to_spot.items()}


def gamma_walls(profile: list, spot: float) -> tuple:
    """(call_wall_$, put_wall_$): strikes carrying the most dealer gamma — magnets/barriers.
    Call wall (largest call gamma ABOVE spot) acts as resistance/pin; put wall (largest put
    gamma BELOW spot) acts as support. Returns (None, None) sides that don't exist."""
    call_by_k: dict[float, float] = {}
    put_by_k: dict[float, float] = {}
    for k, right, oi_val, sigma, t_years in profile:
        g = bs_gamma(spot, k, t_years, sigma)
        dollar = g * oi_val * CONTRACT_MULTIPLIER * (spot ** 2) * 0.01
        (call_by_k if right == "C" else put_by_k)[k] = \
            (call_by_k if right == "C" else put_by_k).get(k, 0.0) + dollar
    calls_above = {k: v for k, v in call_by_k.items() if k > spot}
    puts_below = {k: v for k, v in put_by_k.items() if k < spot}
    call_wall = max(calls_above, key=calls_above.get) if calls_above else None
    put_wall = max(puts_below, key=puts_below.get) if puts_below else None
    return call_wall, put_wall


def spy_gex_intraday(client: ThetaClient, spy_bars, max_dte: int = DEFAULT_MAX_DTE,
                     strike_pct: float = DEFAULT_STRIKE_PCT, root: str = "SPY") -> tuple[dict, dict]:
    """Per-bar net GEX + per-day context, for the intraday GEX signals.

    Returns:
      series   : {'YYYYMMDDHHMM': net_gex}            — net GEX at each bar's spot (timing + size)
      day_ctx  : {date_int: {'flip', 'call_wall', 'put_wall', 'ref_spot'}}  — strike/TP context

    `spy_bars` is a 5-min OHLC frame. Each day's profile is built ONCE at the day's open
    (OI/IV are intraday-static); net GEX is re-evaluated at every bar's close as spot slides
    through the fixed strike ladder. Walls/flip are day-level (computed at the open)."""
    by_day: dict[int, list] = {}
    for ts, row in spy_bars.iterrows():
        by_day.setdefault(int(ts.strftime("%Y%m%d")), []).append((ts, float(row["Open"]), float(row["Close"])))
    series: dict[str, float] = {}
    day_ctx: dict[int, dict] = {}
    for day, rows in by_day.items():
        ref_spot = rows[0][1]   # first bar's open ~ day open
        profile = build_day_profile(client, day, ref_spot, root=root, max_dte=max_dte, strike_pct=strike_pct)
        call_wall, put_wall = gamma_walls(profile, ref_spot)
        day_ctx[day] = {"flip": flip_level(profile, ref_spot * 0.90, ref_spot * 1.10),
                        "call_wall": call_wall, "put_wall": put_wall, "ref_spot": ref_spot}
        for ts, _open, close in rows:
            series[ts.strftime("%Y%m%d%H%M")] = net_gex_at(profile, close)
    return series, day_ctx


if __name__ == "__main__":
    # Synthetic sanity checks (no Terminal needed).
    g_atm = bs_gamma(600, 600, 7 / 365, 0.15)
    g_otm = bs_gamma(600, 660, 7 / 365, 0.15)
    print(f"BS gamma ATM={g_atm:.5f}  OTM(+10%)={g_otm:.5f}  (ATM >> OTM: {g_atm > g_otm})")
    # IV inversion round-trip: price a call at IV=0.20, recover it from the price.
    px = bs_price(600, 605, 14 / 365, 0.20, "C")
    iv_rec = implied_vol(px, 600, 605, 14 / 365, "C")
    print(f"IV round-trip: priced@0.20 -> ${px:.2f} -> recovered IV={iv_rec:.4f}  (≈0.20: {abs(iv_rec-0.20)<0.005})")
    ok = g_atm > g_otm > 0 and abs(iv_rec - 0.20) < 0.005
    print("MATH OK" if ok else "CHECK MATH")
