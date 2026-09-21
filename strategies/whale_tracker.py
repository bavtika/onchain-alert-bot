import config
from strategies.base import BaseStrategy
from utils.logger import log


class WhaleTrackerStrategy(BaseStrategy):
    name = "whale_tracker"

    async def run(self) -> None:
        cex = self.providers["cex"]
        evm = self.providers["evm"]
        solana = self.providers["solana"]

        native_prices = {}
        for sym in ["ETH/USDT", "BNB/USDT", "SOL/USDT"]:
            p = await cex.get_price("binance", sym)
            if p:
                native_prices[sym.split("/")[0]] = p

        for chain in ["ethereum", "bsc", "arbitrum"]:
            try:
                transfers = await evm.get_latest_block_transfers(chain)
            except Exception as e:
                log.error(f"[whale] EVM {chain}: {e}")
                continue

            native = {"ethereum": "ETH", "bsc": "BNB", "arbitrum": "ETH"}.get(chain, "ETH")
            price = native_prices.get(native, 0)

            for tx in transfers:
                usd_value = tx["value"] * price
                if usd_value < config.WHALE_THRESHOLD_USD:
                    continue

                if await self.db.is_tx_seen(tx["tx_hash"]):
                    continue

                await self.db.mark_tx_seen(tx["tx_hash"], chain)

                if not await self.db.should_alert(self.name, f"{chain}_{tx['exchange']}"):
                    continue

                emoji_dir = "-> Exchange" if tx["direction"] == "deposit" else "<- Exchange"
                signal = "SELL PRESSURE" if tx["direction"] == "deposit" else "ACCUMULATION"

                body = (
                    f"Chain: {chain.upper()}\n"
                    f"Amount: {tx['value']:,.2f} {tx['token']} (${usd_value:,.0f})\n"
                    f"Direction: {emoji_dir} ({tx['exchange']})\n"
                    f"Signal: {signal}\n"
                    f"TX: {tx['tx_hash'][:16]}..."
                )

                urgency = "critical" if usd_value >= config.WHALE_THRESHOLD_USD * 5 else "warning"
                await self.notifier.send_alert(self.name, f"Whale: ${usd_value/1e6:.1f}M {tx['token']}", body, urgency)
                await self.db.record_alert(self.name, f"{chain}_{tx['exchange']}", body)

        try:
            sol_transfers = await solana.get_large_transfers()
            sol_price = native_prices.get("SOL", 0)

            for tx in sol_transfers:
                usd_value = tx["value"] * sol_price
                if usd_value < config.WHALE_THRESHOLD_USD:
                    continue

                if await self.db.is_tx_seen(tx["tx_hash"]):
                    continue

                await self.db.mark_tx_seen(tx["tx_hash"], "solana")

                if not await self.db.should_alert(self.name, f"solana_{tx['exchange']}"):
                    continue

                emoji_dir = "-> Exchange" if tx["direction"] == "deposit" else "<- Exchange"
                signal = "SELL PRESSURE" if tx["direction"] == "deposit" else "ACCUMULATION"

                body = (
                    f"Chain: SOLANA\n"
                    f"Amount: {tx['value']:,.2f} SOL (${usd_value:,.0f})\n"
                    f"Direction: {emoji_dir} ({tx['exchange']})\n"
                    f"Signal: {signal}\n"
                    f"TX: {tx['tx_hash'][:16]}..."
                )

                urgency = "critical" if usd_value >= config.WHALE_THRESHOLD_USD * 5 else "warning"
                await self.notifier.send_alert(self.name, f"Whale: ${usd_value/1e6:.1f}M SOL", body, urgency)
                await self.db.record_alert(self.name, f"solana_{tx['exchange']}", body)

        except Exception as e:
            log.error(f"[whale] Solana: {e}")

        log.info("[whale_tracker] Scan complete")
