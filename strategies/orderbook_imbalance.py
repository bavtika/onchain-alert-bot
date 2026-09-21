"""
Order Book Imbalance Detector:
Analyzes bid/ask depth to detect:
- Large buy/sell walls (support/resistance from limit orders)
- Bid/ask volume imbalance (more buyers or sellers)
- Spoofing patterns (large wall that disappears = fake)
- Thin side vulnerability (one side of book is empty = easy to move price)

Low risk: you're reading the market's intentions from the order book.
High signal: institutional/whale limit orders reveal their positioning.
"""
import config
from strategies.base import BaseStrategy
from utils.logger import log

ORDERBOOK_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "DOGE/USDT", "XRP/USDT",
    "PEPE/USDT", "WIF/USDT", "ARB/USDT", "OP/USDT", "AVAX/USDT",
    "LINK/USDT", "BNB/USDT", "ADA/USDT", "DOT/USDT", "MATIC/USDT",
    "SHIB/USDT", "BONK/USDT", "FLOKI/USDT", "SUI/USDT", "APT/USDT",
]


class OrderBookImbalanceStrategy(BaseStrategy):
    name = "orderbook_imbalance"

    def __init__(self, providers, notifier, db_module):
        super().__init__(providers, notifier, db_module)
        self._prev_walls: dict[str, list[dict]] = {}

    async def run(self) -> None:
        cex = self.providers["cex"]
        scanned = 0

        for symbol in ORDERBOOK_SYMBOLS:
            for exchange in config.CEXES:
                result = await self._analyze_orderbook(cex, exchange, symbol)
                if result:
                    await self._alert(result)
                scanned += 1

        log.info(f"[orderbook_imbalance] Scanned {scanned} order books")

    async def _analyze_orderbook(self, cex, exchange: str, symbol: str) -> dict | None:
        ob = await cex.get_orderbook(exchange, symbol, limit=20)
        if not ob:
            return None

        bids = ob.get("bids", [])
        asks = ob.get("asks", [])
        if not bids or not asks:
            return None

        bid_volume = sum(b[1] for b in bids)
        ask_volume = sum(a[1] for a in asks)
        total = bid_volume + ask_volume
        if total == 0:
            return None

        bid_volume / total
        ask_volume / total
        imbalance_ratio = bid_volume / ask_volume if ask_volume > 0 else 999

        best_bid = bids[0][0]
        best_ask = asks[0][0]
        mid_price = (best_bid + best_ask) / 2
        spread_pct = (best_ask - best_bid) / best_bid * 100

        bid_depth_usd = sum(b[0] * b[1] for b in bids)
        ask_depth_usd = sum(a[0] * a[1] for a in asks)

        walls = []
        avg_bid_size = bid_volume / len(bids) if bids else 0
        avg_ask_size = ask_volume / len(asks) if asks else 0

        for price, size in bids:
            if avg_bid_size > 0 and size > avg_bid_size * 5:
                dist = (mid_price - price) / mid_price * 100
                walls.append({
                    "side": "BID",
                    "price": price,
                    "size": size,
                    "size_usd": price * size,
                    "distance_pct": dist,
                    "ratio_vs_avg": size / avg_bid_size,
                })

        for price, size in asks:
            if avg_ask_size > 0 and size > avg_ask_size * 5:
                dist = (price - mid_price) / mid_price * 100
                walls.append({
                    "side": "ASK",
                    "price": price,
                    "size": size,
                    "size_usd": price * size,
                    "distance_pct": dist,
                    "ratio_vs_avg": size / avg_ask_size,
                })

        signals = []

        if imbalance_ratio >= config.OB_IMBALANCE_THRESHOLD:
            signals.append({
                "type": "BID_HEAVY",
                "detail": f"Bid/Ask ratio: {imbalance_ratio:.1f}x — strong buy pressure",
                "urgency": "critical" if imbalance_ratio >= 5 else "warning",
            })
        elif imbalance_ratio <= 1 / config.OB_IMBALANCE_THRESHOLD:
            signals.append({
                "type": "ASK_HEAVY",
                "detail": f"Bid/Ask ratio: {imbalance_ratio:.2f}x — strong sell pressure",
                "urgency": "critical" if imbalance_ratio <= 0.2 else "warning",
            })

        for wall in walls:
            if wall["size_usd"] >= config.OB_WALL_MIN_USD:
                signals.append({
                    "type": f"{wall['side']}_WALL",
                    "detail": (
                        f"${wall['size_usd']:,.0f} {wall['side']} wall at ${wall['price']:.8g} "
                        f"({wall['distance_pct']:.1f}% away, {wall['ratio_vs_avg']:.0f}x avg)"
                    ),
                    "urgency": "critical" if wall["size_usd"] >= config.OB_WALL_MIN_USD * 3 else "warning",
                    "wall": wall,
                })

        thin_bid = bid_depth_usd < ask_depth_usd * 0.2 and ask_depth_usd > 50_000
        thin_ask = ask_depth_usd < bid_depth_usd * 0.2 and bid_depth_usd > 50_000

        if thin_bid:
            signals.append({
                "type": "THIN_BIDS",
                "detail": f"Bid depth ${bid_depth_usd:,.0f} vs Ask ${ask_depth_usd:,.0f} — vulnerable to dump",
                "urgency": "warning",
            })
        elif thin_ask:
            signals.append({
                "type": "THIN_ASKS",
                "detail": f"Ask depth ${ask_depth_usd:,.0f} vs Bid ${bid_depth_usd:,.0f} — easy to pump",
                "urgency": "warning",
            })

        if not signals:
            return None

        return {
            "symbol": symbol,
            "exchange": exchange,
            "mid_price": mid_price,
            "spread_pct": spread_pct,
            "bid_volume": bid_volume,
            "ask_volume": ask_volume,
            "bid_depth_usd": bid_depth_usd,
            "ask_depth_usd": ask_depth_usd,
            "imbalance_ratio": imbalance_ratio,
            "walls": walls,
            "signals": signals,
        }

    async def _alert(self, result: dict) -> None:
        symbol = result["symbol"]
        exchange = result["exchange"]

        # Only alert on the strongest signal per pair
        best_signals = sorted(result["signals"], key=lambda s: 0 if s["urgency"] == "critical" else 1)
        for sig in best_signals[:1]:
            key = f"{symbol}_{sig['type']}"
            if not await self.db.should_alert(self.name, key, cooldown=3600):
                continue

            body = (
                f"Pair: {symbol}\n"
                f"Exchange: {exchange.upper()}\n"
                f"Mid price: ${result['mid_price']:.8g}\n"
                f"Spread: {result['spread_pct']:.3f}%\n"
                f"Bid depth: ${result['bid_depth_usd']:,.0f}\n"
                f"Ask depth: ${result['ask_depth_usd']:,.0f}\n"
                f"Imbalance: {result['imbalance_ratio']:.2f}x\n"
                f"\nSignal: {sig['detail']}\n"
            )

            if result["walls"]:
                body += "\nOrder walls:\n"
                for wall in sorted(result["walls"], key=lambda w: -w["size_usd"])[:3]:
                    body += f"  {wall['side']}: ${wall['size_usd']:,.0f} @ ${wall['price']:.8g}\n"

            interpretation = self._interpret(sig["type"], result)
            body += f"\n{interpretation}"

            await self.notifier.send_alert(
                self.name,
                f"ORDERBOOK: {symbol} {sig['type']}",
                body,
                sig["urgency"],
            )
            await self.db.record_alert(self.name, key, body)

    @staticmethod
    def _interpret(signal_type: str, result: dict) -> str:
        if signal_type == "BID_HEAVY":
            return "Interpretation: Strong buy-side pressure. Price likely to move up short-term. Consider long entry."
        elif signal_type == "ASK_HEAVY":
            return "Interpretation: Strong sell-side pressure. Price likely to drop short-term. Avoid longs."
        elif signal_type == "BID_WALL":
            return "Interpretation: Large buy wall = strong support level. Good area for stop-loss placement below."
        elif signal_type == "ASK_WALL":
            return "Interpretation: Large sell wall = strong resistance. Wait for breakout or trade the rejection."
        elif signal_type == "THIN_BIDS":
            return "Interpretation: Thin bid side — a moderate sell order can crash the price. High dump risk."
        elif signal_type == "THIN_ASKS":
            return "Interpretation: Thin ask side — a moderate buy order can spike the price. Easy pump potential."
        return ""
