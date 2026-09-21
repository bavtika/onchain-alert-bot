"""
New Listing Detection:
- Monitors when a token appears on one exchange but not others
- Detects tokens listed on DEX that are about to list on CEX
- Tracks Binance/Bybit/MEXC new pair additions
- First-mover advantage: tokens typically pump 30-200% on new CEX listing

Method:
- Periodically snapshot all trading pairs on each exchange
- Diff against previous snapshot to find new additions
- Cross-reference: if token exists on exchange A but just appeared on B, alert
- Check DEXScreener for tokens trending on DEX but not yet on any CEX
"""
import time

import config
from strategies.base import BaseStrategy
from utils.logger import log


class ListingSniperStrategy(BaseStrategy):
    name = "listing_sniper"

    def __init__(self, providers, notifier, db_module, scanner=None):
        super().__init__(providers, notifier, db_module)
        self.scanner = scanner
        self._exchange_pairs: dict[str, set[str]] = {}
        self._last_snapshot: dict[str, float] = {}
        self._known_new: set[str] = set()

    async def run(self) -> None:
        cex = self.providers["cex"]

        new_listings = await self._detect_new_cex_listings(cex)
        for listing in new_listings:
            await self._alert_new_listing(listing)

        exclusive = await self._detect_single_exchange_tokens(cex)
        for token in exclusive:
            await self._alert_exclusive(token)

        if self.scanner:
            await self._detect_dex_to_cex_candidates()

        log.info(f"[listing_sniper] Scan complete, tracking {sum(len(v) for v in self._exchange_pairs.values())} pairs")

    async def _detect_new_cex_listings(self, cex) -> list[dict]:
        new_listings = []

        for exchange in config.CEXES:
            try:
                spot_symbols = await cex.get_all_spot_symbols(exchange)
                current_pairs = set(spot_symbols)
            except Exception as e:
                log.debug(f"Load markets {exchange}: {e}")
                continue

            prev_pairs = self._exchange_pairs.get(exchange, set())

            if prev_pairs:
                brand_new = current_pairs - prev_pairs
                for pair in brand_new:
                    if pair in self._known_new:
                        continue
                    self._known_new.add(pair)
                    new_listings.append({
                        "symbol": pair,
                        "exchange": exchange,
                        "type": "new_spot_listing",
                        "on_other_exchanges": [
                            ex for ex in config.CEXES
                            if ex != exchange and pair in self._exchange_pairs.get(ex, set())
                        ],
                    })

            self._exchange_pairs[exchange] = current_pairs
            self._last_snapshot[exchange] = time.time()

        return new_listings

    async def _detect_single_exchange_tokens(self, cex) -> list[dict]:
        """Find tokens that exist on only one exchange — potential listing candidates for others."""
        if len(self._exchange_pairs) < 2:
            return []

        results = []
        all_exchanges = list(self._exchange_pairs.keys())

        for exchange in all_exchanges:
            exclusive = set(self._exchange_pairs[exchange])
            for other_ex in all_exchanges:
                if other_ex != exchange:
                    exclusive -= self._exchange_pairs[other_ex]

            for pair in list(exclusive)[:10]:
                ticker = await cex.get_ticker(exchange, pair)
                if not ticker:
                    continue

                vol = float(ticker.get("quoteVolume") or 0)
                if vol < 100_000:
                    continue

                results.append({
                    "symbol": pair,
                    "exclusive_to": exchange,
                    "volume_24h": vol,
                    "price": float(ticker.get("last") or 0),
                    "missing_from": [ex for ex in all_exchanges if ex != exchange],
                })

        results.sort(key=lambda x: -x["volume_24h"])
        return results[:3]

    async def _detect_dex_to_cex_candidates(self) -> None:
        """Find trending DEX tokens not yet on any CEX."""
        candidates = await self.scanner.discover_candidates()

        all_cex_bases = set()
        for pairs in self._exchange_pairs.values():
            for pair in pairs:
                base = pair.split("/")[0]
                all_cex_bases.add(base.upper())

        for token in candidates:
            symbol = token.get("symbol", "").upper()
            if symbol in all_cex_bases:
                continue

            vol = token.get("volume_24h", 0)
            liq = token.get("liquidity_usd", 0)
            if vol < 200_000 or liq < 100_000:
                continue

            key = f"dex_only_{symbol}"
            if not await self.db.should_alert(self.name, key, cooldown=86400):
                continue

            body = (
                f"Token: {symbol}\n"
                f"Chain: {token.get('chain', 'unknown')}\n"
                f"DEX Volume 24h: ${vol:,.0f}\n"
                f"DEX Liquidity: ${liq:,.0f}\n"
                f"24h Change: {token.get('price_change_24h', 0):+.1f}%\n"
                f"Source: {token.get('source', 'unknown')}\n"
                f"\nNOT on any tracked CEX (Binance/Bybit/MEXC)\n"
                f"If it gets listed, expect significant pump.\n"
                f"Token: {token.get('token_address', '')[:30]}..."
            )

            await self.notifier.send_alert(self.name, f"DEX ONLY: {symbol} (CEX listing candidate)", body, "info")
            await self.db.record_alert(self.name, key, body)

    async def _alert_new_listing(self, listing: dict) -> None:
        key = f"listing_{listing['symbol']}_{listing['exchange']}"
        if not await self.db.should_alert(self.name, key, cooldown=3600):
            return

        on_others = listing.get("on_other_exchanges", [])
        if on_others:
            context = f"Already on: {', '.join(ex.upper() for ex in on_others)}"
        else:
            context = "BRAND NEW — not on other tracked exchanges!"

        body = (
            f"Pair: {listing['symbol']}\n"
            f"Exchange: {listing['exchange'].upper()}\n"
            f"Type: New spot listing\n"
            f"{context}\n"
            f"\nNew listings typically pump 30-200% in first hours.\n"
            f"Check liquidity and spread before entry."
        )

        urgency = "critical" if not on_others else "warning"
        await self.notifier.send_alert(self.name, f"NEW LISTING: {listing['symbol']}", body, urgency)
        await self.db.record_alert(self.name, key, body)

    async def _alert_exclusive(self, token: dict) -> None:
        key = f"exclusive_{token['symbol']}"
        if not await self.db.should_alert(self.name, key, cooldown=86400):
            return

        body = (
            f"Pair: {token['symbol']}\n"
            f"Exclusive to: {token['exclusive_to'].upper()}\n"
            f"Volume 24h: ${token['volume_24h']:,.0f}\n"
            f"Price: ${token['price']:.8g}\n"
            f"Missing from: {', '.join(ex.upper() for ex in token['missing_from'])}\n"
            f"\nHigh volume + single exchange = listing candidate\n"
            f"Watch for listing announcements on other exchanges."
        )

        await self.notifier.send_alert(self.name, f"EXCLUSIVE: {token['symbol']}", body, "info")
        await self.db.record_alert(self.name, key, body)
