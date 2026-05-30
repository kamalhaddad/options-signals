import asyncio
import logging
from datetime import datetime, time as dtime

import discord
from discord.ext import commands, tasks

import config
from signals import scan_watchlist, analyze_ticker, SignalResult
from options import pick_option, OptionPick

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


def format_signal_embed(result: SignalResult, option: OptionPick | None = None) -> discord.Embed:
    """Format a signal result with option contract recommendation."""
    is_buy = result.signal == "BUY"
    color = discord.Color.green() if is_buy else discord.Color.red()
    emoji = "\U0001f7e2" if is_buy else "\U0001f534"
    opt_type = "CALL" if is_buy else "PUT"

    embed = discord.Embed(
        title=f"{emoji} {result.signal} Signal: {result.ticker} \u2192 Buy {opt_type}",
        color=color,
        timestamp=datetime.now(),
    )

    embed.add_field(name="Stock Price", value=f"${result.price}", inline=True)
    embed.add_field(name="Signal Score", value=f"{result.score:+.3f}", inline=True)
    embed.add_field(name="Strength", value=get_strength(result.score), inline=True)

    # Option contract recommendation
    if option:
        contract_text = (
            f"**Contract:** `{option.contract_symbol}`\n"
            f"**Type:** {option.option_type}\n"
            f"**Strike:** ${option.strike:.2f} ({option.otm_pct:+.1f}% OTM)\n"
            f"**Expiry:** {option.expiry} ({option.days_to_expiry}d)\n"
            f"**Premium:** ${option.premium:.2f}\n"
            f"**Bid/Ask:** ${option.bid:.2f} / ${option.ask:.2f} (spread: {option.spread_pct:.1f}%)\n"
            f"**Delta:** ~{option.delta}\n"
            f"**IV:** {option.implied_vol:.0f}%" if option.implied_vol else ""
        )
        embed.add_field(name=f"\U0001f4b0 Recommended {opt_type}", value=contract_text, inline=False)

        # Targets
        tp_pct = config.TAKE_PROFIT_PCT
        sl_pct = config.TRAILING_STOP_PCT
        if option.delta and option.premium > 0:
            tp_stock = result.price * (1 + tp_pct / 100) if is_buy else result.price * (1 - tp_pct / 100)
            sl_stock = result.price * (1 - sl_pct / 100) if is_buy else result.price * (1 + sl_pct / 100)
            tp_option = option.premium + (result.price * tp_pct / 100 * option.delta)
            sl_option = max(0.01, option.premium - (result.price * sl_pct / 100 * option.delta))

            targets_text = (
                f"**Take Profit:** stock ${tp_stock:.2f} \u2192 option ~${tp_option:.2f} "
                f"(+{((tp_option - option.premium) / option.premium * 100):.0f}%)\n"
                f"**Stop Loss:** stock ${sl_stock:.2f} \u2192 option ~${sl_option:.2f} "
                f"({((sl_option - option.premium) / option.premium * 100):.0f}%)"
            )
            embed.add_field(name="\U0001f3af Targets", value=targets_text, inline=False)
    else:
        embed.add_field(
            name=f"\U0001f4b0 {opt_type} Option",
            value="No suitable contract found — low liquidity or no expiries available",
            inline=False,
        )

    # Indicator breakdown
    details = result.details
    scores = details["indicator_scores"]
    indicators_text = (
        f"RSI: {details['rsi']:.0f} ({scores['rsi']:+.1f}) | "
        f"MACD: {scores['macd']:+.1f} | "
        f"EMA: {details['ema_trend']} ({scores['ema_cross']:+.1f}) | "
        f"BB: {details['bb_position']:.0f}% ({scores['bollinger']:+.1f}) | "
        f"StochRSI: {details['stoch_rsi']:.0f} ({scores['stoch_rsi']:+.1f}) | "
        f"Vol: {details['volume_ratio']}x ({scores['volume']:+.1f})"
    )
    embed.add_field(name="Indicators", value=f"`{indicators_text}`", inline=False)

    embed.set_footer(text="Not financial advice \u2022 Always manage your risk")

    return embed


def get_strength(score: float) -> str:
    magnitude = abs(score)
    if magnitude >= 0.7:
        return "\u2b50 STRONG"
    elif magnitude >= 0.5:
        return "\u26a1 MODERATE"
    return "\u2022 WEAK"


def is_market_hours() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    market_open = dtime(9, 30)
    market_close = dtime(16, 0)
    return market_open <= now.time() <= market_close


def analyze_with_option(ticker: str) -> tuple[SignalResult, OptionPick | None] | None:
    """Analyze a ticker and pick an option contract if signal fires."""
    result = analyze_ticker(ticker)
    if result is None:
        return None
    option = None
    if result.signal != "NEUTRAL":
        option = pick_option(result.ticker, result.signal, result.price)
    return result, option


def scan_with_options() -> list[tuple[SignalResult, OptionPick | None]]:
    """Scan watchlist and attach option picks to each signal."""
    results = []
    for ticker in config.WATCHLIST:
        data = analyze_with_option(ticker)
        if data and data[0].signal != "NEUTRAL":
            results.append(data)
    return results


@bot.event
async def on_ready():
    log.info(f"Bot connected as {bot.user} (ID: {bot.user.id})")
    log.info(f"Watching: {', '.join(config.WATCHLIST)}")
    log.info(f"Scan interval: {config.SCAN_INTERVAL_MINUTES} minutes")
    if not scanner_loop.is_running():
        scanner_loop.start()


@tasks.loop(minutes=config.SCAN_INTERVAL_MINUTES)
async def scanner_loop():
    """Periodic scanner that posts signals with option recommendations."""
    channel = bot.get_channel(config.CHANNEL_ID)
    if not channel:
        log.error(f"Channel {config.CHANNEL_ID} not found")
        return

    if not is_market_hours():
        log.info("Market closed - skipping scan")
        return

    log.info("Running watchlist scan...")
    results = await asyncio.to_thread(scan_with_options)

    if not results:
        log.info("No actionable signals found")
        return

    log.info(f"Found {len(results)} signal(s)")
    for result, option in results:
        embed = format_signal_embed(result, option)
        await channel.send(embed=embed)
        await asyncio.sleep(1)


@scanner_loop.before_loop
async def before_scanner():
    await bot.wait_until_ready()


@bot.command(name="scan")
async def manual_scan(ctx):
    """Manually trigger a watchlist scan."""
    await ctx.send("\U0001f50d Scanning watchlist for options signals...")
    results = await asyncio.to_thread(scan_with_options)

    if not results:
        await ctx.send("\u2705 No actionable signals right now.")
        return

    for result, option in results:
        embed = format_signal_embed(result, option)
        await ctx.send(embed=embed)


@bot.command(name="check")
async def check_ticker(ctx, ticker: str):
    """Check a specific ticker for signals. Usage: !check AAPL"""
    ticker = ticker.upper()
    await ctx.send(f"\U0001f50d Analyzing **{ticker}**...")
    data = await asyncio.to_thread(analyze_with_option, ticker)

    if data is None:
        await ctx.send(f"\u274c Could not fetch data for **{ticker}**")
        return

    result, option = data

    if result.signal != "NEUTRAL":
        embed = format_signal_embed(result, option)
        await ctx.send(embed=embed)
    else:
        await ctx.send(
            f"\u26aa **{ticker}** @ ${result.price} \u2014 No signal (score: {result.score:+.3f})"
        )


@bot.command(name="watchlist")
async def show_watchlist(ctx):
    tickers = ", ".join(config.WATCHLIST)
    await ctx.send(f"\U0001f4cb **Watchlist ({len(config.WATCHLIST)} tickers):** {tickers}")


@bot.command(name="status")
async def show_status(ctx):
    embed = discord.Embed(title="\U0001f916 Options Signal Bot", color=discord.Color.blue())
    embed.add_field(name="Scan Interval", value=f"{config.SCAN_INTERVAL_MINUTES} min", inline=True)
    embed.add_field(name="Take Profit", value=f"+{config.TAKE_PROFIT_PCT}%", inline=True)
    embed.add_field(name="Stop Loss", value=f"-{config.TRAILING_STOP_PCT}%", inline=True)
    embed.add_field(name="Buy Threshold", value=f"{config.BUY_THRESHOLD}", inline=True)
    embed.add_field(name="Sell Threshold", value=f"{config.SELL_THRESHOLD}", inline=True)
    embed.add_field(name="Min Bullish", value=f"{config.MIN_BULLISH_INDICATORS}/6", inline=True)
    embed.add_field(name="Watchlist", value=f"{len(config.WATCHLIST)} tickers", inline=True)
    embed.add_field(name="Market Open", value="\u2705 Yes" if is_market_hours() else "\u274c No", inline=True)
    await ctx.send(embed=embed)


if __name__ == "__main__":
    if not config.DISCORD_TOKEN:
        log.error("DISCORD_TOKEN not set in .env file")
        exit(1)
    if config.CHANNEL_ID == 0:
        log.error("DISCORD_CHANNEL_ID not set in .env file")
        exit(1)

    bot.run(config.DISCORD_TOKEN)
