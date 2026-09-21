import os

from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
HEALTH_PORT = int(os.getenv("HEALTH_PORT", "8080"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# --- Free public RPC endpoints ---
EVM_RPCS = {
    "ethereum": [
        "https://eth.llamarpc.com",
        "https://rpc.ankr.com/eth",
        "https://ethereum-rpc.publicnode.com",
    ],
    "bsc": [
        "https://bsc-dataseed1.binance.org",
        "https://rpc.ankr.com/bsc",
        "https://bsc-rpc.publicnode.com",
    ],
    "arbitrum": [
        "https://arb1.arbitrum.io/rpc",
        "https://rpc.ankr.com/arbitrum",
        "https://arbitrum-one-rpc.publicnode.com",
    ],
}

SOLANA_RPCS = [
    "https://api.mainnet-beta.solana.com",
]

# --- Exchanges (public data, no API keys) ---
CEXES = ["binance", "bybit", "mexc"]

# --- Strategy scan intervals (seconds) ---
INTERVALS = {
    "dex_cex_arb": 30,
    "whale_tracker": 15,
    "funding_rate": 300,
    "volume_anomaly": 60,
    "auto_discovery": 120,
    "smart_money": 45,
    "oi_spike": 90,
    "spot_arb": 20,
    "triangular_arb": 15,
    "stablecoin_depeg": 60,
    "liquidation_levels": 120,
    "listing_sniper": 300,
    "orderbook_imbalance": 30,
    "correlation_divergence": 120,
    "exploit_detector": 60,
}

# --- Strategy thresholds (high bar = only real opportunities) ---
ARB_SPREAD_THRESHOLD = 0.05          # 5% DEX/CEX spread (covers gas + slippage)
WHALE_THRESHOLD_USD = 1_000_000      # $1M+ transfers only
FUNDING_RATE_EXTREME = 0.005         # 0.5% per 8h (~66% annualized, really extreme)
VOLUME_SPIKE_MULTIPLIER = 10         # 10x average volume (genuine spike)
PRICE_SPIKE_THRESHOLD = 0.15         # 15% price move in 1h
OI_SPIKE_THRESHOLD = 0.25            # 25% OI change = real spike
OI_DROP_THRESHOLD = -0.25            # -25% OI drop
OI_ALERT_COOLDOWN = 1800             # 30min cooldown per pair

# Spot arb — after fees on both sides
SPOT_ARB_MIN_PROFIT = 0.005          # 0.5% net profit after fees

# Triangular arb
TRIANGULAR_ARB_MIN_PROFIT = 0.003    # 0.3% per cycle ($30 per $10k)

# Stablecoin depeg
DEPEG_THRESHOLD = 0.005              # 0.5% deviation from peg

# Orderbook
OB_IMBALANCE_THRESHOLD = 5.0         # 5x bid/ask ratio (strong signal)
OB_WALL_MIN_USD = 500_000            # $500k minimum wall size

# --- Alert cooldown (seconds) ---
ALERT_COOLDOWN = 1800  # 30 min default cooldown — no spam

# --- Rate limits (requests per second) ---
RATE_LIMITS = {
    "evm": 5,
    "solana": 2,
    "cex": 10,
}

# --- Known exchange hot wallets (lowercase) ---
EXCHANGE_WALLETS = {
    "ethereum": {
        "0x28c6c06298d514db089934071355e5743bf21d60": "Binance Hot",
        "0xdfd5293d8e347dfe59e90efd55b2956a1343963d": "Binance Hot 2",
        "0xf89d7b9c864f589bbf53a82105107622b35eaa40": "Bybit Hot",
        "0x75e89d5979e4f6fba9f97c104c2f0afb3f1dcb88": "MEXC Hot",
    },
    "bsc": {
        "0x8894e0a0c962cb723c1ef8a1b63d28aaa26e8f6f": "Binance Hot BSC",
        "0xe2fc31f816a9b94326492132018c3aecc4a93ae1": "Binance Hot BSC 2",
    },
    "arbitrum": {
        "0xb38e8c17e38363af6ebdcb3dae12e0243582891d": "Binance Hot ARB",
        "0xf89d7b9c864f589bbf53a82105107622b35eaa40": "Bybit Hot ARB",
    },
    "solana": {
        "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9": "Binance Hot SOL",
        "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM": "Bybit Hot SOL",
        "ASTyfSima4LLAdDgoFGkgqoKowG1LZFDr9fAQrg7iaJZ": "MEXC Hot SOL",
    },
}

# --- Watchlist: tokens to monitor for DEX/CEX arb ---
# Format: symbol, chain, dex_pair_address, cex_symbol
WATCHLIST = [
    {
        "symbol": "PEPE",
        "chain": "ethereum",
        "dex_pair": "0xA43fe16908251ee70EF74718545e4FE6C5cCEc9f",  # PEPE/WETH Uniswap V2
        "cex_symbol": "PEPE/USDT",
        "dex_type": "uniswap_v2",
        "token_index": 0,  # token position in pair (0 or 1)
    },
    {
        "symbol": "FLOKI",
        "chain": "bsc",
        "dex_pair": "0x231265dCCC8CDEE5b5b667f522BaA2f08a38D107",  # FLOKI/WBNB PancakeSwap
        "cex_symbol": "FLOKI/USDT",
        "dex_type": "uniswap_v2",
        "token_index": 0,
    },
]

# --- Volume watchlist: tokens to monitor for volume spikes ---
VOLUME_WATCHLIST = [
    "PEPE/USDT",
    "FLOKI/USDT",
    "WIF/USDT",
    "BONK/USDT",
    "DOGE/USDT",
    "SHIB/USDT",
    "ARB/USDT",
    "OP/USDT",
    "SOL/USDT",
    "AVAX/USDT",
]

DB_PATH = "data/alerts.db"
