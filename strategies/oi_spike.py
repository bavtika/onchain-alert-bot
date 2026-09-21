"""
Open Interest spike detection:
- Monitors OI across Binance, Bybit, MEXC perpetual futures
- Detects sudden OI surges (new positions being opened)
- Detects OI drops (mass liquidations or position closes)
- Correlates OI changes with price to determine sentiment
- Cross-exchange OI divergence (one exchange accumulating while others flat)

Signals:
- OI spike + price up = aggressive longs (potential long squeeze if overextended)
- OI spike + price down = aggressive shorts (potential short squeeze)
- OI spike + price flat = stealth accumulation, big move incoming
- OI drop + price drop = long liquidation cascade
- OI drop + price up = short squeeze
"""
import time

import config
from strategies.base import BaseStrategy
from utils.logger import log


class OISpikeStrategy(BaseStrategy):
    name = "oi_spike"

    def __init__(self, providers, notifier, db_module):
        super().__init__(providers, notifier, db_module)
        self._oi_cache: dict[str, list[dict]] = {}

    async def run(self) -> None:
        cex = self.providers["cex"]
        scanned = 0

        for exchange in config.CEXES:
            symbols = await cex.get_all_swap_symbols(exchange)
            if not symbols:
                continue

            top_symbols = await self._get_top_by_volume(cex, exchange, symbols)

            for symbol in top_symbols:
                await self._analyze_oi(cex, exchange, symbol)
                scanned += 1

        await self._detect_cross_exchange_oi()
        log.info(f"[oi_spike] Scanned {scanned} pairs")

    async def _get_top_by_volume(self, cex, exchange: str, symbols: list[str], limit: int = 50) -> list[str]:
        """Pick top symbols by volume — no point scanning dead pairs."""
        tickers = await cex.get_all_tickers_swap(exchange)
        vol_symbols = []
        symbols_set = set(symbols)
        for sym, ticker in tickers.items():
            if sym not in symbols_set:
                continue
            vol = float(ticker.get("quoteVolume") or 0)
            if vol > 0:
                vol_symbols.append((sym, vol))

        vol_symbols.sort(key=lambda x: -x[1])
        return [s[0] for s in vol_symbols[:limit]]

    async def _analyze_oi(self, cex, exchange: str, symbol: str) -> None:
        oi_history = await cex.get_open_interest_history(exchange, symbol, "1h", 25)

        if not oi_history or len(oi_history) < 4:
            current_oi = await cex.get_open_interest(exchange, symbol)
            if current_oi:
                self._update_cache(exchange, symbol, current_oi)
                await self._check_cache_spike(exchange, symbol)
            return

        await self._analyze_oi_history(cex, exchange, symbol, oi_history)

    async def _analyze_oi_history(self, cex, exchange: str, symbol: str, oi_history: list) -> None:
        if len(oi_history) < 4:
            return

        oi_values = []
        for entry in oi_history:
            oi_val = entry.get("openInterestValue") or entry.get("openInterestAmount") or entry.get("baseVolume", 0)
            if isinstance(oi_val, (int, float)) and oi_val > 0:
                oi_values.append(float(oi_val))

        if len(oi_values) < 4:
            return

        current_oi = oi_values[-1]
        recent_avg = sum(oi_values[-4:-1]) / 3
        full_avg = sum(oi_values[:-1]) / len(oi_values[:-1])

        if full_avg <= 0:
            return

        change_vs_recent = (current_oi - recent_avg) / recent_avg if recent_avg > 0 else 0
        change_vs_avg = (current_oi - full_avg) / full_avg

        spike_threshold = config.OI_SPIKE_THRESHOLD
        drop_threshold = config.OI_DROP_THRESHOLD

        is_spike = change_vs_recent >= spike_threshold or change_vs_avg >= spike_threshold
        is_drop = change_vs_recent <= drop_threshold or change_vs_avg <= drop_threshold

        if not is_spike and not is_drop:
            return

        key = f"{exchange}_{symbol}"
        if not await self.db.should_alert(self.name, key, cooldown=config.OI_ALERT_COOLDOWN):
            return

        ticker = await cex.get_ticker(exchange, symbol)
        price_change_pct = 0
        current_price = 0
        if ticker:
            current_price = float(ticker.get("last", 0) or 0)
            open_price = float(ticker.get("open", 0) or 0)
            if open_price > 0:
                price_change_pct = ((current_price - open_price) / open_price) * 100

        signal = self._interpret_signal(is_spike, change_vs_recent, price_change_pct)

        if is_spike:
            event = "OI SPIKE"
            change_pct = max(change_vs_recent, change_vs_avg) * 100
        else:
            event = "OI DROP"
            change_pct = min(change_vs_recent, change_vs_avg) * 100

        clean_symbol = symbol.split(":")[0] if ":" in symbol else symbol

        body = (
            f"Symbol: {clean_symbol}\n"
            f"Exchange: {exchange.upper()}\n"
            f"OI Change: {change_pct:+.1f}%\n"
            f"Current OI: ${current_oi:,.0f}\n"
            f"3h Avg OI: ${recent_avg:,.0f}\n"
            f"24h Avg OI: ${full_avg:,.0f}\n"
        )
        if current_price:
            body += f"Price: ${current_price:,.4f} ({price_change_pct:+.1f}%)\n"
        body += (
            f"\nSignal: {signal['label']}\n"
            f"Interpretation: {signal['interpretation']}"
        )

        urgency = "critical" if abs(change_pct) >= spike_threshold * 200 else "warning"

        await self.notifier.send_alert(self.name, f"{event}: {clean_symbol}", body, urgency)
        await self.db.record_alert(self.name, key, body)

        await self.db.insert_oi_point(symbol, exchange, current_oi, change_vs_recent)

    def _interpret_signal(self, is_spike: bool, oi_change: float, price_change: float) -> dict:
        if is_spike:
            if price_change > 2:
                return {
                    "label": "AGGRESSIVE LONGS",
                    "interpretation": "New long positions opening with rising price. "
                    "Watch for long squeeze if OI gets too extended.",
                }
            elif price_change < -2:
                return {
                    "label": "AGGRESSIVE SHORTS",
                    "interpretation": "New short positions opening with falling price. "
                    "Potential short squeeze setup if price reverses.",
                }
            else:
                return {
                    "label": "STEALTH ACCUMULATION",
                    "interpretation": "Large positions being built with minimal price impact. "
                    "Big move likely incoming — direction unclear. Watch closely.",
                }
        else:
            if price_change < -5:
                return {
                    "label": "LONG LIQUIDATION CASCADE",
                    "interpretation": "Mass long liquidations. Possible capitulation bottom "
                    "if selling pressure exhausts.",
                }
            elif price_change > 5:
                return {
                    "label": "SHORT SQUEEZE",
                    "interpretation": "Shorts getting liquidated, pushing price up. "
                    "Can extend further if more shorts are trapped above.",
                }
            else:
                return {
                    "label": "POSITION UNWINDING",
                    "interpretation": "Traders closing positions. Reduced conviction — "
                    "volatility may decrease short-term.",
                }

    def _update_cache(self, exchange: str, symbol: str, oi_data: dict) -> None:
        key = f"{exchange}_{symbol}"
        oi_val = oi_data.get("openInterestValue") or oi_data.get("openInterestAmount", 0)
        if not oi_val:
            return
        if key not in self._oi_cache:
            self._oi_cache[key] = []
        self._oi_cache[key].append({
            "value": float(oi_val),
            "ts": time.time(),
        })
        self._oi_cache[key] = self._oi_cache[key][-50:]

    async def _check_cache_spike(self, exchange: str, symbol: str) -> None:
        key = f"{exchange}_{symbol}"
        history = self._oi_cache.get(key, [])
        if len(history) < 4:
            return

        current = history[-1]["value"]
        recent = [h["value"] for h in history[-4:-1]]
        avg = sum(recent) / len(recent)
        if avg <= 0:
            return

        change = (current - avg) / avg
        if abs(change) >= config.OI_SPIKE_THRESHOLD:
            await self._analyze_oi_history(
                self.providers["cex"], exchange, symbol,
                [{"openInterestValue": h["value"]} for h in history]
            )

    async def _discover_common_futures(self, cex) -> list[str]:
        """Find symbols available on multiple exchanges for cross-exchange OI comparison."""
        exchange_symbols: dict[str, set[str]] = {}
        for exchange in config.CEXES:
            symbols = await cex.get_all_swap_symbols(exchange)
            exchange_symbols[exchange] = set(symbols)

        if len(exchange_symbols) < 2:
            return []

        all_sets = list(exchange_symbols.values())
        common = all_sets[0]
        for s in all_sets[1:]:
            common = common & s

        return list(common)[:30]

    async def _detect_cross_exchange_oi(self) -> None:
        """Detect when OI diverges significantly across exchanges for same pair."""
        cex = self.providers["cex"]
        common_symbols = await self._discover_common_futures(cex)

        for symbol in common_symbols:
            oi_by_exchange = {}
            for exchange in config.CEXES:
                oi = await cex.get_open_interest(exchange, symbol)
                if oi:
                    val = oi.get("openInterestValue") or oi.get("openInterestAmount", 0)
                    if val and float(val) > 0:
                        oi_by_exchange[exchange] = float(val)

            if len(oi_by_exchange) < 2:
                continue

            values = list(oi_by_exchange.values())
            max_oi = max(values)
            min_oi = min(values)
            if min_oi <= 0:
                continue

            divergence = (max_oi - min_oi) / min_oi

            if divergence < 0.5:
                continue

            key = f"oi_divergence_{symbol}"
            if not await self.db.should_alert(self.name, key, cooldown=3600):
                continue

            max_ex = [ex for ex, v in oi_by_exchange.items() if v == max_oi][0]
            min_ex = [ex for ex, v in oi_by_exchange.items() if v == min_oi][0]
            clean_symbol = symbol.split(":")[0]

            body = (
                f"Symbol: {clean_symbol}\n"
                f"Divergence: {divergence*100:.0f}%\n\n"
                f"OI by exchange:\n"
            )
            for ex, val in sorted(oi_by_exchange.items(), key=lambda x: -x[1]):
                body += f"  {ex.upper()}: ${val:,.0f}\n"

            body += (
                f"\nHighest: {max_ex.upper()} (${max_oi:,.0f})\n"
                f"Lowest: {min_ex.upper()} (${min_oi:,.0f})\n"
                f"\nSignal: Positions concentrating on {max_ex.upper()}. "
                f"Watch for liquidation cascade on that exchange."
            )

            await self.notifier.send_alert(self.name, f"OI DIVERGENCE: {clean_symbol}", body, "warning")
            await self.db.record_alert(self.name, key, body)
