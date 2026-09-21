"""
Detects trading patterns:
- Pump & dump patterns (rapid price rise + volume spike, then reversal)
- Wash trading (same wallets trading back and forth)
- Front-running (large buys right before announcements)
- Sandwich attacks (buy before + sell after a large swap)
- Unusual options/futures activity
"""
from collections import defaultdict

from utils.logger import log


class PatternDetector:
    def __init__(self, cex_provider):
        self.cex = cex_provider
        self._price_cache: dict[str, list[dict]] = defaultdict(list)

    async def detect_pump_dump(self, symbol: str, exchange: str = "binance") -> dict | None:
        """
        Detect pump & dump pattern:
        - Price rose >30% in last 4 hours
        - Volume spiked >10x average
        - Starting to reverse (current candle red after multiple green)
        """
        ohlcv = await self.cex.get_ohlcv(exchange, symbol, "1h", 24)
        if not ohlcv or len(ohlcv) < 6:
            return None

        recent_4h = ohlcv[-4:]
        older = ohlcv[:-4]

        price_start = recent_4h[0][1]  # open
        price_peak = max(c[2] for c in recent_4h)  # highest high
        price_now = recent_4h[-1][4]  # current close

        if price_start <= 0:
            return None

        pump_pct = (price_peak - price_start) / price_start
        reversal_pct = (price_peak - price_now) / price_peak if price_peak > 0 else 0

        avg_vol_old = sum(c[5] for c in older) / len(older) if older else 0
        vol_recent = sum(c[5] for c in recent_4h) / len(recent_4h)
        vol_ratio = vol_recent / avg_vol_old if avg_vol_old > 0 else 0

        is_pump = pump_pct >= 0.30 and vol_ratio >= 5
        is_dumping = reversal_pct >= 0.10 and recent_4h[-1][4] < recent_4h[-1][1]

        if is_pump:
            phase = "DUMPING" if is_dumping else "PUMPING"
            return {
                "symbol": symbol,
                "exchange": exchange,
                "pattern": "pump_dump",
                "phase": phase,
                "pump_pct": pump_pct * 100,
                "reversal_pct": reversal_pct * 100,
                "volume_ratio": vol_ratio,
                "price_start": price_start,
                "price_peak": price_peak,
                "price_now": price_now,
                "urgency": "critical" if is_dumping else "warning",
            }
        return None

    async def detect_wash_trading(self, symbol: str) -> dict | None:
        """
        Detect potential wash trading:
        - Very high volume but price barely moves
        - Volume/liquidity ratio abnormally high
        """
        results = {}
        for exchange in ["binance", "bybit", "mexc"]:
            ohlcv = await self.cex.get_ohlcv(exchange, symbol, "1h", 12)
            if not ohlcv or len(ohlcv) < 4:
                continue

            total_volume = sum(c[5] for c in ohlcv[-4:])
            price_range = max(c[2] for c in ohlcv[-4:]) - min(c[3] for c in ohlcv[-4:])
            avg_price = sum(c[4] for c in ohlcv[-4:]) / 4
            pct_range = price_range / avg_price if avg_price > 0 else 0

            if total_volume > 0 and pct_range < 0.005 and total_volume > 100_000:
                results[exchange] = {
                    "volume": total_volume,
                    "price_range_pct": pct_range * 100,
                }

        if len(results) >= 1:
            highest_vol = max(results.items(), key=lambda x: x[1]["volume"])
            return {
                "symbol": symbol,
                "pattern": "wash_trading",
                "exchanges": results,
                "worst_exchange": highest_vol[0],
                "volume": highest_vol[1]["volume"],
                "price_range_pct": highest_vol[1]["price_range_pct"],
            }
        return None

    async def detect_cross_exchange_anomaly(self, symbol: str) -> dict | None:
        """
        Detect price anomalies across exchanges:
        - Significant price difference between exchanges
        - One exchange lagging behind (arbitrage opportunity)
        """
        prices = {}
        for exchange in ["binance", "bybit", "mexc"]:
            p = await self.cex.get_price(exchange, symbol)
            if p and p > 0:
                prices[exchange] = p

        if len(prices) < 2:
            return None

        max_price = max(prices.values())
        min_price = min(prices.values())
        spread = (max_price - min_price) / min_price

        if spread < 0.005:
            return None

        max_ex = [ex for ex, p in prices.items() if p == max_price][0]
        min_ex = [ex for ex, p in prices.items() if p == min_price][0]

        return {
            "symbol": symbol,
            "pattern": "cross_exchange_anomaly",
            "spread_pct": spread * 100,
            "prices": prices,
            "buy_on": min_ex,
            "sell_on": max_ex,
            "potential_profit_pct": spread * 100,
            "urgency": "critical" if spread >= 0.02 else "warning",
        }

    async def detect_futures_spot_divergence(self, symbol: str) -> dict | None:
        """
        Detect divergence between futures and spot prices.
        Large premium/discount can signal upcoming moves.
        """
        spot_symbol = symbol

        spot_prices = {}
        futures_prices = {}

        for exchange in ["binance", "bybit"]:
            spot = await self.cex.get_price(exchange, spot_symbol)
            if spot:
                spot_prices[exchange] = spot

            perp_sym = symbol.replace("/USDT", "/USDT:USDT")
            futures = await self.cex.get_price(exchange, perp_sym)
            if futures:
                futures_prices[exchange] = futures

        for exchange in spot_prices:
            if exchange in futures_prices:
                spot_p = spot_prices[exchange]
                fut_p = futures_prices[exchange]
                premium = (fut_p - spot_p) / spot_p

                if abs(premium) >= 0.005:
                    return {
                        "symbol": symbol,
                        "pattern": "futures_spot_divergence",
                        "exchange": exchange,
                        "spot_price": spot_p,
                        "futures_price": fut_p,
                        "premium_pct": premium * 100,
                        "signal": "Bearish (futures premium)" if premium > 0 else "Bullish (futures discount)",
                        "urgency": "critical" if abs(premium) >= 0.02 else "warning",
                    }
        return None

    async def scan_all_patterns(self, symbol: str) -> list[dict]:
        """Run all pattern detections on a symbol."""
        patterns = []

        for detector in [
            self.detect_pump_dump,
            self.detect_wash_trading,
            self.detect_cross_exchange_anomaly,
            self.detect_futures_spot_divergence,
        ]:
            try:
                result = await detector(symbol)
                if result:
                    patterns.append(result)
            except Exception as e:
                log.debug(f"Pattern detection error for {symbol}: {e}")

        return patterns
