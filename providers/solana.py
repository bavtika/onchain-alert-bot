import aiohttp

import config
from utils.http import create_session
from utils.logger import log
from utils.rate_limiter import RateLimiter


class SolanaProvider:
    def __init__(self):
        self._limiter = RateLimiter(rate=config.RATE_LIMITS["solana"], burst=1)
        self._session: aiohttp.ClientSession | None = None
        self._rpc_index = 0

    def _rpc_url(self) -> str:
        return config.SOLANA_RPCS[self._rpc_index % len(config.SOLANA_RPCS)]

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = create_session()
        return self._session

    async def _rpc_call(self, method: str, params: list | None = None) -> dict | None:
        await self._limiter.acquire()
        session = await self._get_session()
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params or [],
        }
        try:
            async with session.post(self._rpc_url(), json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                data = await resp.json()
                if "error" in data:
                    log.debug(f"Solana RPC error: {data['error']}")
                    return None
                return data.get("result")
        except Exception as e:
            log.debug(f"Solana RPC call {method} failed: {e}")
            return None

    async def get_recent_signatures(self, address: str, limit: int = 20) -> list[dict]:
        result = await self._rpc_call(
            "getSignaturesForAddress",
            [address, {"limit": limit}],
        )
        return result or []

    async def get_transaction(self, signature: str) -> dict | None:
        return await self._rpc_call(
            "getTransaction",
            [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
        )

    async def get_large_transfers(self) -> list[dict]:
        exchange_wallets = config.EXCHANGE_WALLETS.get("solana", {})
        transfers = []

        for wallet_addr, exchange_name in exchange_wallets.items():
            sigs = await self.get_recent_signatures(wallet_addr, limit=10)

            for sig_info in sigs:
                sig = sig_info.get("signature")
                if not sig:
                    continue

                tx = await self.get_transaction(sig)
                if not tx:
                    continue

                meta = tx.get("meta", {})
                if meta.get("err"):
                    continue

                pre_balances = meta.get("preBalances", [])
                post_balances = meta.get("postBalances", [])
                account_keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])

                if not account_keys or not pre_balances:
                    continue

                for i, key in enumerate(account_keys):
                    addr = key.get("pubkey", key) if isinstance(key, dict) else str(key)
                    if addr == wallet_addr and i < len(pre_balances) and i < len(post_balances):
                        diff_lamports = post_balances[i] - pre_balances[i]
                        diff_sol = abs(diff_lamports) / 1e9
                        if diff_sol > 0.1:
                            direction = "deposit" if diff_lamports > 0 else "withdrawal"
                            transfers.append({
                                "tx_hash": sig,
                                "chain": "solana",
                                "token": "SOL",
                                "value": diff_sol,
                                "direction": direction,
                                "exchange": exchange_name,
                            })

        return transfers

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
