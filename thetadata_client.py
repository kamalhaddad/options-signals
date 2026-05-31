"""
Thin client for the ThetaData Terminal REST API (host-local, 127.0.0.1:25510).

Used by the direct-on-ThetaData options backtester (`theta_backtest.py`). Runs on
the HOST in the same process as the backtest, so all requests share one source IP
— no Docker, no WRONG_IP (476) issues.

Endpoints/formats verified live against the running Terminal:
  stock OHLC  /v2/hist/stock/ohlc    -> [ms_of_day, o, h, l, c, volume, count, date]
  option quote /v2/hist/option/quote -> [ms_of_day, bid_size, bid_exch, bid, bid_cond,
                                          ask_size, ask_exch, ask, ask_cond, date]
  option greeks /v2/hist/option/greeks-> [ms_of_day, bid, ask, delta, theta, vega, rho,
                                          epsilon, lambda, implied_vol, iv_error,
                                          ms_of_day2, underlying_price, date]
  list/expirations, list/strikes      -> {"response": [int, ...]}  (strike = dollars*1000)

ThetaData returns HTTP 472 for "no data" (treated as empty), other 4xx/5xx raise.
"""

from __future__ import annotations
import os
import time
import pickle
import hashlib
import datetime
import requests
import pandas as pd

BASE_URL = os.getenv("THETADATA_URL", "http://127.0.0.1:25510")
FIVE_MIN_MS = 5 * 60 * 1000
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache", "thetadata")


class ThetaError(RuntimeError):
    pass


class ThetaClient:
    def __init__(self, base_url: str = BASE_URL, timeout: int = 60, retries: int = 3,
                 use_cache: bool = True, cache_dir: str = CACHE_DIR, offline: bool = False):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.s = requests.Session()
        self.use_cache = use_cache
        self.cache_dir = cache_dir
        self.offline = offline       # serve only from disk cache; never touch the network
        self.offline_misses = 0      # count of cache misses while offline (incomplete snapshot)
        self._bulk_memo: dict = {}   # in-memory (root,exp,day,datatype)->parsed bulk, shared by gex+gates
        if use_cache:
            os.makedirs(cache_dir, exist_ok=True)

    # ── disk cache (immutable past data only) ─────────────────────────────────
    @staticmethod
    def _cacheable(params: dict) -> bool:
        """Only cache fully-past data. Chain metadata (no end_date) is cacheable;
        hist data is cacheable iff end_date is strictly before today."""
        end = params.get("end_date")
        if end is None:
            return True
        today = int(datetime.date.today().strftime("%Y%m%d"))
        return int(end) < today

    def _cache_path(self, path: str, params: dict) -> str:
        key = path + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
        return os.path.join(self.cache_dir, hashlib.sha1(key.encode()).hexdigest() + ".pkl")

    # ── low-level ─────────────────────────────────────────────────────────────
    def _get(self, path: str, params: dict) -> tuple[list, list]:
        """Return (format, rows). Follows pagination. 472 -> empty. Disk-cached."""
        cache_ok = self.use_cache and self._cacheable(params)
        cpath = self._cache_path(path, params) if cache_ok else None
        if cache_ok and os.path.exists(cpath):
            try:
                with open(cpath, "rb") as f:
                    return pickle.load(f)
            except Exception:
                pass  # corrupt cache entry -> refetch

        if self.offline:
            # Terminal-free run: a miss means the snapshot doesn't cover this request.
            self.offline_misses += 1
            return [], []

        url = self.base_url + path
        fmt: list = []
        rows: list = []
        next_url = url
        next_params = dict(params)
        while next_url:
            last_exc = None
            for attempt in range(self.retries):
                try:
                    r = self.s.get(next_url, params=next_params, timeout=self.timeout)
                    break
                except requests.RequestException as e:
                    last_exc = e
                    time.sleep(0.5 * (attempt + 1))
            else:
                raise ThetaError(f"request failed for {path}: {last_exc}")

            if r.status_code == 472:        # NO_DATA (cache the empty result too)
                if cache_ok:
                    self._cache_store(cpath, (fmt, rows))
                return fmt, rows
            if r.status_code == 476:        # WRONG_IP
                raise ThetaError(
                    "476 WRONG_IP — the Terminal locked to a different client IP. "
                    "Restart ThetaTerminal and run only from this host."
                )
            if r.status_code != 200:
                raise ThetaError(f"{r.status_code} for {path}: {r.text[:200]}")

            payload = r.json()
            header = payload.get("header", {})
            fmt = header.get("format", fmt)
            rows.extend(payload.get("response", []) or [])

            nxt = header.get("next_page")
            if nxt and nxt != "null":
                next_url, next_params = nxt, {}   # next_page is a full URL
            else:
                next_url = None
        if cache_ok:
            self._cache_store(cpath, (fmt, rows))
        return fmt, rows

    def _cache_store(self, cpath: str, result) -> None:
        try:
            tmp = cpath + ".tmp"
            with open(tmp, "wb") as f:
                pickle.dump(result, f)
            os.replace(tmp, cpath)
        except Exception:
            pass  # cache best-effort

    @staticmethod
    def _to_dt(date_int: int, ms_of_day: int) -> pd.Timestamp:
        d = str(int(date_int))
        base = pd.Timestamp(year=int(d[:4]), month=int(d[4:6]), day=int(d[6:8]))
        return base + pd.Timedelta(milliseconds=int(ms_of_day))

    def _frame(self, fmt: list, rows: list) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows, columns=fmt)
        if "date" in df.columns and "ms_of_day" in df.columns:
            df.index = [self._to_dt(d, m) for d, m in zip(df["date"], df["ms_of_day"])]
        return df

    # ── chain enumeration ─────────────────────────────────────────────────────
    def expirations(self, root: str) -> list[int]:
        _, rows = self._get("/v2/list/expirations", {"root": root})
        return [int(x) for x in rows]

    def strikes(self, root: str, exp: int) -> list[int]:
        """Strikes in ThetaData integer units (dollars * 1000)."""
        _, rows = self._get("/v2/list/strikes", {"root": root, "exp": int(exp)})
        return [int(x) for x in rows]

    # ── histories ─────────────────────────────────────────────────────────────
    def stock_ohlc(self, root: str, start: int, end: int, ivl_ms: int = FIVE_MIN_MS) -> pd.DataFrame:
        fmt, rows = self._get("/v2/hist/stock/ohlc",
                              {"root": root, "start_date": start, "end_date": end, "ivl": ivl_ms})
        df = self._frame(fmt, rows)
        if df.empty:
            return df
        out = pd.DataFrame({
            "Open": df["open"].astype(float), "High": df["high"].astype(float),
            "Low": df["low"].astype(float), "Close": df["close"].astype(float),
            "Volume": df["volume"].astype(float),
        }, index=df.index)
        return out[(out["Close"] > 0)]

    def option_quote(self, root: str, exp: int, strike: int, right: str,
                     start: int, end: int, ivl_ms: int = FIVE_MIN_MS) -> pd.DataFrame:
        fmt, rows = self._get("/v2/hist/option/quote",
                              {"root": root, "exp": int(exp), "strike": int(strike),
                               "right": right, "start_date": start, "end_date": end, "ivl": ivl_ms})
        df = self._frame(fmt, rows)
        if df.empty:
            return df
        out = pd.DataFrame({
            "bid": df["bid"].astype(float), "ask": df["ask"].astype(float),
            "bid_size": df["bid_size"].astype(float), "ask_size": df["ask_size"].astype(float),
        }, index=df.index)
        out["mid"] = (out["bid"] + out["ask"]) / 2
        return out

    def bulk_hist(self, datatype: str, root: str, exp: int, date_i: int,
                  ivl_ms: int | None = None) -> tuple[list, list]:
        """Whole-expiration history in one call (Pro). datatype: open_interest, quote, ...
        Returns (tick_format, contracts) where each contract is
        {"contract": {root, expiration, strike, right}, "ticks": [[...], ...]}.

        Pass ivl_ms for tick-heavy types (quote/ohlc) to avoid 570 'request too large'
        — without it the response is tick-level for the whole chain. OI is one-per-day
        so needs no ivl. (OI format verified live: ['ms_of_day','open_interest','date'].)
        """
        params = {"root": root, "exp": int(exp), "start_date": date_i, "end_date": date_i}
        if ivl_ms is not None:
            params["ivl"] = ivl_ms
        return self._get(f"/v2/bulk_hist/option/{datatype}", params)

    def _bulk_frames(self, fmt: list, rows: list, fields: list) -> dict:
        """Bulk response -> {(strike_int, right): DataFrame[fields]} indexed by datetime.
        One contract per row ({"contract": {...}, "ticks": [[...]]}). Fields absent from
        `fmt` are skipped. Used to slice whole-expiration pulls into per-contract frames."""
        if not fmt or not rows:
            return {}
        has_ts = "ms_of_day" in fmt and "date" in fmt
        mi = fmt.index("ms_of_day") if has_ts else None
        di = fmt.index("date") if has_ts else None
        cols = {f: fmt.index(f) for f in fields if f in fmt}
        out: dict = {}
        for item in rows:
            if not isinstance(item, dict):
                continue
            c = item.get("contract", {})
            ticks = item.get("ticks", [])
            strike, right = c.get("strike"), c.get("right")
            if strike is None or right is None or not ticks:
                continue
            recs, index = [], []
            for tk in ticks:
                try:
                    rec = {f: float(tk[j]) for f, j in cols.items()}
                    ts = self._to_dt(tk[di], tk[mi]) if has_ts else None
                except (ValueError, IndexError, TypeError):
                    continue
                recs.append(rec)
                index.append(ts)
            if recs:
                out[(int(strike), right)] = pd.DataFrame(recs, index=index if has_ts else None)
        return out

    def has_bulk(self, datatype: str, root: str, exp: int, date_i: int, ivl_ms=None) -> bool:
        """True if this bulk pull is already in memory (e.g. fetched for the GEX profile),
        so a consumer can reuse it for free instead of a per-contract call."""
        return (datatype, root, int(exp), int(date_i), ivl_ms) in self._bulk_memo

    def _bulk_memoized(self, datatype: str, root: str, exp: int, date_i: int, ivl_ms=None):
        key = (datatype, root, int(exp), int(date_i), ivl_ms)
        if key not in self._bulk_memo:
            try:
                self._bulk_memo[key] = self.bulk_hist(datatype, root, exp, date_i, ivl_ms=ivl_ms)
            except ThetaError:
                self._bulk_memo[key] = ([], [])
        return self._bulk_memo[key]

    def bulk_option_quotes(self, root: str, exp: int, date_i: int,
                           ivl_ms: int = FIVE_MIN_MS) -> dict:
        """Whole-expiration quotes for one day in ONE call -> {(strike_int, right): quote_df}
        with bid/ask/sizes/mid. {} on error (caller falls back to per-contract)."""
        fmt, rows = self._bulk_memoized("quote", root, exp, date_i, ivl_ms)
        frames = self._bulk_frames(fmt, rows, ["bid", "ask", "bid_size", "ask_size"])
        for df in frames.values():
            if "bid" in df.columns and "ask" in df.columns:
                df["mid"] = (df["bid"] + df["ask"]) / 2
        return frames

    def bulk_option_oi(self, root: str, exp: int, date_i: int) -> dict:
        """Whole-expiration open interest for one day -> {(strike_int, right): oi_int} (last tick)."""
        fmt, rows = self._bulk_memoized("open_interest", root, exp, date_i)
        if not fmt or "open_interest" not in fmt:
            return {}
        oi_i = fmt.index("open_interest")
        out: dict = {}
        for item in rows:
            if not isinstance(item, dict):
                continue
            c, ticks = item.get("contract", {}), item.get("ticks", [])
            if c.get("strike") is None or c.get("right") is None or not ticks:
                continue
            try:
                out[(int(c["strike"]), c["right"])] = int(ticks[-1][oi_i])
            except (ValueError, IndexError, TypeError):
                continue
        return out

    def bulk_option_iv(self, root: str, exp: int, date_i: int, ivl_ms: int = FIVE_MIN_MS) -> dict:
        """Whole-expiration greeks for one day -> {(strike_int, right): first_positive_iv}.
        {} if the bulk greeks endpoint isn't available (caller falls back)."""
        fmt, rows = self._bulk_memoized("greeks", root, exp, date_i, ivl_ms)
        if not fmt or "implied_vol" not in fmt:
            return {}
        iv_i = fmt.index("implied_vol")
        out: dict = {}
        for item in rows:
            if not isinstance(item, dict):
                continue
            c, ticks = item.get("contract", {}), item.get("ticks", [])
            if c.get("strike") is None or c.get("right") is None or not ticks:
                continue
            for tk in ticks:
                try:
                    v = float(tk[iv_i])
                except (ValueError, IndexError, TypeError):
                    continue
                if v > 0:
                    out[(int(c["strike"]), c["right"])] = v
                    break
        return out

    def option_oi(self, root: str, exp: int, strike: int, right: str, date_i: int) -> int:
        """Open interest for a contract on a given day (0 if none)."""
        fmt, rows = self._get("/v2/hist/option/open_interest",
                              {"root": root, "exp": int(exp), "strike": int(strike),
                               "right": right, "start_date": date_i, "end_date": date_i})
        if not rows:
            return 0
        try:
            return int(rows[-1][fmt.index("open_interest")])
        except (ValueError, IndexError):
            return 0

    def option_greeks(self, root: str, exp: int, strike: int, right: str,
                      start: int, end: int, ivl_ms: int = FIVE_MIN_MS) -> pd.DataFrame:
        fmt, rows = self._get("/v2/hist/option/greeks",
                              {"root": root, "exp": int(exp), "strike": int(strike),
                               "right": right, "start_date": start, "end_date": end, "ivl": ivl_ms})
        df = self._frame(fmt, rows)
        if df.empty:
            return df
        return pd.DataFrame({
            "delta": df["delta"].astype(float),
            "implied_vol": df["implied_vol"].astype(float),
            "underlying_price": df["underlying_price"].astype(float),
        }, index=df.index)


# ── strike helpers ────────────────────────────────────────────────────────────
def dollars_to_strike(dollars: float) -> int:
    return int(round(dollars * 1000))


def strike_to_dollars(strike: int) -> float:
    return strike / 1000.0


if __name__ == "__main__":
    # Smoke test against the running Terminal.
    c = ThetaClient()
    print("expirations:", len(c.expirations("NVDA")))
    s = c.stock_ohlc("NVDA", 20240304, 20240304)
    print("stock bars:", len(s), s.iloc[0].to_dict() if len(s) else "none")
    q = c.option_quote("NVDA", 20240308, 850000, "C", 20240304, 20240304)
    print("option quote bars:", len(q), q.dropna().query("ask>0").iloc[0].to_dict() if len(q.query("ask>0")) else "none")
