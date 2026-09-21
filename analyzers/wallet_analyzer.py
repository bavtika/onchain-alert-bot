"""
Wallet relationship analyzer:
- Clusters wallets that interact frequently
- Detects coordinated buys (multiple wallets buying same token in short window)
- Tracks wallet-to-wallet transfers to find insider groups
- Identifies "smart money" wallets by tracking profitable patterns
"""
import time
from collections import defaultdict


class WalletAnalyzer:
    def __init__(self, db_module):
        self.db = db_module
        self._wallet_scores: dict[str, float] = {}
        self._wallet_labels: dict[str, str] = {}
        self._clusters: dict[str, set[str]] = {}
        self._buy_history: dict[str, list[dict]] = defaultdict(list)

    async def init_tables(self) -> None:
        db = await self.db.get_db()
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS wallet_activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                chain TEXT NOT NULL,
                action TEXT NOT NULL,
                token_address TEXT,
                token_symbol TEXT,
                value_usd REAL,
                tx_hash TEXT,
                ts REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_wallet_ts ON wallet_activity(wallet, ts);
            CREATE INDEX IF NOT EXISTS idx_token_ts ON wallet_activity(token_address, ts);

            CREATE TABLE IF NOT EXISTS wallet_scores (
                wallet TEXT PRIMARY KEY,
                chain TEXT NOT NULL,
                score REAL DEFAULT 50,
                total_profit_usd REAL DEFAULT 0,
                total_trades INTEGER DEFAULT 0,
                win_rate REAL DEFAULT 0,
                avg_hold_time_hours REAL DEFAULT 0,
                label TEXT DEFAULT '',
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS wallet_clusters (
                cluster_id TEXT NOT NULL,
                wallet TEXT NOT NULL,
                chain TEXT NOT NULL,
                confidence REAL DEFAULT 0,
                created_at REAL NOT NULL,
                PRIMARY KEY (cluster_id, wallet)
            );

            CREATE TABLE IF NOT EXISTS coordinated_buys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_address TEXT NOT NULL,
                token_symbol TEXT,
                chain TEXT NOT NULL,
                wallets TEXT NOT NULL,
                time_window_seconds REAL,
                total_value_usd REAL,
                ts REAL NOT NULL
            );
        """)
        await db.commit()

    async def record_activity(
        self, wallet: str, chain: str, action: str,
        token_address: str = "", token_symbol: str = "",
        value_usd: float = 0, tx_hash: str = ""
    ) -> None:
        db = await self.db.get_db()
        await db.execute(
            "INSERT INTO wallet_activity (wallet,chain,action,token_address,token_symbol,value_usd,tx_hash,ts) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (wallet.lower(), chain, action, token_address.lower(), token_symbol, value_usd, tx_hash, time.time()),
        )
        await db.commit()

        self._buy_history[token_address.lower()].append({
            "wallet": wallet.lower(),
            "chain": chain,
            "action": action,
            "value_usd": value_usd,
            "ts": time.time(),
        })

    async def detect_coordinated_buys(
        self, token_address: str, time_window: float = 300
    ) -> dict | None:
        """
        Detect if multiple wallets bought the same token within a short time window.
        Returns cluster info if suspicious coordination detected.
        """
        token_addr = token_address.lower()
        recent = self._buy_history.get(token_addr, [])

        now = time.time()
        window_buys = [b for b in recent if b["action"] == "buy" and now - b["ts"] < time_window]

        if len(window_buys) < 3:
            return None

        wallets = list(set(b["wallet"] for b in window_buys))
        if len(wallets) < 3:
            return None

        total_value = sum(b["value_usd"] for b in window_buys)
        first_ts = min(b["ts"] for b in window_buys)
        last_ts = max(b["ts"] for b in window_buys)
        window_actual = last_ts - first_ts

        cluster_id = f"coord_{token_addr[:8]}_{int(first_ts)}"
        self._clusters[cluster_id] = set(wallets)

        db = await self.db.get_db()
        await db.execute(
            "INSERT INTO coordinated_buys (token_address,token_symbol,chain,wallets,time_window_seconds,total_value_usd,ts) "
            "VALUES (?,?,?,?,?,?,?)",
            (token_addr, "", "", ",".join(wallets), window_actual, total_value, time.time()),
        )
        await db.commit()

        return {
            "cluster_id": cluster_id,
            "token_address": token_addr,
            "wallets": wallets,
            "wallet_count": len(wallets),
            "total_value_usd": total_value,
            "time_window": window_actual,
            "avg_buy_size": total_value / len(wallets) if wallets else 0,
        }

    async def find_wallet_connections(self, wallet: str, chain: str) -> list[dict]:
        """Find wallets that frequently interact with a given wallet."""
        db = await self.db.get_db()
        wallet = wallet.lower()

        cursor = await db.execute(
            "SELECT DISTINCT token_address, token_symbol, ts FROM wallet_activity "
            "WHERE wallet=? AND chain=? AND action='buy' ORDER BY ts DESC LIMIT 50",
            (wallet, chain),
        )
        tokens_bought = await cursor.fetchall()

        connections = defaultdict(lambda: {"count": 0, "tokens": set(), "total_value": 0})

        for row in tokens_bought:
            token_addr = row["token_address"]
            buy_ts = row["ts"]

            cursor2 = await db.execute(
                "SELECT wallet, value_usd, token_symbol FROM wallet_activity "
                "WHERE token_address=? AND chain=? AND action='buy' "
                "AND wallet!=? AND abs(ts - ?) < 600",
                (token_addr, chain, wallet, buy_ts),
            )
            co_buyers = await cursor2.fetchall()

            for co in co_buyers:
                w = co["wallet"]
                connections[w]["count"] += 1
                connections[w]["tokens"].add(co["token_symbol"] or token_addr[:10])
                connections[w]["total_value"] += co["value_usd"] or 0

        result = []
        for w, info in sorted(connections.items(), key=lambda x: -x[1]["count"]):
            if info["count"] >= 2:
                result.append({
                    "wallet": w,
                    "co_buy_count": info["count"],
                    "common_tokens": list(info["tokens"])[:10],
                    "total_value": info["total_value"],
                    "confidence": min(1.0, info["count"] / 10),
                })

        return result[:20]

    async def get_wallet_profile(self, wallet: str, chain: str) -> dict:
        """Build a profile of a wallet's trading history."""
        db = await self.db.get_db()
        wallet = wallet.lower()

        cursor = await db.execute(
            "SELECT action, token_symbol, value_usd, ts FROM wallet_activity "
            "WHERE wallet=? AND chain=? ORDER BY ts DESC LIMIT 200",
            (wallet, chain),
        )
        activities = await cursor.fetchall()

        if not activities:
            return {"wallet": wallet, "chain": chain, "activity_count": 0}

        buys = [a for a in activities if a["action"] == "buy"]
        sells = [a for a in activities if a["action"] == "sell"]
        total_bought = sum(a["value_usd"] or 0 for a in buys)
        total_sold = sum(a["value_usd"] or 0 for a in sells)

        unique_tokens = set(a["token_symbol"] for a in activities if a["token_symbol"])

        first_seen = min(a["ts"] for a in activities)
        last_seen = max(a["ts"] for a in activities)

        return {
            "wallet": wallet,
            "chain": chain,
            "activity_count": len(activities),
            "buy_count": len(buys),
            "sell_count": len(sells),
            "total_bought_usd": total_bought,
            "total_sold_usd": total_sold,
            "pnl_estimate": total_sold - total_bought,
            "unique_tokens": len(unique_tokens),
            "tokens": list(unique_tokens)[:20],
            "first_seen": first_seen,
            "last_seen": last_seen,
            "active_days": (last_seen - first_seen) / 86400,
        }

    async def update_smart_money_scores(self) -> list[dict]:
        """
        Recalculate which wallets are 'smart money' based on their trading results.
        Returns top wallets by score.
        """
        db = await self.db.get_db()

        cursor = await db.execute(
            "SELECT DISTINCT wallet, chain FROM wallet_activity WHERE ts > ?",
            (time.time() - 7 * 86400,),
        )
        active_wallets = await cursor.fetchall()

        top_wallets = []
        for row in active_wallets:
            profile = await self.get_wallet_profile(row["wallet"], row["chain"])
            if profile["activity_count"] < 5:
                continue

            score = 50.0
            if profile["pnl_estimate"] > 10000:
                score += 30
            elif profile["pnl_estimate"] > 1000:
                score += 15
            elif profile["pnl_estimate"] < -5000:
                score -= 20

            if profile["unique_tokens"] > 20:
                score += 10
            if profile["buy_count"] > 0 and profile["sell_count"] / max(1, profile["buy_count"]) > 0.8:
                score += 10

            score = max(0, min(100, score))

            await db.execute(
                "INSERT OR REPLACE INTO wallet_scores (wallet,chain,score,total_profit_usd,total_trades,label,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (row["wallet"], row["chain"], score, profile["pnl_estimate"],
                 profile["activity_count"], "", time.time()),
            )

            if score >= 70:
                top_wallets.append({
                    "wallet": row["wallet"],
                    "chain": row["chain"],
                    "score": score,
                    "pnl": profile["pnl_estimate"],
                    "trades": profile["activity_count"],
                })

        await db.commit()
        return sorted(top_wallets, key=lambda x: -x["score"])[:20]

    async def get_smart_money_wallets(self) -> list[dict]:
        """Get currently tracked smart money wallets."""
        db = await self.db.get_db()
        cursor = await db.execute(
            "SELECT wallet, chain, score, total_profit_usd, total_trades "
            "FROM wallet_scores WHERE score >= 70 ORDER BY score DESC LIMIT 50"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    def cleanup_old_buys(self, max_age: float = 3600) -> None:
        cutoff = time.time() - max_age
        for token in list(self._buy_history.keys()):
            self._buy_history[token] = [
                b for b in self._buy_history[token] if b["ts"] > cutoff
            ]
            if not self._buy_history[token]:
                del self._buy_history[token]
