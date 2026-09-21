"""
Correlation Divergence — auto-discovery mode.
Dynamically groups tokens by sector/category and detects divergences.
When normally correlated assets diverge, one is mispriced — mean reversion trade.
"""
import config
from strategies.base import BaseStrategy
from utils.logger import log

SECTOR_KEYWORDS = {
    "L1": ["BTC", "ETH", "SOL", "BNB", "AVAX", "ADA", "DOT", "NEAR", "SUI", "APT", "SEI", "TIA"],
    "L2": ["ARB", "OP", "MATIC", "STRK", "ZK", "MANTA", "METIS", "BLAST"],
    "MEME": ["DOGE", "SHIB", "PEPE", "FLOKI", "BONK", "WIF", "MEME", "TURBO", "BRETT"],
    "DEFI": ["UNI", "AAVE", "LINK", "MKR", "CRV", "COMP", "SNX", "SUSHI", "DYDX"],
    "AI": ["FET", "RNDR", "AGIX", "TAO", "WLD", "ARKM"],
    "GAMING": ["AXS", "SAND", "MANA", "IMX", "GALA", "ILV"],
}

DIVERGENCE_THRESHOLD = 6.0


class CorrelationDivergenceStrategy(BaseStrategy):
    name = "correlation_divergence"

    def __init__(self, providers, notifier, db_module):
        super().__init__(providers, notifier, db_module)
        self._pairs_cache: list[dict] | None = None
        self._cache_ts = 0

    async def _discover_correlated_pairs(self, cex) -> list[dict]:
        """Find pairs within the same sector that both exist on exchanges."""
        available_bases: set[str] = set()

        for exchange in config.CEXES:
            symbols = await cex.get_all_spot_symbols(exchange)
            for sym in symbols:
                base = sym.split("/")[0]
                available_bases.add(base)

        pairs = []
        for sector, tokens in SECTOR_KEYWORDS.items():
            active = [t for t in tokens if t in available_bases]
            for i in range(len(active)):
                for j in range(i + 1, len(active)):
                    a, b = active[i], active[j]
                    threshold = DIVERGENCE_THRESHOLD
                    if sector == "MEME":
                        threshold = 10.0
                    elif sector in ("L1", "DEFI"):
                        threshold = 5.0

                    pairs.append({
                        "a": f"{a}/USDT",
                        "b": f"{b}/USDT",
                        "name": f"{a}/{b} ({sector})",
                        "threshold": threshold,
                    })

        return pairs

    async def run(self) -> None:
        cex = self.providers["cex"]
        import time

        if self._pairs_cache is None or time.time() - self._cache_ts > 3600:
            self._pairs_cache = await self._discover_correlated_pairs(cex)
            self._cache_ts = time.time()
            log.info(f"[correlation_divergence] Discovered {len(self._pairs_cache)} pairs")

        exchange = "binance"
        scanned = 0

        for pair_config in self._pairs_cache:
            await self._check_divergence(cex, exchange, pair_config)
            scanned += 1

        log.info(f"[correlation_divergence] Scanned {scanned} pairs")

    async def _check_divergence(self, cex, exchange: str, pair_config: dict) -> None:
        sym_a = pair_config["a"]
        sym_b = pair_config["b"]
        name = pair_config["name"]
        threshold = pair_config["threshold"]

        ohlcv_a = await cex.get_ohlcv(exchange, sym_a, "1h", 25)
        ohlcv_b = await cex.get_ohlcv(exchange, sym_b, "1h", 25)

        if not ohlcv_a or not ohlcv_b or len(ohlcv_a) < 6 or len(ohlcv_b) < 6:
            return

        min_len = min(len(ohlcv_a), len(ohlcv_b))
        ohlcv_a = ohlcv_a[-min_len:]
        ohlcv_b = ohlcv_b[-min_len:]

        ratios = []
        for ca, cb in zip(ohlcv_a, ohlcv_b):
            if cb[4] > 0:
                ratios.append(ca[4] / cb[4])

        if len(ratios) < 6:
            return

        current_ratio = ratios[-1]
        hist_ratios = ratios[:-1]
        avg_ratio = sum(hist_ratios) / len(hist_ratios)

        if avg_ratio <= 0:
            return

        deviation_pct = (current_ratio - avg_ratio) / avg_ratio * 100

        change_a_1h = (ohlcv_a[-1][4] - ohlcv_a[-2][4]) / ohlcv_a[-2][4] * 100 if ohlcv_a[-2][4] > 0 else 0
        change_b_1h = (ohlcv_b[-1][4] - ohlcv_b[-2][4]) / ohlcv_b[-2][4] * 100 if ohlcv_b[-2][4] > 0 else 0

        change_a_24h = (ohlcv_a[-1][4] - ohlcv_a[0][4]) / ohlcv_a[0][4] * 100 if ohlcv_a[0][4] > 0 else 0
        change_b_24h = (ohlcv_b[-1][4] - ohlcv_b[0][4]) / ohlcv_b[0][4] * 100 if ohlcv_b[0][4] > 0 else 0

        performance_diff = abs(change_a_24h - change_b_24h)

        if abs(deviation_pct) < threshold and performance_diff < threshold:
            return

        key = f"{name}_{exchange}"
        if not await self.db.should_alert(self.name, key, cooldown=1800):
            return

        if deviation_pct > 0:
            overperformer = sym_a
            underperformer = sym_b
        else:
            overperformer = sym_b
            underperformer = sym_a

        urgency = "critical" if abs(deviation_pct) >= threshold * 2 else "warning"

        body = (
            f"Pair: {name}\n"
            f"Exchange: {exchange.upper()}\n"
            f"\n{sym_a}:\n"
            f"  Price: ${ohlcv_a[-1][4]:.8g}\n"
            f"  1h: {change_a_1h:+.2f}%\n"
            f"  24h: {change_a_24h:+.2f}%\n"
            f"\n{sym_b}:\n"
            f"  Price: ${ohlcv_b[-1][4]:.8g}\n"
            f"  1h: {change_b_1h:+.2f}%\n"
            f"  24h: {change_b_24h:+.2f}%\n"
            f"\nRatio deviation: {deviation_pct:+.1f}% from 24h avg\n"
            f"Performance gap: {performance_diff:.1f}%\n"
            f"\nTrade: Long {underperformer.split('/')[0]}, Short {overperformer.split('/')[0]}\n"
            f"Risk: DELTA NEUTRAL (hedged, profits from mean reversion)\n"
            f"Expected reversion: ratio returns to {avg_ratio:.4f} (currently {current_ratio:.4f})"
        )

        await self.notifier.send_alert(
            self.name,
            f"DIVERGENCE: {name} {deviation_pct:+.1f}%",
            body, urgency,
        )
        await self.db.record_alert(self.name, key, body)
