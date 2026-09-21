import config
from strategies.base import BaseStrategy
from utils.logger import log


class FundingRateStrategy(BaseStrategy):
    name = "funding_rate"

    async def run(self) -> None:
        cex = self.providers["cex"]
        all_rates: dict[str, dict[str, float]] = {}

        for exchange in config.CEXES:
            rates = await cex.get_funding_rates(exchange)
            for r in rates:
                symbol = r.get("symbol", "")
                rate = r.get("fundingRate")
                if rate is None or not symbol:
                    continue
                if symbol not in all_rates:
                    all_rates[symbol] = {}
                all_rates[symbol][exchange] = float(rate)

        # Collect extreme rates and sort by magnitude
        extremes = []
        for symbol, rates_by_exchange in all_rates.items():
            for exchange, rate in rates_by_exchange.items():
                if abs(rate) >= config.FUNDING_RATE_EXTREME:
                    extremes.append((symbol, exchange, rate, rates_by_exchange))

        extremes.sort(key=lambda x: -abs(x[2]))

        # Only alert top 5 most extreme
        alerted = 0
        for symbol, exchange, rate, rates_by_exchange in extremes:
            if alerted >= 5:
                break

            if not await self.db.should_alert(self.name, f"{symbol}_{exchange}"):
                continue

            direction = "LONGS pay" if rate > 0 else "SHORTS pay"
            annualized = rate * 3 * 365 * 100
            urgency = "critical" if abs(rate) >= config.FUNDING_RATE_EXTREME * 3 else "warning"

            body = (
                f"Symbol: {symbol}\n"
                f"Exchange: {exchange.upper()}\n"
                f"Rate: {rate:.6f} ({rate*100:.4f}%)\n"
                f"Annualized: {annualized:.1f}%\n"
                f"Direction: {direction}\n"
            )

            other_rates = [f"{ex}: {r:.6f}" for ex, r in rates_by_exchange.items() if ex != exchange]
            if other_rates:
                body += f"Other exchanges: {', '.join(other_rates)}"

            await self.notifier.send_alert(self.name, f"Extreme Funding: {symbol}", body, urgency)
            await self.db.record_alert(self.name, f"{symbol}_{exchange}", body)
            alerted += 1

        # Funding spread arbitrage — only top 3
        spreads = []
        for symbol, rates_by_exchange in all_rates.items():
            values = list(rates_by_exchange.values())
            if len(values) < 2:
                continue
            max_r = max(values)
            min_r = min(values)
            spread = abs(max_r - min_r)
            # Both sides must be extreme individually for spread to matter
            if spread >= 0.003 and abs(max_r) >= config.FUNDING_RATE_EXTREME and abs(min_r) >= config.FUNDING_RATE_EXTREME:
                max_ex = [ex for ex, r in rates_by_exchange.items() if r == max_r][0]
                min_ex = [ex for ex, r in rates_by_exchange.items() if r == min_r][0]
                spreads.append((symbol, spread, max_ex, max_r, min_ex, min_r))

        spreads.sort(key=lambda x: -x[1])

        alerted_spreads = 0
        for symbol, spread, max_ex, max_r, min_ex, min_r in spreads:
            if alerted_spreads >= 3:
                break

            key = f"{symbol}_spread"
            if not await self.db.should_alert(self.name, key):
                continue

            body = (
                f"Symbol: {symbol}\n"
                f"Spread: {spread:.6f} ({spread*100:.4f}%)\n"
                f"High: {max_ex.upper()} = {max_r:.6f}\n"
                f"Low: {min_ex.upper()} = {min_r:.6f}\n"
                f"Annualized spread: {spread * 3 * 365 * 100:.0f}%\n"
                f"Opportunity: Short on {max_ex.upper()}, Long on {min_ex.upper()}"
            )
            await self.notifier.send_alert(self.name, f"Funding Spread: {symbol}", body, "critical")
            await self.db.record_alert(self.name, key, body)
            alerted_spreads += 1

        log.info(f"[funding_rate] Scanned {len(all_rates)} symbols, {alerted} extreme, {alerted_spreads} spreads")
