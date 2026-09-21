"""
Auto-discovers tokens via free APIs:
- DEXScreener: new pairs, trending, boosted tokens
- GeckoTerminal: trending pools, new pools, top gainers
No API keys needed.
"""

import aiohttp

from utils.http import create_session
from utils.logger import log
from utils.rate_limiter import RateLimiter

DEXSCREENER_BASE = "https://api.dexscreener.com"
GECKOTERMINAL_BASE = "https://api.geckoterminal.com/api/v2"

CHAIN_MAP_GECKO = {
    "ethereum": "eth",
    "bsc": "bsc",
    "arbitrum": "arbitrum",
    "solana": "solana",
}

CHAIN_MAP_DEXSCREENER = {
    "ethereum": "ethereum",
    "bsc": "bsc",
    "arbitrum": "arbitrum",
    "solana": "solana",
}


class TokenScanner:
    def __init__(self):
        self._session: aiohttp.ClientSession | None = None
        self._limiter = RateLimiter(rate=2, burst=3)
        self._seen_pairs: dict[str, float] = {}

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = create_session()
        return self._session

    async def _get(self, url: str) -> dict | list | None:
        await self._limiter.acquire()
        session = await self._get_session()
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
        except Exception as e:
            log.debug(f"HTTP GET {url}: {e}")
            return None

    # --- DEXScreener ---

    async def dexscreener_search(self, query: str) -> list[dict]:
        data = await self._get(f"{DEXSCREENER_BASE}/latest/dex/search?q={query}")
        if not data:
            return []
        return data.get("pairs", [])

    async def dexscreener_new_pairs(self) -> list[dict]:
        """Latest token profiles (recently listed)."""
        data = await self._get(f"{DEXSCREENER_BASE}/token-profiles/latest/v1")
        return data if isinstance(data, list) else []

    async def dexscreener_boosted(self) -> list[dict]:
        """Tokens with active boosts (paid promotion = potential pump)."""
        data = await self._get(f"{DEXSCREENER_BASE}/token-boosts/top/v1")
        return data if isinstance(data, list) else []

    async def dexscreener_token_pairs(self, chain: str, token_address: str) -> list[dict]:
        chain_id = CHAIN_MAP_DEXSCREENER.get(chain, chain)
        data = await self._get(f"{DEXSCREENER_BASE}/tokens/v1/{chain_id}/{token_address}")
        if not data:
            return []
        return data if isinstance(data, list) else data.get("pairs", [])

    async def dexscreener_pair(self, chain: str, pair_address: str) -> dict | None:
        chain_id = CHAIN_MAP_DEXSCREENER.get(chain, chain)
        data = await self._get(f"{DEXSCREENER_BASE}/pairs/v1/{chain_id}/{pair_address}")
        if not data:
            return None
        pairs = data if isinstance(data, list) else data.get("pairs", [])
        return pairs[0] if pairs else None

    # --- GeckoTerminal ---

    async def gecko_trending_pools(self, chain: str) -> list[dict]:
        chain_id = CHAIN_MAP_GECKO.get(chain, chain)
        data = await self._get(f"{GECKOTERMINAL_BASE}/networks/{chain_id}/trending_pools")
        if not data:
            return []
        return data.get("data", [])

    async def gecko_new_pools(self, chain: str) -> list[dict]:
        chain_id = CHAIN_MAP_GECKO.get(chain, chain)
        data = await self._get(f"{GECKOTERMINAL_BASE}/networks/{chain_id}/new_pools")
        if not data:
            return []
        return data.get("data", [])

    async def gecko_top_gainers(self) -> list[dict]:
        data = await self._get(f"{GECKOTERMINAL_BASE}/tokens/info_recently_updated")
        if not data:
            return []
        return data.get("data", [])

    async def gecko_pool_info(self, chain: str, pool_address: str) -> dict | None:
        chain_id = CHAIN_MAP_GECKO.get(chain, chain)
        data = await self._get(
            f"{GECKOTERMINAL_BASE}/networks/{chain_id}/pools/{pool_address}"
        )
        if not data:
            return None
        return data.get("data")

    # --- Unified discovery ---

    async def discover_candidates(self) -> list[dict]:
        """
        Scan all chains for interesting tokens. Returns normalized list:
        [{symbol, chain, pair_address, token_address, price_usd, volume_24h,
          liquidity_usd, price_change_24h, age_hours, source}]
        """
        candidates = []

        for chain in ["ethereum", "bsc", "arbitrum", "solana"]:
            trending = await self.gecko_trending_pools(chain)
            for pool in trending:
                parsed = self._parse_gecko_pool(pool, chain, "trending")
                if parsed:
                    candidates.append(parsed)

            new_pools = await self.gecko_new_pools(chain)
            for pool in new_pools:
                parsed = self._parse_gecko_pool(pool, chain, "new_pool")
                if parsed:
                    candidates.append(parsed)

        boosted = await self.dexscreener_boosted()
        for token in boosted:
            chain = token.get("chainId", "")
            addr = token.get("tokenAddress", "")
            if chain and addr:
                pairs = await self.dexscreener_token_pairs(chain, addr)
                for pair in pairs[:1]:
                    parsed = self._parse_dexscreener_pair(pair, "boosted")
                    if parsed:
                        candidates.append(parsed)

        seen = set()
        unique = []
        for c in candidates:
            key = f"{c['chain']}_{c.get('token_address', c.get('pair_address', ''))}"
            if key not in seen:
                seen.add(key)
                unique.append(c)

        return unique

    def _parse_gecko_pool(self, pool: dict, chain: str, source: str) -> dict | None:
        attrs = pool.get("attributes", {})
        if not attrs:
            return None

        name = attrs.get("name", "")
        price_usd = self._safe_float(attrs.get("base_token_price_usd"))
        volume_24h = self._safe_float(
            (attrs.get("volume_usd") or {}).get("h24")
        )
        liquidity = self._safe_float(
            attrs.get("reserve_in_usd")
        )
        price_change = self._safe_float(
            (attrs.get("price_change_percentage") or {}).get("h24")
        )
        attrs.get("pool_created_at", "")
        address = attrs.get("address", "")

        base_token = (pool.get("relationships", {}).get("base_token", {}).get("data", {}).get("id", ""))
        token_address = base_token.split("_")[-1] if "_" in base_token else ""

        symbol_parts = name.split(" / ") if " / " in name else name.split("/")
        symbol = symbol_parts[0].strip() if symbol_parts else name

        return {
            "symbol": symbol,
            "chain": chain,
            "pair_address": address,
            "token_address": token_address,
            "price_usd": price_usd,
            "volume_24h": volume_24h,
            "liquidity_usd": liquidity,
            "price_change_24h": price_change,
            "source": source,
        }

    def _parse_dexscreener_pair(self, pair: dict, source: str) -> dict | None:
        if not pair:
            return None
        chain = pair.get("chainId", "")
        return {
            "symbol": pair.get("baseToken", {}).get("symbol", "?"),
            "chain": chain,
            "pair_address": pair.get("pairAddress", ""),
            "token_address": pair.get("baseToken", {}).get("address", ""),
            "price_usd": self._safe_float(pair.get("priceUsd")),
            "volume_24h": self._safe_float((pair.get("volume") or {}).get("h24")),
            "liquidity_usd": self._safe_float((pair.get("liquidity") or {}).get("usd")),
            "price_change_24h": self._safe_float(
                (pair.get("priceChange") or {}).get("h24")
            ),
            "source": source,
        }

    @staticmethod
    def _safe_float(val) -> float:
        if val is None:
            return 0.0
        try:
            return float(val)
        except (ValueError, TypeError):
            return 0.0

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
