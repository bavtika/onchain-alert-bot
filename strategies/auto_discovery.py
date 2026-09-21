"""
Auto-discovery strategy: the brain of the bot.
1. Scans DEXScreener/GeckoTerminal for new/trending tokens
2. Runs risk scoring on each candidate
3. Runs pattern detection
4. Tracks wallet activity and detects coordination
5. Alerts only on high-confidence opportunities
"""
import config
from strategies.base import BaseStrategy
from utils.logger import log


class AutoDiscoveryStrategy(BaseStrategy):
    name = "auto_discovery"

    def __init__(self, providers, notifier, db_module, scanner, risk_scorer, pattern_detector, wallet_analyzer):
        super().__init__(providers, notifier, db_module)
        self.scanner = scanner
        self.risk_scorer = risk_scorer
        self.pattern_detector = pattern_detector
        self.wallet_analyzer = wallet_analyzer

    async def run(self) -> None:
        candidates = await self.scanner.discover_candidates()
        log.info(f"[auto_discovery] Found {len(candidates)} candidates")

        interesting = self._filter_candidates(candidates)
        log.info(f"[auto_discovery] {len(interesting)} passed initial filter")

        for token in interesting[:15]:
            await self._analyze_token(token)

        # periodic smart money score update
        top_wallets = await self.wallet_analyzer.update_smart_money_scores()
        if top_wallets:
            log.info(f"[auto_discovery] {len(top_wallets)} smart money wallets tracked")

        self.wallet_analyzer.cleanup_old_buys()

    def _filter_candidates(self, candidates: list[dict]) -> list[dict]:
        """Pre-filter: remove obvious noise before expensive API calls."""
        filtered = []
        for c in candidates:
            liq = c.get("liquidity_usd", 0)
            vol = c.get("volume_24h", 0)

            if liq < 50_000:
                continue
            if vol < 50_000:
                continue

            price_change = abs(c.get("price_change_24h", 0))
            vol_liq = vol / liq if liq > 0 else 0

            interest_score = 0
            if price_change > 20:
                interest_score += 3
            if price_change > 50:
                interest_score += 3
            if vol_liq > 2:
                interest_score += 2
            if vol_liq > 10:
                interest_score += 3
            if liq > 100_000:
                interest_score += 2
            if c.get("source") == "boosted":
                interest_score += 2
            if c.get("source") == "new_pool":
                interest_score += 1

            c["interest_score"] = interest_score
            if interest_score >= 5:
                filtered.append(c)

        filtered.sort(key=lambda x: -x["interest_score"])
        return filtered

    async def _analyze_token(self, token: dict) -> None:
        symbol = token["symbol"]
        chain = token["chain"]
        token_address = token.get("token_address", "")

        alert_key = f"{chain}_{token_address or symbol}"
        if not await self.db.should_alert(self.name, alert_key, cooldown=7200):
            return

        risk = {"score": 50, "risks": [], "verdict": "UNKNOWN", "details": {}}
        if token_address and chain != "solana":
            risk = await self.risk_scorer.score_token(chain, token_address, token)

        if risk["verdict"] in ("SCAM", "DANGER"):
            log.debug(f"[auto_discovery] {symbol} flagged as {risk['verdict']}, skipping")
            return

        patterns = []
        cex_symbol = f"{symbol}/USDT"
        for exchange in config.CEXES:
            p = await self.providers["cex"].get_price(exchange, cex_symbol)
            if p:
                patterns = await self.pattern_detector.scan_all_patterns(cex_symbol)
                break

        len(patterns) > 0
        len(risk["risks"]) > 0

        opportunity_score = self._calculate_opportunity(token, risk, patterns)

        if opportunity_score < 65:
            return

        title = f"{symbol} ({chain.upper()})"
        body_lines = [
            f"Source: {token.get('source', 'unknown')}",
            f"Price: ${token.get('price_usd', 0):.8g}",
            f"24h Change: {token.get('price_change_24h', 0):+.1f}%",
            f"Volume 24h: ${token.get('volume_24h', 0):,.0f}",
            f"Liquidity: ${token.get('liquidity_usd', 0):,.0f}",
            "",
            f"Risk Score: {risk['score']}/100 ({risk['verdict']})",
        ]

        if risk["risks"]:
            body_lines.append("Risks:")
            for r in risk["risks"][:5]:
                body_lines.append(f"  - {r}")

        if patterns:
            body_lines.append("")
            body_lines.append("Patterns detected:")
            for p in patterns:
                pat = p.get("pattern", "unknown")
                if pat == "pump_dump":
                    body_lines.append(
                        f"  - PUMP&DUMP: {p['phase']} (+{p['pump_pct']:.0f}%, vol {p['volume_ratio']:.0f}x)"
                    )
                elif pat == "cross_exchange_anomaly":
                    body_lines.append(
                        f"  - CROSS-EXCHANGE: {p['spread_pct']:.2f}% spread (buy {p['buy_on']}, sell {p['sell_on']})"
                    )
                elif pat == "wash_trading":
                    body_lines.append(
                        f"  - WASH TRADING: high vol, {p['price_range_pct']:.2f}% price range"
                    )
                elif pat == "futures_spot_divergence":
                    body_lines.append(
                        f"  - FUTURES/SPOT: {p['premium_pct']:+.2f}% premium ({p['signal']})"
                    )

        if token_address:
            body_lines.append("")
            body_lines.append(f"Token: {token_address[:20]}...")

        body_lines.append(f"\nOpportunity Score: {opportunity_score}/100")

        urgency = "critical" if opportunity_score >= 75 else "warning" if opportunity_score >= 50 else "info"

        await self.notifier.send_alert(self.name, title, "\n".join(body_lines), urgency)
        await self.db.record_alert(self.name, alert_key, title)

    def _calculate_opportunity(self, token: dict, risk: dict, patterns: list) -> int:
        """
        Calculate opportunity score combining all signals.
        0 = garbage, 100 = max opportunity.
        """
        score = 0

        # risk-adjusted base
        risk_score = risk.get("score", 50)
        if risk_score < 20:
            return 0
        score += risk_score * 0.3

        # price momentum
        price_change = abs(token.get("price_change_24h", 0))
        if 20 <= price_change <= 100:
            score += 15
        elif 100 < price_change <= 300:
            score += 25
        elif price_change > 300:
            score += 10  # too much, likely already played out

        # volume quality
        vol = token.get("volume_24h", 0)
        liq = token.get("liquidity_usd", 0)
        if vol > 50_000 and liq > 50_000:
            score += 15
        elif vol > 10_000:
            score += 8

        # patterns add conviction
        for p in patterns:
            pat = p.get("pattern")
            if pat == "pump_dump" and p.get("phase") == "PUMPING":
                score += 15
            elif pat == "cross_exchange_anomaly" and p.get("spread_pct", 0) > 1:
                score += 20
            elif pat == "futures_spot_divergence" and abs(p.get("premium_pct", 0)) > 1:
                score += 10

        # source bonus
        if token.get("source") == "boosted":
            score += 5
        if token.get("source") == "trending":
            score += 5

        return min(100, max(0, int(score)))
