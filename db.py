import hashlib
import time
from pathlib import Path

import aiosqlite

import config

_db: aiosqlite.Connection | None = None


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        _db = await aiosqlite.connect(config.DB_PATH)
        _db.row_factory = aiosqlite.Row
        await _init_tables(_db)
    return _db


async def _init_tables(db: aiosqlite.Connection) -> None:
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy TEXT NOT NULL,
            symbol TEXT NOT NULL,
            message_hash TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_alerts_lookup ON alerts(strategy, symbol, created_at);

        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            source TEXT NOT NULL,
            price REAL NOT NULL,
            volume REAL NOT NULL,
            ts REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_price_ts ON price_history(symbol, source, ts);

        CREATE TABLE IF NOT EXISTS whale_txns (
            tx_hash TEXT PRIMARY KEY,
            chain TEXT NOT NULL,
            created_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS oi_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            exchange TEXT NOT NULL,
            oi_value REAL NOT NULL,
            oi_change REAL NOT NULL,
            ts REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_oi_ts ON oi_history(symbol, exchange, ts);
    """)
    await db.commit()


async def should_alert(strategy: str, symbol: str, cooldown: float = None) -> bool:
    if cooldown is None:
        cooldown = config.ALERT_COOLDOWN
    db = await get_db()
    cutoff = time.time() - cooldown
    cursor = await db.execute(
        "SELECT 1 FROM alerts WHERE strategy=? AND symbol=? AND created_at>? LIMIT 1",
        (strategy, symbol, cutoff),
    )
    row = await cursor.fetchone()
    return row is None


async def record_alert(strategy: str, symbol: str, message: str) -> None:
    db = await get_db()
    msg_hash = hashlib.md5(message.encode()).hexdigest()
    await db.execute(
        "INSERT INTO alerts (strategy, symbol, message_hash, created_at) VALUES (?,?,?,?)",
        (strategy, symbol, msg_hash, time.time()),
    )
    await db.commit()


async def insert_price_point(symbol: str, source: str, price: float, volume: float) -> None:
    db = await get_db()
    await db.execute(
        "INSERT INTO price_history (symbol, source, price, volume, ts) VALUES (?,?,?,?,?)",
        (symbol, source, price, volume, time.time()),
    )
    await db.commit()


async def get_volume_history(symbol: str, source: str, hours: float = 24) -> list[dict]:
    db = await get_db()
    cutoff = time.time() - hours * 3600
    cursor = await db.execute(
        "SELECT price, volume, ts FROM price_history WHERE symbol=? AND source=? AND ts>? ORDER BY ts",
        (symbol, source, cutoff),
    )
    rows = await cursor.fetchall()
    return [{"price": r["price"], "volume": r["volume"], "ts": r["ts"]} for r in rows]


async def is_tx_seen(tx_hash: str) -> bool:
    db = await get_db()
    cursor = await db.execute("SELECT 1 FROM whale_txns WHERE tx_hash=? LIMIT 1", (tx_hash,))
    return (await cursor.fetchone()) is not None


async def mark_tx_seen(tx_hash: str, chain: str) -> None:
    db = await get_db()
    await db.execute(
        "INSERT OR IGNORE INTO whale_txns (tx_hash, chain, created_at) VALUES (?,?,?)",
        (tx_hash, chain, time.time()),
    )
    await db.commit()


async def insert_oi_point(symbol: str, exchange: str, oi_value: float, oi_change: float) -> None:
    db = await get_db()
    await db.execute(
        "INSERT INTO oi_history (symbol, exchange, oi_value, oi_change, ts) VALUES (?,?,?,?,?)",
        (symbol, exchange, oi_value, oi_change, time.time()),
    )
    await db.commit()


async def get_oi_history(symbol: str, exchange: str, hours: float = 24) -> list[dict]:
    db = await get_db()
    cutoff = time.time() - hours * 3600
    cursor = await db.execute(
        "SELECT oi_value, oi_change, ts FROM oi_history WHERE symbol=? AND exchange=? AND ts>? ORDER BY ts",
        (symbol, exchange, cutoff),
    )
    rows = await cursor.fetchall()
    return [{"oi_value": r["oi_value"], "oi_change": r["oi_change"], "ts": r["ts"]} for r in rows]


async def prune_old_records() -> None:
    db = await get_db()
    now = time.time()
    await db.execute("DELETE FROM alerts WHERE created_at < ?", (now - 86400,))
    await db.execute("DELETE FROM price_history WHERE ts < ?", (now - 86400,))
    await db.execute("DELETE FROM whale_txns WHERE created_at < ?", (now - 21600,))
    await db.execute("DELETE FROM oi_history WHERE ts < ?", (now - 172800,))
    await db.commit()


async def close() -> None:
    global _db
    if _db:
        await _db.close()
        _db = None
