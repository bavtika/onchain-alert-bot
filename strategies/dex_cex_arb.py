"""
DEX/CEX Arbitrage — auto-discovery mode.
1. Pulls trending pools from GeckoTerminal/DEXScreener (already have scanner)
2. For each token, checks if it trades on any CEX (Binance/Bybit/MEXC)
3. Compares DEX price vs CEX price
4. Alerts when spread > threshold

No manual watchlist needed — fully automatic.
"""
import config
from strategies.base import BaseStrategy
from utils.logger import log


class DexCexArbStrategy(BaseStrategy):
    name = "dex_cex_arb"

    def __init__(self, providers, notifier, db_module, scanner=None):
        super().__init__(providers, notifier, db_module)
        self.scanner = scanner
        self._cex_symbols_cache: set[str] | None = None

    async def run(self) -> None:
        cex = self.providers["cex"]

        if self._cex_symbols_cache is None:
            self._cex_symbols_cache = set()
            for exchange in config.CEXES:
                symbols = await cex.get_all_spot_symbols(exchange)
                for s in symbols:
                    base = s.split("/")[0]
                    self._cex_symbols_cache.add(base.upper())

        pairs_scanned = 0

        # Static watchlist (manual pairs with exact DEX contract)
        for token in config.WATCHLIST:
            await self._check_manual_pair(cex, token)
            pairs_scanned += 1

        # Auto-discovery: trending DEX pools that also exist on CEX
        if self.scanner:
            auto_pairs = await self._discover_arb_pairs()
            for pair in auto_pairs:
                await self._check_auto_pair(cex, pair)
                pairs_scanned += 1

        log.info(f"[dex_cex_arb] Scanned {pairs_scanned} pairs")

    async def _discover_arb_pairs(self) -> list[dict]:
        """Find trending DEX tokens that also trade on a CEX."""
        pairs = []

        for chain in ["ethereum", "bsc", "arbitrum", "solana"]:
            try:
                trending = await self.scanner.gecko_trending_pools(chain)
            except Exception:
                continue

            for pool in trending:
                attrs = pool.get("attributes", {})
                name = attrs.get("name", "")
                address = attrs.get("address", "")
                price_usd = self.scanner._safe_float(attrs.get("base_token_price_usd"))
                liq = self.scanner._safe_float(attrs.get("reserve_in_usd"))

                if liq < 50_000 or price_usd <= 0:
                    continue

                symbol_parts = name.split(" / ") if " / " in name else name.split("/")
                symbol = symbol_parts[0].strip().upper() if symbol_parts else ""

                if not symbol or symbol not in self._cex_symbols_cache:
                    continue

                pairs.append({
                    "symbol": symbol,
                    "chain": chain,
                    "dex_price_usd": price_usd,
                    "liquidity": liq,
                    "pool_address": address,
                })

        return pairs

    async def _check_auto_pair(self, cex, pair: dict) -> None:
        symbol = pair["symbol"]
        chain = pair["chain"]
        dex_price = pair["dex_price_usd"]
        cex_symbol = f"{symbol}/USDT"

        best_cex_price, best_exchange = await self._best_cex_price(cex, cex_symbol)
        if best_cex_price is None:
            return

        spread = abs(dex_price - best_cex_price) / min(dex_price, best_cex_price)
        if spread < config.ARB_SPREAD_THRESHOLD:
            return

        key = f"auto_{symbol}_{chain}"
        if not await self.db.should_alert(self.name, key):
            return

        if dex_price < best_cex_price:
            direction = f"Buy DEX ({chain}) -> Sell CEX ({best_exchange})"
        else:
            direction = f"Buy CEX ({best_exchange}) -> Sell DEX ({chain})"

        urgency = "critical" if spread >= 0.10 else "warning"

        body = (
            f"Token: {symbol}\n"
            f"DEX Price ({chain}): ${dex_price:.10g}\n"
            f"CEX Price ({best_exchange}): ${best_cex_price:.10g}\n"
            f"Spread: {spread*100:.2f}%\n"
            f"DEX Liquidity: ${pair['liquidity']:,.0f}\n"
            f"Direction: {direction}\n"
            f"Pool: {pair['pool_address'][:20]}...\n"
            f"Note: Check gas + slippage before executing"
        )

        await self.notifier.send_alert(self.name, f"DEX/CEX ARB: {symbol} {spread*100:.1f}%", body, urgency)
        await self.db.record_alert(self.name, key, body)

    async def _check_manual_pair(self, cex, token: dict) -> None:
        evm = self.providers["evm"]
        symbol = token["symbol"]
        chain = token["chain"]
        cex_symbol = token["cex_symbol"]
        pair_address = token["dex_pair"]
        token_index = token.get("token_index", 0)

        dex_price = await evm.get_dex_price(chain, pair_address, token_index)
        if dex_price is None or dex_price <= 0:
            return

        native_symbol = {"ethereum": "ETH/USDT", "bsc": "BNB/USDT", "arbitrum": "ETH/USDT"}.get(chain)
        if native_symbol:
            native_price = await cex.get_price("binance", native_symbol)
            if native_price:
                dex_price_usd = dex_price * native_price
            else:
                return
        else:
            return

        best_cex_price, best_exchange = await self._best_cex_price(cex, cex_symbol)
        if best_cex_price is None:
            return

        spread = abs(dex_price_usd - best_cex_price) / min(dex_price_usd, best_cex_price)
        if spread < config.ARB_SPREAD_THRESHOLD:
            return

        if not await self.db.should_alert(self.name, symbol):
            return

        if dex_price_usd < best_cex_price:
            direction = f"Buy DEX ({chain}) -> Sell CEX ({best_exchange})"
        else:
            direction = f"Buy CEX ({best_exchange}) -> Sell DEX ({chain})"

        urgency = "critical" if spread >= 0.10 else "warning"

        body = (
            f"Token: {symbol}\n"
            f"DEX Price ({chain}): ${dex_price_usd:.10g}\n"
            f"CEX Price ({best_exchange}): ${best_cex_price:.10g}\n"
            f"Spread: {spread*100:.2f}%\n"
            f"Direction: {direction}\n"
            f"Note: Check gas + slippage before executing"
        )

        await self.notifier.send_alert(self.name, f"DEX/CEX ARB: {symbol} {spread*100:.1f}%", body, urgency)
        await self.db.record_alert(self.name, symbol, body)

    async def _best_cex_price(self, cex, cex_symbol: str) -> tuple[float | None, str | None]:
        best_price = None
        best_ex = None
        for exchange in config.CEXES:
            p = await cex.get_price(exchange, cex_symbol)
            if p and p > 0:
                if best_price is None or p > best_price:
                    best_price = p
                    best_ex = exchange
        return best_price, best_ex
