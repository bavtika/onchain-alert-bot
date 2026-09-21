"""
Smart money following strategy:
- Track what smart money wallets are doing
- Alert when smart money buys something new
- Alert when multiple smart money wallets converge on same token
"""
from strategies.base import BaseStrategy
from utils.logger import log


class SmartMoneyStrategy(BaseStrategy):
    name = "smart_money"

    def __init__(self, providers, notifier, db_module, smart_money_tracker, wallet_analyzer):
        super().__init__(providers, notifier, db_module)
        self.smart_money = smart_money_tracker
        self.wallet_analyzer = wallet_analyzer

    async def run(self) -> None:
        moves = await self.smart_money.scan_smart_wallets()

        for move in moves:
            wallet = move.get("wallet", "")[:10]
            chain = move.get("chain", "")
            action = move.get("action", "")
            tx_hash = move.get("tx_hash", "")
            score = move.get("smart_money_score", 0)

            if await self.db.is_tx_seen(tx_hash):
                continue
            await self.db.mark_tx_seen(tx_hash, chain)

            key = f"sm_{wallet}_{chain}"
            if not await self.db.should_alert(self.name, key, cooldown=300):
                continue

            move.get("token_address", "unknown")
            native_val = move.get("value_native", 0)
            native_sym = move.get("native_symbol", "")

            body = (
                f"Wallet: {move.get('wallet', '')[:16]}...\n"
                f"Chain: {chain.upper()}\n"
                f"Action: {action.upper()}\n"
                f"Smart Score: {score}/100\n"
                f"Historical PnL: ${move.get('wallet_pnl', 0):,.0f}\n"
            )
            if native_val > 0:
                body += f"Size: {native_val:.4f} {native_sym}\n"
            body += f"TX: {tx_hash[:20]}..."

            urgency = "critical" if score >= 85 else "warning"
            await self.notifier.send_alert(
                self.name,
                f"Smart Money {action.upper()} on {chain.upper()}",
                body,
                urgency,
            )
            await self.db.record_alert(self.name, key, body)

        convergences = await self.smart_money.detect_convergence(min_wallets=2)
        for conv in convergences:
            key = f"convergence_{conv['token_address'][:16]}"
            if not await self.db.should_alert(self.name, key, cooldown=3600):
                continue

            body = (
                f"Token: {conv['token_address'][:20]}...\n"
                f"Smart wallets buying: {conv['wallet_count']}\n"
                f"Wallets:\n"
            )
            for w in conv["wallets"][:5]:
                body += f"  {w[:16]}...\n"

            await self.notifier.send_alert(
                self.name,
                f"CONVERGENCE: {conv['wallet_count']} Smart Wallets",
                body,
                "critical",
            )
            await self.db.record_alert(self.name, key, body)

        self.smart_money.cleanup()
        log.info(f"[smart_money] {len(moves)} moves scanned")
