"""
Stablecoin Depeg Arbitrage:
Only USD-pegged stablecoins. Buy at discount, wait for re-peg.
"""
import config
from strategies.base import BaseStrategy
from utils.logger import log

# Only direct stable/stable pairs — no proxy guessing
STABLE_PAIRS = [
    "USDC/USDT",
    "DAI/USDT",
    "TUSD/USDT",
    "FDUSD/USDT",
]


class StablecoinDepegStrategy(BaseStrategy):
    name = "stablecoin_depeg"

    async def run(self) -> None:
        cex = self.providers["cex"]

        for pair in STABLE_PAIRS:
            for exchange in config.CEXES:
                ticker = await cex.get_ticker(exchange, pair)
                if not ticker:
                    continue

                price = float(ticker.get("last") or 0)
                if price <= 0:
                    continue

                deviation = abs(price - 1.0)
                if deviation < config.DEPEG_THRESHOLD:
                    continue

                bid = float(ticker.get("bid") or 0)
                ask = float(ticker.get("ask") or 0)
                spread = (ask - bid) / bid if bid > 0 else 0
                vol = float(ticker.get("quoteVolume") or 0)

                key = f"{pair}_{exchange}"
                if not await self.db.should_alert(self.name, key, cooldown=1800):
                    continue

                base = pair.split("/")[0]
                if price < 1.0:
                    action = f"Buy {base} at ${price:.4f}, wait for re-peg to $1.00"
                    profit_potential = (1.0 - price) / price * 100
                else:
                    action = f"Sell {base} at ${price:.4f}, buy back at $1.00"
                    profit_potential = (price - 1.0) / 1.0 * 100

                urgency = "critical" if deviation >= 0.02 else "warning"
                risk_note = "LOW RISK (minor depeg)" if deviation < 0.02 else "MEDIUM RISK (significant depeg)"

                body = (
                    f"Pair: {pair}\n"
                    f"Exchange: {exchange.upper()}\n"
                    f"Price: ${price:.6f}\n"
                    f"Deviation: {deviation*100:.3f}%\n"
                    f"Spread: {spread*100:.3f}%\n"
                    f"Volume: ${vol:,.0f}\n"
                    f"Profit: {profit_potential:.2f}%\n"
                    f"\nAction: {action}\n"
                    f"Risk: {risk_note}"
                )

                await self.notifier.send_alert(self.name, f"DEPEG: {pair} {deviation*100:.2f}%", body, urgency)
                await self.db.record_alert(self.name, key, body)

        log.info("[stablecoin_depeg] Scan complete")
