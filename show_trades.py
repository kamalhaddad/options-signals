"""Show all buy/sell signals with option contract details and estimated option P&L."""

import argparse
import pandas as pd
from backtest import fetch_intraday_data, compute_signals_over_time
from options import pick_option, estimate_option_pnl, compute_options_signals
import config


def show_all_trades(lookback_hours: int = 24):
    print(f"{'='*120}")
    print(f"  OPTIONS SCALP SIGNALS — PAST {lookback_hours} HOURS")
    print(f"  Buy: {config.BUY_THRESHOLD} | Sell: {config.SELL_THRESHOLD} | "
          f"TP: +{config.TAKE_PROFIT_PCT}% | SL: -{config.TRAILING_STOP_PCT}% | "
          f"Skip open: {getattr(config, 'SKIP_OPEN_MINUTES', 0)}min | "
          f"Skip close: {getattr(config, 'SKIP_CLOSE_MINUTES', 0)}min")
    print(f"{'='*120}")

    all_trades = []
    skip_minutes = getattr(config, "SKIP_OPEN_MINUTES", 0)

    # Options signal weights
    opt_weights = getattr(config, "OPTIONS_SIGNAL_WEIGHTS", {
        "iv_rank": 0.05, "put_call": 0.05, "unusual_vol": 0.05,
    })

    for ticker in config.WATCHLIST:
        print(f"  Scanning {ticker}...", end=" ", flush=True)
        df = fetch_intraday_data(ticker)
        if df.empty or len(df) < 50:
            print("skip")
            continue

        df = compute_signals_over_time(df)
        last_time = df.index[-1]
        cutoff = last_time - pd.Timedelta(hours=lookback_hours)
        window = df[df.index >= cutoff]

        # Fetch options-specific signals once per ticker
        opt_sigs = compute_options_signals(ticker)
        if opt_sigs:
            opt_bonus = (
                opt_sigs.iv_rank_score * opt_weights["iv_rank"]
                + opt_sigs.put_call_score * opt_weights["put_call"]
                + opt_sigs.unusual_vol_score * opt_weights["unusual_vol"]
            )
        else:
            opt_bonus = 0.0

        in_trade = False
        entry = None

        for idx, row in window.iterrows():
            score = row["signal_score"]
            price = float(row["Close"])

            if pd.isna(score):
                continue

            # Add options-specific bonus to the technical score
            adjusted_score = score + opt_bonus

            # Skip entries during open/close noise
            if not in_trade and hasattr(idx, 'hour'):
                minutes_since_open = (idx.hour * 60 + idx.minute) - (9 * 60 + 30)
                minutes_to_close = (16 * 60) - (idx.hour * 60 + idx.minute)
                skip_close = getattr(config, "SKIP_CLOSE_MINUTES", 0)
                if 0 <= minutes_since_open < skip_minutes:
                    continue
                if skip_close and minutes_to_close <= skip_close:
                    continue

            # Check take-profit and trailing stop (fixed %)
            if in_trade and entry:
                peak_price = entry.get("peak_price", entry["price"])
                peak_price = max(peak_price, price)
                entry["peak_price"] = peak_price
                entry_price = entry["price"]

                gain_pct = (price - entry_price) / entry_price * 100
                if gain_pct >= config.TAKE_PROFIT_PCT:
                    all_trades.append({**entry, "exit_price": price, "exit_time": idx,
                                       "exit_reason": f"Take profit (+{gain_pct:.1f}%)"})
                    in_trade = False
                    entry = None
                    continue

                drop_pct = (peak_price - price) / peak_price * 100
                if drop_pct >= config.TRAILING_STOP_PCT:
                    all_trades.append({**entry, "exit_price": price, "exit_time": idx,
                                       "exit_reason": f"Stop loss ({drop_pct:.1f}% drop)"})
                    in_trade = False
                    entry = None
                    continue

            min_bullish = getattr(config, "MIN_BULLISH_INDICATORS", 0)
            bullish = int(row.get("bullish_count", 0))

            # Use adjusted score (technical + options signals) for entry
            if adjusted_score >= config.BUY_THRESHOLD and bullish >= min_bullish and not in_trade:
                entry = {"ticker": ticker, "price": price, "time": idx,
                         "score": round(adjusted_score, 3), "peak_price": price, "signal": "BUY",
                         "opt_bonus": round(opt_bonus, 3)}
                in_trade = True

            elif adjusted_score <= config.SELL_THRESHOLD and in_trade:
                all_trades.append({**entry, "exit_price": price, "exit_time": idx,
                                   "exit_reason": "SELL signal"})
                in_trade = False
                entry = None

        if in_trade and entry:
            last_price = float(window["Close"].iloc[-1])
            all_trades.append({**entry, "exit_price": last_price, "exit_time": window.index[-1],
                               "exit_reason": "End of day (held)"})

        trade_count = sum(1 for t in all_trades if t["ticker"] == ticker)
        iv_str = f" IV:{opt_sigs.iv_rank:.0f}%" if opt_sigs and opt_sigs.iv_rank else ""
        pc_str = f" P/C:{opt_sigs.put_call_ratio}" if opt_sigs and opt_sigs.put_call_ratio else ""
        uv_str = f" V/OI:{opt_sigs.vol_oi_ratio}" if opt_sigs and opt_sigs.vol_oi_ratio else ""
        print(f"{trade_count} trades{iv_str}{pc_str}{uv_str}")

    all_trades.sort(key=lambda x: x["time"])

    # Fetch option contracts for each unique ticker entry
    print(f"\n  Fetching options chains...")
    option_cache = {}
    for t in all_trades:
        key = t["ticker"]
        if key not in option_cache:
            opt = pick_option(t["ticker"], t["signal"], t["price"])
            option_cache[key] = opt

    # Print detailed trades
    print(f"\n{'='*120}")
    print(f"  {'#':<4} {'Ticker':<6} {'Type':<5} {'Strike':<9} {'Expiry':<12} "
          f"{'Premium':<9} {'Buy @':<18} {'Sell @':<18} {'P&L':<9} {'Exit Reason'}")
    print(f"  {'─'*4} {'─'*6} {'─'*5} {'─'*9} {'─'*12} "
          f"{'─'*9} {'─'*18} {'─'*18} {'─'*9} {'─'*20}")

    total_pnl = 0.0
    winners = 0
    losers = 0
    trade_data = []  # for log file

    for i, t in enumerate(all_trades, 1):
        stock_pnl = ((t["exit_price"] - t["price"]) / t["price"]) * 100
        opt = option_cache.get(t["ticker"])

        if opt:
            opt_pnl = estimate_option_pnl(opt, stock_pnl)
            opt_pnl_str = f"{'+' if opt_pnl >= 0 else ''}{opt_pnl:.1f}%"
            strike_str = f"${opt.strike:.0f}"
            expiry_str = opt.expiry
            premium_str = f"${opt.premium:.2f}"
            type_str = opt.option_type
            contract = opt.contract_symbol
        else:
            opt_pnl = stock_pnl * 5
            opt_pnl_str = f"~{'+' if opt_pnl >= 0 else ''}{opt_pnl:.1f}%"
            strike_str = "N/A"
            expiry_str = "N/A"
            premium_str = "N/A"
            type_str = "CALL"
            contract = "N/A"

        total_pnl += opt_pnl
        if opt_pnl > 0:
            winners += 1
        else:
            losers += 1

        buy_str = f"${t['price']:.2f} {t['time'].strftime('%m/%d %H:%M')}"
        sell_str = f"${t['exit_price']:.2f} {t['exit_time'].strftime('%m/%d %H:%M')}"

        print(
            f"  {i:<4} {t['ticker']:<6} {type_str:<5} {strike_str:<9} {expiry_str:<12} "
            f"{premium_str:<9} {buy_str:<18} {sell_str:<18} {opt_pnl_str:<9} {t['exit_reason']}"
        )

        trade_data.append({
            "i": i, "ticker": t["ticker"], "type": type_str, "strike": strike_str,
            "expiry": expiry_str, "premium": premium_str, "contract": contract,
            "entry_price": t["price"], "exit_price": t["exit_price"],
            "entry_time": t["time"], "exit_time": t["exit_time"],
            "score": t["score"], "opt_pnl": opt_pnl, "exit_reason": t["exit_reason"],
        })

    # Summary
    total = len(all_trades)
    print(f"\n  {'='*120}")
    print(f"  SUMMARY")
    print(f"  {'='*120}")
    print(f"  Total trades:      {total}")
    print(f"  Winners:           {winners} | Losers: {losers}")
    print(f"  Win rate:          {winners / total * 100:.1f}%")
    print(f"  Options P&L (est): {'+' if total_pnl >= 0 else ''}{total_pnl:.1f}% total | "
          f"{'+' if total_pnl >= 0 else ''}{total_pnl / total:.1f}% avg per trade")
    print(f"  {'='*120}")
    print()

    # Write trade log file
    from datetime import datetime as dt
    log_path = "backtest.log"

    with open(log_path, "w") as f:
        f.write(f"OPTIONS SCALP BACKTEST LOG — {dt.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Settings: Buy={config.BUY_THRESHOLD} Sell={config.SELL_THRESHOLD} "
                f"TP=+{config.TAKE_PROFIT_PCT}% SL=-{config.TRAILING_STOP_PCT}% "
                f"SkipOpen={getattr(config, 'SKIP_OPEN_MINUTES', 0)}min "
                f"SkipClose={getattr(config, 'SKIP_CLOSE_MINUTES', 0)}min\n")
        f.write(f"{'='*140}\n\n")

        for td in trade_data:
            result = "WIN" if td["opt_pnl"] > 0 else "LOSS"
            pnl_str = f"{'+' if td['opt_pnl'] >= 0 else ''}{td['opt_pnl']:.1f}%"

            f.write(f"Trade #{td['i']} [{result}] {pnl_str}\n")
            f.write(f"  BUY  {td['ticker']} {td['type']} {td['strike']} exp {td['expiry']} "
                    f"@ {td['premium']}  |  {td['entry_time'].strftime('%Y-%m-%d %H:%M')}  |  "
                    f"Stock ${td['entry_price']:.2f}  |  Score: {td['score']:+.3f}\n")
            f.write(f"  SELL {td['ticker']} {td['type']} {td['strike']}  |  "
                    f"{td['exit_time'].strftime('%Y-%m-%d %H:%M')}  |  "
                    f"Stock ${td['exit_price']:.2f}  |  {td['exit_reason']}\n")
            f.write(f"  Contract: {td['contract']}\n")
            f.write(f"  Option P&L: {pnl_str}\n\n")

        f.write(f"{'='*140}\n")
        f.write(f"SUMMARY\n")
        f.write(f"{'='*140}\n")
        f.write(f"Total trades:    {total}\n")
        f.write(f"Winners:         {winners} | Losers: {losers}\n")
        f.write(f"Win rate:        {winners / total * 100:.1f}%\n")
        f.write(f"Options P&L:     {'+' if total_pnl >= 0 else ''}{total_pnl:.1f}% total | "
                f"{'+' if total_pnl >= 0 else ''}{total_pnl / total:.1f}% avg per trade\n")

    print(f"  Trade log saved to: {log_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Options backtest")
    parser.add_argument("--hours", type=int, default=24, help="Lookback period in hours (default: 24)")
    args = parser.parse_args()
    show_all_trades(lookback_hours=args.hours)
