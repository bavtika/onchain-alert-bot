
from web3 import AsyncHTTPProvider, AsyncWeb3

import config
from utils.logger import log
from utils.rate_limiter import RateLimiter

UNISWAP_V2_PAIR_ABI = [
    {
        "constant": True,
        "inputs": [],
        "name": "getReserves",
        "outputs": [
            {"name": "_reserve0", "type": "uint112"},
            {"name": "_reserve1", "type": "uint112"},
            {"name": "_blockTimestampLast", "type": "uint32"},
        ],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "token0",
        "outputs": [{"name": "", "type": "address"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "token1",
        "outputs": [{"name": "", "type": "address"}],
        "type": "function",
    },
]

ERC20_DECIMALS_ABI = [
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function",
    }
]

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


class EvmProvider:
    def __init__(self):
        self._w3: dict[str, AsyncWeb3] = {}
        self._limiter = RateLimiter(rate=config.RATE_LIMITS["evm"], burst=2)
        self._decimals_cache: dict[str, int] = {}

    def _get_w3(self, chain: str) -> AsyncWeb3:
        if chain not in self._w3:
            rpcs = config.EVM_RPCS.get(chain, [])
            if not rpcs:
                raise ValueError(f"No RPC for chain {chain}")
            self._w3[chain] = AsyncWeb3(AsyncHTTPProvider(rpcs[0]))
        return self._w3[chain]

    async def _try_with_fallback(self, chain: str, coro_factory):
        rpcs = config.EVM_RPCS.get(chain, [])
        for i, rpc in enumerate(rpcs):
            try:
                if i > 0:
                    self._w3[chain] = AsyncWeb3(AsyncHTTPProvider(rpc))
                return await coro_factory(self._get_w3(chain))
            except Exception as e:
                log.debug(f"RPC {rpc} failed: {e}")
                continue
        return None

    async def get_dex_price(self, chain: str, pair_address: str, token_index: int = 0) -> float | None:
        await self._limiter.acquire()

        async def _fetch(w3: AsyncWeb3):
            pair = w3.eth.contract(
                address=w3.to_checksum_address(pair_address),
                abi=UNISWAP_V2_PAIR_ABI,
            )
            reserves = await pair.functions.getReserves().call()
            r0, r1 = reserves[0], reserves[1]

            token0_addr = await pair.functions.token0().call()
            token1_addr = await pair.functions.token1().call()

            d0 = await self._get_decimals(w3, token0_addr)
            d1 = await self._get_decimals(w3, token1_addr)

            adj_r0 = r0 / (10 ** d0)
            adj_r1 = r1 / (10 ** d1)

            if token_index == 0:
                return adj_r1 / adj_r0 if adj_r0 > 0 else None
            else:
                return adj_r0 / adj_r1 if adj_r1 > 0 else None

        return await self._try_with_fallback(chain, _fetch)

    async def _get_decimals(self, w3: AsyncWeb3, token_address: str) -> int:
        addr = w3.to_checksum_address(token_address)
        if addr in self._decimals_cache:
            return self._decimals_cache[addr]
        try:
            await self._limiter.acquire()
            contract = w3.eth.contract(address=addr, abi=ERC20_DECIMALS_ABI)
            decimals = await contract.functions.decimals().call()
            self._decimals_cache[addr] = decimals
            return decimals
        except Exception:
            return 18

    async def get_latest_block_transfers(self, chain: str) -> list[dict]:
        await self._limiter.acquire()

        async def _fetch(w3: AsyncWeb3):
            block = await w3.eth.get_block("latest", full_transactions=True)
            transfers = []
            native_symbol = {"ethereum": "ETH", "bsc": "BNB", "arbitrum": "ETH"}.get(chain, "ETH")

            exchange_wallets = config.EXCHANGE_WALLETS.get(chain, {})
            wallet_set = set(k.lower() for k in exchange_wallets)

            for tx in block.get("transactions", []):
                value_wei = tx.get("value", 0)
                if value_wei == 0:
                    continue
                from_addr = tx.get("from", "").lower()
                to_addr = tx.get("to", "").lower() if tx.get("to") else ""

                is_exchange_involved = from_addr in wallet_set or to_addr in wallet_set
                if not is_exchange_involved:
                    continue

                value_eth = float(w3.from_wei(value_wei, "ether"))
                direction = "deposit" if to_addr in wallet_set else "withdrawal"
                exchange_name = exchange_wallets.get(to_addr if direction == "deposit" else from_addr, "Unknown")

                transfers.append({
                    "tx_hash": tx["hash"].hex() if hasattr(tx["hash"], "hex") else str(tx["hash"]),
                    "chain": chain,
                    "token": native_symbol,
                    "value": value_eth,
                    "direction": direction,
                    "exchange": exchange_name,
                    "from": from_addr,
                    "to": to_addr,
                })

            return transfers

        result = await self._try_with_fallback(chain, _fetch)
        return result or []
