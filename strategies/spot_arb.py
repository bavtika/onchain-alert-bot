"""
CEX-to-CEX Spot Arbitrage:
Buy on exchange A, sell on exchange B simultaneously.
Zero directional risk — pure spread capture.
Scans ALL pairs across Binance/Bybit/MEXC for price discrepancies.
"""
import config
from strategies.base import BaseStrategy
from utils.logger import log


class SpotArbStrategy(BaseStrategy):
    name = "spot_arb"

    def __init__(self, providers, notifier, db_module):
        super().__init__(providers, notifier, db_module)
        self._pair_cache: dict[str, set[str]] = {}

    async def run(self) -> None:
        cex = self.providers["cex"]
        common_pairs = await self._find_common_pairs(cex)
        log.info(f"[spot_arb] Scanning {len(common_pairs)} common pairs")

        for symbol in common_pairs:
            prices = {}
            volumes = {}
            spreads = {}

            for exchange in config.CEXES:
                ticker = await cex.get_ticker(exchange, symbol)
                if not ticker:
                    continue
                bid = float(ticker.get("bid") or 0)
                ask = float(ticker.get("ask") or 0)
                vol = float(ticker.get("quoteVolume") or 0)
                if bid <= 0 or ask <= 0:
                    continue
                prices[exchange] = {"bid": bid, "ask": ask, "mid": (bid + ask) / 2}
                volumes[exchange] = vol
                spreads[exchange] = (ask - bid) / bid

            if len(prices) < 2:
                continue

            exchanges = list(prices.keys())
            for i in range(len(exchanges)):
                for j in range(i + 1, len(exchanges)):
                    ex_a, ex_b = exchanges[i], exchanges[j]
                    p_a, p_b = prices[ex_a], prices[ex_b]

                    profit_a_to_b = (p_b["bid"] - p_a["ask"]) / p_a["ask"]
                    profit_b_to_a = (p_a["bid"] - p_b["ask"]) / p_b["ask"]

                    best_profit = max(profit_a_to_b, profit_b_to_a)
                    if best_profit < config.SPOT_ARB_MIN_PROFIT:
                        continue

                    if best_profit == profit_a_to_b:
                        buy_ex, sell_ex = ex_a, ex_b
                        buy_price, sell_price = p_a["ask"], p_b["bid"]
                    else:
                        buy_ex, sell_ex = ex_b, ex_a
                        buy_price, sell_price = p_b["ask"], p_a["bid"]

                    min_vol = min(volumes.get(buy_ex, 0), volumes.get(sell_ex, 0))
                    if min_vol < 10_000:
                        continue

                    key = f"{symbol}_{buy_ex}_{sell_ex}"
                    if not await self.db.should_alert(self.name, key, cooldown=300):
                        continue

                    net_profit = best_profit - self._estimate_fees(buy_ex, sell_ex)

                    # Sanity: >10% on spot is data error or illiquid garbage
                    if net_profit > 0.10:
                        log.debug(f"[spot_arb] Skipping {symbol} {buy_ex}->{sell_ex}: {net_profit*100:.1f}% (likely stale)")
                        continue

                    urgency = "critical" if net_profit >= 0.005 else "warning"
                    if net_profit < 0.001:
                        continue

                    body = (
                        f"Pair: {symbol}\n"
                        f"Buy: {buy_ex.upper()} @ ${buy_price:.8g}\n"
                        f"Sell: {sell_ex.upper()} @ ${sell_price:.8g}\n"
                        f"Gross spread: {best_profit*100:.3f}%\n"
                        f"Est. fees: {self._estimate_fees(buy_ex, sell_ex)*100:.3f}%\n"
                        f"Net profit: {net_profit*100:.3f}%\n"
                        f"Min volume: ${min_vol:,.0f}\n"
                        f"\nAction: Buy {buy_ex.upper()}, Sell {sell_ex.upper()} simultaneously"
                    )

                    await self.notifier.send_alert(self.name, f"SPOT ARB: {symbol} +{net_profit*100:.2f}%", body, urgency)
                    await self.db.record_alert(self.name, key, body)

    async def _find_common_pairs(self, cex) -> list[str]:
        if self._pair_cache:
            all_sets = list(self._pair_cache.values())
        else:
            all_sets = []
            for exchange in config.CEXES:
                symbols = await self._get_spot_symbols(cex, exchange)
                self._pair_cache[exchange] = set(symbols)
                all_sets.append(set(symbols))

        if len(all_sets) < 2:
            return []

        common = all_sets[0]
        for s in all_sets[1:]:
            common = common & s

        usdt_pairs = [s for s in common if s.endswith("/USDT")]
        return usdt_pairs[:200]

    async def _get_spot_symbols(self, cex, exchange: str) -> list[str]:
        return await cex.get_all_spot_symbols(exchange)

    @staticmethod
    def _estimate_fees(buy_ex: str, sell_ex: str) -> float:
        fees = {"binance": 0.001, "bybit": 0.001, "mexc": 0.001}
        return fees.get(buy_ex, 0.001) + fees.get(sell_ex, 0.001)
