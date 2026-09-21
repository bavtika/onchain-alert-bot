"""
Smart Money Tracker:
- Monitors wallets with proven profitable track records
- Tracks what they buy/sell in real-time
- Detects when multiple smart money wallets converge on same token
- Alerts on new positions from tracked wallets
"""
import time
from collections import defaultdict

from utils.logger import log
from utils.rate_limiter import RateLimiter


class SmartMoneyTracker:
    def __init__(self, wallet_analyzer, cex_provider, evm_provider, solana_provider):
        self.wallet_analyzer = wallet_analyzer
        self.cex = cex_provider
        self.evm = evm_provider
        self.solana = solana_provider
        self._limiter = RateLimiter(rate=2, burst=2)
        self._last_scanned: dict[str, float] = {}
        self._convergence_cache: dict[str, list[str]] = defaultdict(list)

    async def scan_smart_wallets(self) -> list[dict]:
        """
        Scan recent activity from smart money wallets.
        Returns list of interesting moves.
        """
        smart_wallets = await self.wallet_analyzer.get_smart_money_wallets()
        if not smart_wallets:
            return []

        interesting_moves = []

        for sw in smart_wallets[:20]:
            wallet = sw["wallet"]
            chain = sw["chain"]

            last_scan = self._last_scanned.get(wallet, 0)
            if time.time() - last_scan < 60:
                continue

            if chain in ("ethereum", "bsc", "arbitrum"):
                moves = await self._scan_evm_wallet(wallet, chain)
            elif chain == "solana":
                moves = await self._scan_solana_wallet(wallet)
            else:
                continue

            self._last_scanned[wallet] = time.time()

            for move in moves:
                move["smart_money_score"] = sw["score"]
                move["wallet_pnl"] = sw.get("total_profit_usd", 0)
                interesting_moves.append(move)

                token = move.get("token_address", "")
                if token and move["action"] == "buy":
                    self._convergence_cache[token].append(wallet)

        return interesting_moves

    async def detect_convergence(self, min_wallets: int = 2, time_window: float = 3600) -> list[dict]:
        """
        Detect tokens where multiple smart money wallets are converging.
        """
        convergences = []
        for token, wallets in self._convergence_cache.items():
            unique_wallets = list(set(wallets))
            if len(unique_wallets) >= min_wallets:
                convergences.append({
                    "token_address": token,
                    "wallet_count": len(unique_wallets),
                    "wallets": unique_wallets[:10],
                })

        return convergences

    async def _scan_evm_wallet(self, wallet: str, chain: str) -> list[dict]:
        """Check recent transactions for an EVM wallet."""
        await self._limiter.acquire()
        try:
            w3 = self.evm._get_w3(chain)
            block = await w3.eth.get_block("latest", full_transactions=True)
            moves = []

            for tx in block.get("transactions", []):
                from_addr = (tx.get("from") or "").lower()
                to_addr = (tx.get("to") or "").lower()

                if from_addr != wallet.lower() and to_addr != wallet.lower():
                    continue

                value_wei = tx.get("value", 0)
                input_data = tx.get("input", "0x")

                if isinstance(input_data, bytes):
                    input_hex = input_data.hex()
                else:
                    input_hex = str(input_data)

                is_swap = len(input_hex) > 10 and input_hex[:10] in (
                    "0x38ed1739",  # swapExactTokensForTokens
                    "0x7ff36ab5",  # swapExactETHForTokens
                    "0x18cbafe5",  # swapExactTokensForETH
                    "0x5c11d795",  # swapExactTokensForTokensSupportingFeeOnTransferTokens
                    "0xfb3bdb41",  # swapETHForExactTokens
                    "0x791ac947",  # swapExactTokensForETHSupportingFeeOnTransferTokens
                    "0x04e45aaf",  # exactInputSingle (V3)
                    "0xb858183f",  # exactInput (V3)
                )

                if is_swap:
                    native_symbol = {"ethereum": "ETH", "bsc": "BNB", "arbitrum": "ETH"}.get(chain, "ETH")
                    value_native = float(w3.from_wei(value_wei, "ether"))

                    action = "buy" if value_wei > 0 else "sell"
                    moves.append({
                        "wallet": wallet,
                        "chain": chain,
                        "action": action,
                        "token_address": to_addr,
                        "value_native": value_native,
                        "native_symbol": native_symbol,
                        "tx_hash": tx["hash"].hex() if hasattr(tx["hash"], "hex") else str(tx["hash"]),
                    })

            return moves
        except Exception as e:
            log.debug(f"Smart money EVM scan {wallet[:10]}...: {e}")
            return []

    async def _scan_solana_wallet(self, wallet: str) -> list[dict]:
        """Check recent transactions for a Solana wallet."""
        try:
            sigs = await self.solana.get_recent_signatures(wallet, limit=5)
            moves = []

            for sig_info in sigs:
                sig = sig_info.get("signature")
                if not sig:
                    continue

                tx = await self.solana.get_transaction(sig)
                if not tx or tx.get("meta", {}).get("err"):
                    continue

                instructions = (
                    tx.get("transaction", {})
                    .get("message", {})
                    .get("instructions", [])
                )

                for ix in instructions:
                    program = ix.get("programId", "")
                    if program in (
                        "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium AMM
                        "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",  # Jupiter
                        "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",  # Orca Whirlpool
                    ):
                        moves.append({
                            "wallet": wallet,
                            "chain": "solana",
                            "action": "swap",
                            "token_address": "",
                            "tx_hash": sig,
                        })
                        break

            return moves
        except Exception as e:
            log.debug(f"Smart money Solana scan {wallet[:10]}...: {e}")
            return []

    def cleanup(self) -> None:
        self._convergence_cache.clear()
