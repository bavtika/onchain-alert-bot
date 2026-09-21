import aiohttp
import ccxt.async_support as ccxt
from aiohttp.resolver import ThreadedResolver

import config
from utils.logger import log
from utils.rate_limiter import RateLimiter


class CexProvider:
    def __init__(self):
        self._spot: dict[str, ccxt.Exchange] = {}
        self._swap: dict[str, ccxt.Exchange] = {}
        self._limiter = RateLimiter(rate=config.RATE_LIMITS["cex"], burst=3)
        self._markets_loaded: set[str] = set()
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(resolver=ThreadedResolver())
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    def _get_spot(self, name: str) -> ccxt.Exchange:
        if name not in self._spot:
            cls = getattr(ccxt, name)
            self._spot[name] = cls({
                "enableRateLimit": True,
                "options": {"defaultType": "spot"},
                "session": self._get_session(),
            })
        return self._spot[name]

    def _get_swap(self, name: str) -> ccxt.Exchange:
        if name not in self._swap:
            cls = getattr(ccxt, name)
            self._swap[name] = cls({
                "enableRateLimit": True,
                "options": {"defaultType": "swap"},
                "session": self._get_session(),
            })
        return self._swap[name]

    def _get_exchange(self, name: str) -> ccxt.Exchange:
        return self._get_spot(name)

    async def _ensure_markets(self, ex: ccxt.Exchange, key: str) -> None:
        if key not in self._markets_loaded:
            await self._limiter.acquire()
            await ex.load_markets()
            self._markets_loaded.add(key)

    # --- Bulk ---

    async def get_all_tickers_spot(self, exchange: str) -> dict[str, dict]:
        await self._limiter.acquire()
        try:
            ex = self._get_spot(exchange)
            await self._ensure_markets(ex, f"{exchange}_spot")
            return await ex.fetch_tickers()
        except Exception as e:
            log.debug(f"Bulk tickers spot {exchange}: {e}")
            return {}

    async def get_all_tickers_swap(self, exchange: str) -> dict[str, dict]:
        await self._limiter.acquire()
        try:
            ex = self._get_swap(exchange)
            await self._ensure_markets(ex, f"{exchange}_swap")
            return await ex.fetch_tickers()
        except Exception as e:
            log.debug(f"Bulk tickers swap {exchange}: {e}")
            return {}

    # --- Spot ---

    async def get_ticker(self, exchange: str, symbol: str) -> dict | None:
        await self._limiter.acquire()
        try:
            is_swap = ":USDT" in symbol or ":USD" in symbol
            ex = self._get_swap(exchange) if is_swap else self._get_spot(exchange)
            await self._ensure_markets(ex, f"{exchange}_{'swap' if is_swap else 'spot'}")
            return await ex.fetch_ticker(symbol)
        except Exception as e:
            log.debug(f"Ticker {symbol} on {exchange}: {e}")
            return None

    async def get_orderbook(self, exchange: str, symbol: str, limit: int = 20) -> dict | None:
        await self._limiter.acquire()
        try:
            ex = self._get_spot(exchange)
            await self._ensure_markets(ex, f"{exchange}_spot")
            return await ex.fetch_order_book(symbol, limit=limit)
        except Exception as e:
            log.debug(f"Orderbook {symbol} on {exchange}: {e}")
            return None

    async def get_ohlcv(self, exchange: str, symbol: str, timeframe: str = "1h", limit: int = 24) -> list:
        await self._limiter.acquire()
        try:
            is_swap = ":USDT" in symbol or ":USD" in symbol
            ex = self._get_swap(exchange) if is_swap else self._get_spot(exchange)
            await self._ensure_markets(ex, f"{exchange}_{'swap' if is_swap else 'spot'}")
            return await ex.fetch_ohlcv(symbol, timeframe, limit=limit)
        except Exception as e:
            log.debug(f"OHLCV {symbol} on {exchange}: {e}")
            return []

    async def get_price(self, exchange: str, symbol: str) -> float | None:
        ticker = await self.get_ticker(exchange, symbol)
        if ticker and ticker.get("last"):
            return float(ticker["last"])
        return None

    # --- Futures / Swap ---

    async def get_funding_rates(self, exchange: str) -> list[dict]:
        await self._limiter.acquire()
        try:
            ex = self._get_swap(exchange)
            await self._ensure_markets(ex, f"{exchange}_swap")

            if ex.has.get("fetchFundingRates"):
                rates = await ex.fetch_funding_rates()
                return list(rates.values())

            swap_symbols = [s for s in ex.symbols if ex.market(s).get("swap")][:50]
            rates = {}
            for sym in swap_symbols:
                await self._limiter.acquire()
                try:
                    r = await ex.fetch_funding_rate(sym)
                    if r:
                        rates[sym] = r
                except Exception:
                    continue
            return list(rates.values())
        except Exception as e:
            log.error(f"Funding rates {exchange}: {e}")
            return []

    async def get_open_interest(self, exchange: str, symbol: str) -> dict | None:
        await self._limiter.acquire()
        try:
            ex = self._get_swap(exchange)
            await self._ensure_markets(ex, f"{exchange}_swap")
            if not ex.has.get("fetchOpenInterest"):
                return None
            return await ex.fetch_open_interest(symbol)
        except Exception as e:
            log.debug(f"OI {symbol} on {exchange}: {e}")
            return None

    async def get_open_interest_history(self, exchange: str, symbol: str, timeframe: str = "1h", limit: int = 24) -> list:
        await self._limiter.acquire()
        try:
            ex = self._get_swap(exchange)
            await self._ensure_markets(ex, f"{exchange}_swap")
            if not ex.has.get("fetchOpenInterestHistory"):
                return []
            return await ex.fetch_open_interest_history(symbol, timeframe, limit=limit)
        except Exception as e:
            log.debug(f"OI history {symbol} on {exchange}: {e}")
            return []

    async def get_all_swap_symbols(self, exchange: str) -> list[str]:
        try:
            ex = self._get_swap(exchange)
            await self._ensure_markets(ex, f"{exchange}_swap")
            return [s for s in ex.symbols if ex.market(s).get("swap")]
        except Exception as e:
            log.debug(f"Load swap markets {exchange}: {e}")
            return []

    async def get_all_spot_symbols(self, exchange: str) -> list[str]:
        try:
            ex = self._get_spot(exchange)
            await self._ensure_markets(ex, f"{exchange}_spot")
            return [s for s in ex.symbols if ex.market(s).get("spot") and "/USDT" in s]
        except Exception as e:
            log.debug(f"Load spot markets {exchange}: {e}")
            return []

    async def close(self) -> None:
        for ex in list(self._spot.values()) + list(self._swap.values()):
            try:
                await ex.close()
            except Exception:
                pass
        self._spot.clear()
        self._swap.clear()
        self._markets_loaded.clear()
        if self._session and not self._session.closed:
            await self._session.close()
