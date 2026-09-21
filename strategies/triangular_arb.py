"""
Triangular Arbitrage — auto-discovery mode.
Dynamically finds all valid triangles from exchange markets.
Exploits price inconsistencies between 3 pairs on the same exchange.
"""
import config
from strategies.base import BaseStrategy
from utils.logger import log


class TriangularArbStrategy(BaseStrategy):
    name = "triangular_arb"

    def __init__(self, providers, notifier, db_module):
        super().__init__(providers, notifier, db_module)
        self._triangles_cache: dict[str, list[dict]] = {}
        self._cache_ts = 0

    async def _discover_triangles(self, cex, exchange: str) -> list[dict]:
        """Find all valid triangular paths: USDT -> X -> Y -> USDT."""
        ex = cex._get_spot(exchange)
        await cex._ensure_markets(ex, f"{exchange}_spot")

        usdt_pairs = {}  # base -> symbol (e.g. "BTC" -> "BTC/USDT")
        cross_pairs = []  # pairs like ETH/BTC, SOL/ETH

        for sym in ex.symbols:
            market = ex.market(sym)
            if not market.get("spot") or not market.get("active", True):
                continue
            base = market["base"]
            quote = market["quote"]

            if quote == "USDT":
                usdt_pairs[base] = sym
            elif quote in ("BTC", "ETH", "BNB"):
                cross_pairs.append({"symbol": sym, "base": base, "quote": quote})

        triangles = []
        for cross in cross_pairs:
            base = cross["base"]
            quote = cross["quote"]

            if base not in usdt_pairs or quote not in usdt_pairs:
                continue

            triangles.append({
                "a": usdt_pairs[quote],      # e.g. BTC/USDT
                "b": cross["symbol"],          # e.g. ETH/BTC
                "c": usdt_pairs[base],         # e.g. ETH/USDT
            })

        return triangles

    async def run(self) -> None:
        cex = self.providers["cex"]
        import time

        if not self._triangles_cache or time.time() - self._cache_ts > 3600:
            for exchange in config.CEXES:
                tris = await self._discover_triangles(cex, exchange)
                self._triangles_cache[exchange] = tris
                log.info(f"[triangular_arb] Discovered {len(tris)} triangles on {exchange}")
            self._cache_ts = time.time()

        total = 0

        for exchange in config.CEXES:
            fee = self._get_fee(exchange)
            triangles = self._triangles_cache.get(exchange, [])

            for tri in triangles:
                await self._check_triangle(cex, exchange, tri, fee)
                total += 1

        log.info(f"[triangular_arb] Scanned {total} triangles")

    async def _check_triangle(self, cex, exchange: str, tri: dict, fee: float) -> None:
        pair_a, pair_b, pair_c = tri["a"], tri["b"], tri["c"]

        ticker_a = await cex.get_ticker(exchange, pair_a)
        ticker_b = await cex.get_ticker(exchange, pair_b)
        ticker_c = await cex.get_ticker(exchange, pair_c)

        if not all([ticker_a, ticker_b, ticker_c]):
            return

        ask_a = float(ticker_a.get("ask") or 0)
        bid_a = float(ticker_a.get("bid") or 0)
        ask_b = float(ticker_b.get("ask") or 0)
        bid_b = float(ticker_b.get("bid") or 0)
        ask_c = float(ticker_c.get("ask") or 0)
        bid_c = float(ticker_c.get("bid") or 0)

        if not all([ask_a, bid_a, ask_b, bid_b, ask_c, bid_c]):
            return

        # Forward: USDT -> buy A base -> buy B base via cross -> sell for USDT
        amount_a = (1.0 / ask_a) * (1 - fee)
        amount_b = (amount_a / ask_b) * (1 - fee)
        result_forward = amount_b * bid_c * (1 - fee)

        # Reverse: USDT -> buy C base -> sell via cross for A base -> sell for USDT
        amount_c = (1.0 / ask_c) * (1 - fee)
        amount_a_r = amount_c * bid_b * (1 - fee)
        result_reverse = amount_a_r * bid_a * (1 - fee)

        best = max(result_forward, result_reverse)
        if best <= 1.0:
            return

        profit = best - 1.0
        if profit < config.TRIANGULAR_ARB_MIN_PROFIT:
            return

        if profit > 0.05:
            log.debug(f"[triangular_arb] Skipping {pair_a}/{pair_b}/{pair_c} on {exchange}: {profit*100:.2f}% (likely stale)")
            return

        if best == result_forward:
            path = f"USDT -buy-> {pair_a.split('/')[0]} -buy-> {pair_c.split('/')[0]} -sell-> USDT"
        else:
            path = f"USDT -buy-> {pair_c.split('/')[0]} -sell-> {pair_a.split('/')[0]} -sell-> USDT"

        pairs_str = f"{pair_a}|{pair_b}|{pair_c}"
        key = f"{exchange}_{pairs_str}"
        if not await self.db.should_alert(self.name, key, cooldown=180):
            return

        urgency = "critical" if profit >= 0.005 else "warning"

        body = (
            f"Exchange: {exchange.upper()}\n"
            f"Pairs: {pairs_str}\n"
            f"Path: {path}\n"
            f"Profit: +{profit*100:.3f}%\n"
            f"Per $1000: +${profit * 1000:.2f}\n"
            f"Per $10000: +${profit * 10000:.2f}\n"
            f"\nPrices:\n"
            f"  {pair_a}: ask={ask_a:.8g} bid={bid_a:.8g}\n"
            f"  {pair_b}: ask={ask_b:.8g} bid={bid_b:.8g}\n"
            f"  {pair_c}: ask={ask_c:.8g} bid={bid_c:.8g}\n"
            f"\nRisk: NEAR ZERO (single exchange)\n"
            f"Note: Speed critical — use API"
        )

        await self.notifier.send_alert(
            self.name,
            f"TRIANGLE: {exchange.upper()} +{profit*100:.3f}%",
            body, urgency,
        )
        await self.db.record_alert(self.name, key, body)

    @staticmethod
    def _get_fee(exchange: str) -> float:
        return {"binance": 0.001, "bybit": 0.001, "mexc": 0.001}.get(exchange, 0.001)
