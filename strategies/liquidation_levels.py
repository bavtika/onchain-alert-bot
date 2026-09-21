"""
Liquidation Level Detector — auto-discovery mode.
Scans top futures pairs by volume instead of hardcoded list.
Estimates where liquidation clusters sit based on OI, funding, and price action.
"""
import config
from strategies.base import BaseStrategy
from utils.logger import log

LEVERAGES = [5, 10, 20, 50, 100]


class LiquidationLevelsStrategy(BaseStrategy):
    name = "liquidation_levels"

    def __init__(self, providers, notifier, db_module):
        super().__init__(providers, notifier, db_module)
        self._tracked_cache: list[str] | None = None
        self._cache_ts = 0

    async def _discover_top_futures(self, cex, limit: int = 40) -> list[str]:
        """Find top futures symbols by volume across exchanges."""
        vol_map: dict[str, float] = {}

        for exchange in config.CEXES:
            tickers = await cex.get_all_tickers_swap(exchange)
            for sym, ticker in tickers.items():
                vol = float(ticker.get("quoteVolume") or 0)
                if vol > 0:
                    if sym in vol_map:
                        vol_map[sym] = max(vol_map[sym], vol)
                    else:
                        vol_map[sym] = vol

        sorted_syms = sorted(vol_map.items(), key=lambda x: -x[1])
        return [s[0] for s in sorted_syms[:limit]]

    async def run(self) -> None:
        cex = self.providers["cex"]
        import time

        if self._tracked_cache is None or time.time() - self._cache_ts > 3600:
            self._tracked_cache = await self._discover_top_futures(cex)
            self._cache_ts = time.time()
            log.info(f"[liquidation_levels] Discovered {len(self._tracked_cache)} futures symbols")

        alerts_sent = 0

        for symbol in self._tracked_cache:
            for exchange in config.CEXES:
                result = await self._analyze_liquidation_risk(cex, exchange, symbol)
                if result:
                    alerts_sent += 1

        log.info(f"[liquidation_levels] Scanned {len(self._tracked_cache)} symbols, {alerts_sent} alerts")

    async def _analyze_liquidation_risk(self, cex, exchange: str, symbol: str) -> bool:
        ticker = await cex.get_ticker(exchange, symbol)
        if not ticker:
            return False

        current_price = float(ticker.get("last") or 0)
        if current_price <= 0:
            return False

        funding_data = None
        try:
            ex = cex._get_swap(exchange)
            await cex._ensure_markets(ex, f"{exchange}_swap")
            if ex.has.get("fetchFundingRate"):
                funding_data = await ex.fetch_funding_rate(symbol)
        except Exception:
            pass

        funding_rate = 0
        if funding_data:
            funding_rate = float(funding_data.get("fundingRate") or 0)

        if funding_rate > 0.0005:
            dominant = "long"
            bias_strength = min(1.0, funding_rate / 0.002)
        elif funding_rate < -0.0005:
            dominant = "short"
            bias_strength = min(1.0, abs(funding_rate) / 0.002)
        else:
            return False

        ohlcv = await cex.get_ohlcv(exchange, symbol, "4h", 12)
        if not ohlcv or len(ohlcv) < 6:
            return False

        recent_high = max(c[2] for c in ohlcv[-6:])
        recent_low = min(c[3] for c in ohlcv[-6:])

        liq_levels = self._calculate_liquidation_bands(
            current_price, recent_high, recent_low, dominant
        )

        nearest = None
        nearest_distance = float("inf")
        for level in liq_levels:
            dist = abs(current_price - level["price"]) / current_price
            if dist < nearest_distance:
                nearest_distance = dist
                nearest = level

        if not nearest or nearest_distance > 0.05:
            return False

        clean = symbol.split(":")[0]
        key = f"{exchange}_{clean}_{dominant}"
        if not await self.db.should_alert(self.name, key, cooldown=1800):
            return False

        urgency = "critical" if nearest_distance < 0.02 else "warning"

        body = (
            f"Symbol: {clean}\n"
            f"Exchange: {exchange.upper()}\n"
            f"Current price: ${current_price:,.2f}\n"
            f"Funding rate: {funding_rate*100:.4f}%\n"
            f"Dominant side: {dominant.upper()} (bias: {bias_strength:.0%})\n"
            f"\nNearest liquidation band:\n"
            f"  Price: ${nearest['price']:,.2f}\n"
            f"  Distance: {nearest_distance*100:.1f}%\n"
            f"  Leverage: {nearest['leverage']}x\n"
            f"  Type: {nearest['type']} liquidation\n"
            f"\nAll liquidation levels:\n"
        )

        for level in sorted(liq_levels, key=lambda x: abs(current_price - x["price"])):
            dist = (level["price"] - current_price) / current_price * 100
            marker = " <-- CLOSE" if abs(dist) < 3 else ""
            body += f"  ${level['price']:,.2f} ({dist:+.1f}%) [{level['leverage']}x {level['type']}]{marker}\n"

        body += (
            f"\nSignal: If price reaches ${nearest['price']:,.2f}, expect "
            f"cascading {nearest['type']} liquidations. "
            f"Fade the liquidation move for low-risk entry."
        )

        await self.notifier.send_alert(
            self.name,
            f"LIQ ZONE: {clean} {nearest_distance*100:.1f}% away",
            body, urgency,
        )
        await self.db.record_alert(self.name, key, body)
        return True

    def _calculate_liquidation_bands(
        self, current_price: float, recent_high: float, recent_low: float, dominant: str
    ) -> list[dict]:
        levels = []

        for lev in LEVERAGES:
            maint_margin = 0.5 / lev

            if dominant == "long":
                entry_estimate = (current_price + recent_high) / 2
                liq_price = entry_estimate * (1 - (1 / lev) + maint_margin)
                levels.append({
                    "price": liq_price,
                    "leverage": lev,
                    "type": "LONG",
                    "entry_estimate": entry_estimate,
                })
            else:
                entry_estimate = (current_price + recent_low) / 2
                liq_price = entry_estimate * (1 + (1 / lev) - maint_margin)
                levels.append({
                    "price": liq_price,
                    "leverage": lev,
                    "type": "SHORT",
                    "entry_estimate": entry_estimate,
                })

        return levels
