import asyncio
import logging
import signal
import sys
import warnings

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

import config
import db
import health
import notifier
from analyzers.pattern_detector import PatternDetector
from analyzers.risk_scorer import RiskScorer
from analyzers.smart_money import SmartMoneyTracker
from analyzers.wallet_analyzer import WalletAnalyzer
from providers.cex import CexProvider
from providers.evm import EvmProvider
from providers.solana import SolanaProvider
from scanners.token_scanner import TokenScanner
from strategies.auto_discovery import AutoDiscoveryStrategy
from strategies.correlation_divergence import CorrelationDivergenceStrategy
from strategies.dex_cex_arb import DexCexArbStrategy
from strategies.exploit_detector import ExploitDetectorStrategy
from strategies.liquidation_levels import LiquidationLevelsStrategy
from strategies.oi_spike import OISpikeStrategy
from strategies.smart_money_strategy import SmartMoneyStrategy
from strategies.stablecoin_depeg import StablecoinDepegStrategy
from strategies.triangular_arb import TriangularArbStrategy
from strategies.volume_anomaly import VolumeAnomalyStrategy
from strategies.whale_tracker import WhaleTrackerStrategy
from utils.logger import log

warnings.filterwarnings("ignore", message="Unclosed client session")
warnings.filterwarnings("ignore", category=ResourceWarning)
logging.getLogger("asyncio").setLevel(logging.WARNING)

STRATEGIES = []


# --- Telegram commands ---

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Onchain Alert Bot v2.0\n\n"
        "11 strategies active:\n\n"
        "Discovery:\n"
        "  1. Auto Token Discovery\n"
        "  2. Smart Money Tracker\n"
        "\nArbitrage (low risk):\n"
        "  3. Triangular Arbitrage\n"
        "  4. DEX/CEX Arbitrage\n"
        "  5. Stablecoin Depeg\n\n"
        "Futures:\n"
        "  6. Open Interest Spikes\n"
        "  7. Liquidation Levels\n"
        "  8. Correlation Divergence\n\n"
        "Security:\n"
        "  9. Exploit/Hack Detector\n\n"
        "Market Micro:\n"
        "  10. Volume/Price Spikes\n"
        "  11. Whale Tracking\n\n"
        "Analysis:\n"
        "- Risk scoring (honeypot, GoPlus, liquidity)\n"
        "- Wallet clustering & coordination detection\n"
        "- Pump&dump / wash trading patterns\n"
        "- Cross-exchange anomalies\n"
        "- Futures/spot divergence\n\n"
        "Commands:\n"
        "/status - Bot health\n"
        "/watchlist - Token watchlist\n"
        "/risk <chain> <address> - Check token risk\n"
        "/wallet <chain> <address> - Wallet profile"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = [
        "Bot Status: RUNNING",
        f"Strategies: {len(STRATEGIES)} active",
        "",
        "Intervals:",
    ]
    for name, interval in config.INTERVALS.items():
        lines.append(f"  {name}: every {interval}s")
    lines.append(f"\nExchanges: {', '.join(config.CEXES)}")
    lines.append("Chains: ETH, BSC, ARB, Solana")
    lines.append("\nFree APIs: DEXScreener, GeckoTerminal, GoPlus, Honeypot.is")
    await update.message.reply_text("\n".join(lines))


async def cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = ["DEX/CEX Watchlist:"]
    for t in config.WATCHLIST:
        lines.append(f"  {t['symbol']} ({t['chain']}) - {t['cex_symbol']}")
    lines.append("\nVolume Watchlist:")
    for s in config.VOLUME_WATCHLIST:
        lines.append(f"  {s}")
    lines.append("\nAuto-discovery scans ALL trending/new tokens automatically.")
    await update.message.reply_text("\n".join(lines))


async def cmd_risk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text("Usage: /risk <chain> <token_address>\nExample: /risk ethereum 0x6982508145454ce325ddbe47a25d4ec3d2311933")
        return
    chain = args[0].lower()
    token_address = args[1]

    risk_scorer = context.bot_data.get("risk_scorer")
    if not risk_scorer:
        await update.message.reply_text("Risk scorer not initialized.")
        return

    await update.message.reply_text(f"Scanning {token_address[:16]}... on {chain}")
    result = await risk_scorer.score_token(chain, token_address)

    lines = [
        f"Risk Score: {result['score']}/100 ({result['verdict']})",
        "",
    ]
    if result["risks"]:
        lines.append("Risks found:")
        for r in result["risks"]:
            lines.append(f"  - {r}")
    else:
        lines.append("No risks detected.")

    lines.append(f"\nDetails: {result['details']}")
    await update.message.reply_text("\n".join(lines))


async def cmd_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text("Usage: /wallet <chain> <address>")
        return
    chain = args[0].lower()
    address = args[1]

    wallet_analyzer = context.bot_data.get("wallet_analyzer")
    if not wallet_analyzer:
        await update.message.reply_text("Wallet analyzer not initialized.")
        return

    profile = await wallet_analyzer.get_wallet_profile(address, chain)

    lines = [
        f"Wallet: {address[:16]}...",
        f"Chain: {chain}",
        f"Activities: {profile.get('activity_count', 0)}",
        f"Buys: {profile.get('buy_count', 0)}",
        f"Sells: {profile.get('sell_count', 0)}",
        f"Total bought: ${profile.get('total_bought_usd', 0):,.0f}",
        f"Total sold: ${profile.get('total_sold_usd', 0):,.0f}",
        f"Est. PnL: ${profile.get('pnl_estimate', 0):,.0f}",
        f"Unique tokens: {profile.get('unique_tokens', 0)}",
    ]
    if profile.get("tokens"):
        lines.append(f"Tokens: {', '.join(profile['tokens'][:10])}")
    await update.message.reply_text("\n".join(lines))


# --- Strategy loop ---

async def strategy_loop(strategy, interval: int) -> None:
    await asyncio.sleep(2)
    while True:
        try:
            await strategy.run()
        except Exception as e:
            log.error(f"[{strategy.name}] Error: {e}")
        await asyncio.sleep(interval)


async def prune_loop() -> None:
    while True:
        await asyncio.sleep(1800)
        try:
            await db.prune_old_records()
            log.info("DB pruned")
        except Exception as e:
            log.error(f"Prune error: {e}")


async def health_check_loop() -> None:
    """Send a health ping every 6 hours."""
    while True:
        await asyncio.sleep(21600)
        await notifier.send_raw(f"Health check: {len(STRATEGIES)} strategies running.")


# --- Main ---

async def main() -> None:
    global STRATEGIES

    if not config.TELEGRAM_BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN not set. Create .env file (see .env.example)")
        sys.exit(1)
    if not config.TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_CHAT_ID not set. Create .env file (see .env.example)")
        sys.exit(1)

    # Init DB
    await db.get_db()
    log.info("Database initialized")

    # Init providers
    cex = CexProvider()
    evm = EvmProvider()
    solana = SolanaProvider()
    providers = {"cex": cex, "evm": evm, "solana": solana}

    # Init analyzers
    scanner = TokenScanner()
    risk_scorer = RiskScorer()
    wallet_analyzer = WalletAnalyzer(db)
    await wallet_analyzer.init_tables()
    pattern_detector = PatternDetector(cex)
    smart_money_tracker = SmartMoneyTracker(wallet_analyzer, cex, evm, solana)

    # Init strategies
    STRATEGIES = [
        # Discovery
        AutoDiscoveryStrategy(providers, notifier, db, scanner, risk_scorer, pattern_detector, wallet_analyzer),
        SmartMoneyStrategy(providers, notifier, db, smart_money_tracker, wallet_analyzer),
        # Arbitrage
        TriangularArbStrategy(providers, notifier, db),
        DexCexArbStrategy(providers, notifier, db),
        StablecoinDepegStrategy(providers, notifier, db),
        # Futures
        OISpikeStrategy(providers, notifier, db),
        LiquidationLevelsStrategy(providers, notifier, db),
        CorrelationDivergenceStrategy(providers, notifier, db),
        # Security
        ExploitDetectorStrategy(providers, notifier, db, scanner),
        # Market micro
        VolumeAnomalyStrategy(providers, notifier, db),
        WhaleTrackerStrategy(providers, notifier, db),
    ]

    # Telegram bot
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    app.bot_data["risk_scorer"] = risk_scorer
    app.bot_data["wallet_analyzer"] = wallet_analyzer

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("watchlist", cmd_watchlist))
    app.add_handler(CommandHandler("risk", cmd_risk))
    app.add_handler(CommandHandler("wallet", cmd_wallet))

    health_runner = await health.start_health_server(config.HEALTH_PORT)

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    log.info("Telegram bot started")

    await notifier.send_raw(
        "Bot v2.0 started!\n\n"
        f"Strategies: {len(STRATEGIES)}\n"
        "- Auto Token Discovery\n"
        "- Smart Money Tracker\n"
        "- Triangular/DEX-CEX Arbitrage\n"
        "- Stablecoin Depeg\n"
        "- Open Interest Spikes\n"
        "- Liquidation Levels\n"
        "- Correlation Divergence\n"
        "- Exploit Detector\n"
        "- Whale Tracking\n"
        "- Volume/Price Spikes\n\n"
        f"Exchanges: {', '.join(config.CEXES)}\n"
        "Chains: ETH, BSC, ARB, Solana\n\n"
        "Analysis: risk scoring, wallet clustering, pattern detection\n"
        "Free APIs: DEXScreener, GeckoTerminal, GoPlus, Honeypot.is"
    )

    # Start all strategy loops
    tasks = []
    for strategy in STRATEGIES:
        interval = config.INTERVALS.get(strategy.name, 60)
        t = asyncio.create_task(strategy_loop(strategy, interval))
        tasks.append(t)
        log.info(f"Started {strategy.name} (every {interval}s)")

    tasks.append(asyncio.create_task(prune_loop()))
    tasks.append(asyncio.create_task(health_check_loop()))
    health.set_ready(True, len(STRATEGIES))

    stop_event = asyncio.Event()

    def handle_signal(*_):
        log.info("Shutdown signal received")
        stop_event.set()

    if sys.platform != "win32":
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGINT, handle_signal)
        loop.add_signal_handler(signal.SIGTERM, handle_signal)

    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        pass

    log.info("Shutting down...")
    health.set_ready(False)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await app.updater.stop()
    await app.stop()
    await app.shutdown()
    await health_runner.cleanup()
    await cex.close()
    await solana.close()
    await scanner.close()
    await risk_scorer.close()
    await db.close()
    log.info("Bye")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    import warnings
    warnings.simplefilter("ignore", ResourceWarning)
    asyncio.run(main())
