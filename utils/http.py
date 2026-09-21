import aiohttp
from aiohttp.resolver import ThreadedResolver


def create_session(**kwargs) -> aiohttp.ClientSession:
    connector = aiohttp.TCPConnector(resolver=ThreadedResolver())
    defaults = {
        "connector": connector,
        "timeout": aiohttp.ClientTimeout(total=15),
        "headers": {"Accept": "application/json"},
    }
    defaults.update(kwargs)
    return aiohttp.ClientSession(**defaults)
