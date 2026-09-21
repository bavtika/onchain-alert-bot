import config
from strategies.base import BaseStrategy
from utils.logger import log


class VolumeAnomalyStrategy(BaseStrategy):
    name = "volume_anomaly"

    def __init__(self, providers, notifier, db_module):
        super().__init__(providers, notifier, db_module)
        self._top_symbols_cache: list[str] | None = None
        self._cache_refresh = 0

    async def _discover_top_symbols(self, cex, limit: int = 80) -> list[str]:
        """Find top spot symbols by 24h volume across all exchanges."""
        vol_map: dict[str, float] = {}

        for exchange in config.CEXES:
            tickers = await cex.get_all_tickers_spot(exchange)
            for sym, ticker in tickers.items():
                if "/USDT" not in sym:
                    continue
                vol = float(ticker.get("quoteVolume") or 0)
                if vol > 0:
                    base = sym.split("/")[0]
                    if base in vol_map:
                        vol_map[base] = max(vol_map[base], vol)
                    else:
                        vol_map[base] = vol

        sorted_symbols = sorted(vol_map.items(), key=lambda x: -x[1])
        return [f"{base}/USDT" for base, _ in sorted_symbols[:limit]]

    async def run(self) -> None:
        cex = self.providers["cex"]
        import time

        if self._top_symbols_cache is None or time.time() - self._cache_refresh > 3600:
            self._top_symbols_cache = await self._discover_top_symbols(cex)
            self._cache_refresh = time.time()
            log.info(f"[volume_anomaly] Discovered {len(self._top_symbols_cache)} symbols")

        scanned = 0

        for symbol in self._top_symbols_cache:
            for exchange in config.CEXES:
                ohlcv = await cex.get_ohlcv(exchange, symbol, "1h", 25)
                if not ohlcv or len(ohlcv) < 3:
                    continue

                latest = ohlcv[-1]
                history = ohlcv[:-1]

                current_volume = latest[5]
                current_close = latest[4]
                current_open = latest[1]

                avg_volume = sum(c[5] for c in history) / len(history) if history else 0
                if avg_volume == 0:
                    continue

                volume_ratio = current_volume / avg_volume
                price_change = (current_close - current_open) / current_open if current_open else 0

                await self.db.insert_price_point(symbol, exchange, current_close, current_volume)

                if volume_ratio >= config.VOLUME_SPIKE_MULTIPLIER:
                    key = f"{symbol}_{exchange}"
                    if not await self.db.should_alert(self.name, key):
                        continue

                    is_price_spike = abs(price_change) >= config.PRICE_SPIKE_THRESHOLD
                    urgency = "critical" if is_price_spike else "warning"
                    price_dir = "UP" if price_change > 0 else "DOWN"

                    body = (
                        f"Symbol: {symbol}\n"
                        f"Exchange: {exchange.upper()}\n"
                        f"Volume: {current_volume:,.0f} ({volume_ratio:.1f}x avg)\n"
                        f"Avg 24h Volume: {avg_volume:,.0f}\n"
                        f"Price: {current_close} ({price_change*100:+.2f}%)\n"
                        f"Direction: {price_dir}"
                    )

                    title = f"Volume Spike: {symbol}"
                    if is_price_spike:
                        title = f"VOLUME + PRICE SPIKE: {symbol}"

                    await self.notifier.send_alert(self.name, title, body, urgency)
                    await self.db.record_alert(self.name, key, body)

            scanned += 1

        log.info(f"[volume_anomaly] Scanned {scanned} tokens")
