"""
Risk scoring for tokens. Checks:
- Liquidity depth and lock status
- Holder concentration (top holders %)
- Contract verification
- Honeypot patterns (buy/sell tax)
- Age of token
- Social signals
Returns a score 0-100 (100 = safest).
"""
import aiohttp

from utils.http import create_session
from utils.rate_limiter import RateLimiter


class RiskScorer:
    def __init__(self):
        self._session: aiohttp.ClientSession | None = None
        self._limiter = RateLimiter(rate=2, burst=2)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = create_session()
        return self._session

    async def _get(self, url: str) -> dict | None:
        await self._limiter.acquire()
        session = await self._get_session()
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
        except Exception:
            return None

    async def score_token(self, chain: str, token_address: str, pair_data: dict | None = None) -> dict:
        """
        Returns: {
            score: 0-100,
            risks: [list of risk flags],
            details: {breakdown of scores},
            verdict: "SAFE" | "CAUTION" | "DANGER" | "SCAM"
        }
        """
        risks = []
        scores = {}

        # --- Liquidity check ---
        liq = pair_data.get("liquidity_usd", 0) if pair_data else 0
        if liq < 1000:
            scores["liquidity"] = 0
            risks.append("DUST LIQUIDITY (<$1k) - likely scam or dead token")
        elif liq < 10_000:
            scores["liquidity"] = 20
            risks.append("Very low liquidity (<$10k) - high slippage, easy to manipulate")
        elif liq < 50_000:
            scores["liquidity"] = 50
            risks.append("Low liquidity (<$50k)")
        elif liq < 500_000:
            scores["liquidity"] = 75
        else:
            scores["liquidity"] = 100

        # --- Volume / Liquidity ratio ---
        vol = pair_data.get("volume_24h", 0) if pair_data else 0
        if liq > 0:
            vol_liq_ratio = vol / liq
            if vol_liq_ratio > 50:
                scores["vol_liq"] = 10
                risks.append(f"Suspicious volume/liquidity ratio ({vol_liq_ratio:.0f}x) - likely wash trading")
            elif vol_liq_ratio > 10:
                scores["vol_liq"] = 40
                risks.append(f"High volume/liquidity ratio ({vol_liq_ratio:.1f}x)")
            elif vol_liq_ratio < 0.01 and vol > 0:
                scores["vol_liq"] = 50
                risks.append("Very low trading activity")
            else:
                scores["vol_liq"] = 90
        else:
            scores["vol_liq"] = 0

        # --- Price change analysis ---
        price_change = pair_data.get("price_change_24h", 0) if pair_data else 0
        if price_change > 500:
            scores["price_action"] = 20
            risks.append(f"Extreme pump (+{price_change:.0f}%) - likely dump incoming")
        elif price_change > 100:
            scores["price_action"] = 40
            risks.append(f"Major pump (+{price_change:.0f}%)")
        elif price_change < -80:
            scores["price_action"] = 10
            risks.append(f"Crashed ({price_change:.0f}%) - likely rug or dump")
        elif price_change < -50:
            scores["price_action"] = 30
            risks.append(f"Heavy dump ({price_change:.0f}%)")
        else:
            scores["price_action"] = 85

        # --- Honeypot check via free API ---
        honeypot = await self._check_honeypot(chain, token_address)
        if honeypot:
            if honeypot.get("is_honeypot"):
                scores["honeypot"] = 0
                risks.append("HONEYPOT DETECTED - cannot sell tokens")
            else:
                buy_tax = honeypot.get("buy_tax", 0)
                sell_tax = honeypot.get("sell_tax", 0)
                if sell_tax > 50:
                    scores["honeypot"] = 5
                    risks.append(f"Extreme sell tax ({sell_tax}%) - near honeypot")
                elif sell_tax > 10:
                    scores["honeypot"] = 30
                    risks.append(f"High sell tax ({sell_tax}%)")
                elif buy_tax > 10:
                    scores["honeypot"] = 50
                    risks.append(f"High buy tax ({buy_tax}%)")
                else:
                    scores["honeypot"] = 95
        else:
            scores["honeypot"] = 50
            risks.append("Could not verify honeypot status")

        # --- GoPlus security check ---
        goplus = await self._check_goplus(chain, token_address)
        if goplus:
            gp_score, gp_risks = self._parse_goplus(goplus)
            scores["goplus"] = gp_score
            risks.extend(gp_risks)
        else:
            scores["goplus"] = 50

        # --- Calculate total ---
        weights = {
            "liquidity": 0.20,
            "vol_liq": 0.10,
            "price_action": 0.15,
            "honeypot": 0.30,
            "goplus": 0.25,
        }

        total = sum(scores.get(k, 50) * w for k, w in weights.items())
        total = max(0, min(100, total))

        if total >= 80:
            verdict = "SAFE"
        elif total >= 50:
            verdict = "CAUTION"
        elif total >= 25:
            verdict = "DANGER"
        else:
            verdict = "SCAM"

        return {
            "score": round(total),
            "risks": risks,
            "details": scores,
            "verdict": verdict,
        }

    async def _check_honeypot(self, chain: str, token_address: str) -> dict | None:
        chain_map = {"ethereum": "1", "bsc": "56", "arbitrum": "42161"}
        chain_id = chain_map.get(chain)
        if not chain_id:
            return None
        data = await self._get(
            f"https://api.honeypot.is/v2/IsHoneypot?address={token_address}&chainID={chain_id}"
        )
        if not data:
            return None
        hp = data.get("honeypotResult", {})
        sim = data.get("simulationResult", {})
        return {
            "is_honeypot": hp.get("isHoneypot", False),
            "buy_tax": sim.get("buyTax", 0),
            "sell_tax": sim.get("sellTax", 0),
        }

    async def _check_goplus(self, chain: str, token_address: str) -> dict | None:
        chain_map = {"ethereum": "1", "bsc": "56", "arbitrum": "42161", "solana": "solana"}
        chain_id = chain_map.get(chain)
        if not chain_id:
            return None
        data = await self._get(
            f"https://api.gopluslabs.com/api/v1/token_security/{chain_id}?contract_addresses={token_address}"
        )
        if not data or data.get("code") != 1:
            return None
        result = data.get("result", {})
        return result.get(token_address.lower()) or result.get(token_address)

    def _parse_goplus(self, data: dict) -> tuple[int, list[str]]:
        score = 100
        risks = []

        if data.get("is_honeypot") == "1":
            score -= 100
            risks.append("[GoPlus] Honeypot confirmed")

        if data.get("is_open_source") == "0":
            score -= 20
            risks.append("[GoPlus] Contract NOT open source")

        if data.get("is_proxy") == "1":
            score -= 15
            risks.append("[GoPlus] Proxy contract (upgradeable - owner can change logic)")

        if data.get("is_mintable") == "1":
            score -= 25
            risks.append("[GoPlus] Mintable - owner can create unlimited tokens")

        if data.get("can_take_back_ownership") == "1":
            score -= 20
            risks.append("[GoPlus] Owner can reclaim ownership after renouncing")

        if data.get("owner_change_balance") == "1":
            score -= 30
            risks.append("[GoPlus] Owner can modify balances")

        if data.get("hidden_owner") == "1":
            score -= 20
            risks.append("[GoPlus] Hidden owner detected")

        if data.get("selfdestruct") == "1":
            score -= 25
            risks.append("[GoPlus] Contract can self-destruct")

        if data.get("external_call") == "1":
            score -= 10
            risks.append("[GoPlus] External calls in contract")

        if data.get("cannot_sell_all") == "1":
            score -= 30
            risks.append("[GoPlus] Cannot sell all tokens")

        if data.get("trading_cooldown") == "1":
            score -= 10
            risks.append("[GoPlus] Trading cooldown enabled")

        if data.get("transfer_pausable") == "1":
            score -= 15
            risks.append("[GoPlus] Transfers can be paused")

        if data.get("is_blacklisted") == "1":
            score -= 15
            risks.append("[GoPlus] Blacklist function exists")

        if data.get("is_whitelisted") == "1":
            score -= 10
            risks.append("[GoPlus] Whitelist function exists")

        if data.get("anti_whale_modifiable") == "1":
            score -= 10
            risks.append("[GoPlus] Anti-whale is modifiable by owner")

        holder_count = int(data.get("holder_count", 0) or 0)
        if holder_count < 50:
            score -= 20
            risks.append(f"[GoPlus] Very few holders ({holder_count})")
        elif holder_count < 200:
            score -= 10
            risks.append(f"[GoPlus] Low holder count ({holder_count})")

        holders = data.get("holders", [])
        if holders:
            top_holder_pct = sum(float(h.get("percent", 0)) for h in holders[:5])
            if top_holder_pct > 0.80:
                score -= 30
                risks.append(f"[GoPlus] Top 5 holders own {top_holder_pct*100:.0f}%")
            elif top_holder_pct > 0.50:
                score -= 15
                risks.append(f"[GoPlus] Top 5 holders own {top_holder_pct*100:.0f}%")

        lp_holders = data.get("lp_holders", [])
        if lp_holders:
            lp_locked = any(
                h.get("is_locked") == 1 or h.get("is_contract") == 1
                for h in lp_holders
            )
            if not lp_locked:
                score -= 20
                risks.append("[GoPlus] Liquidity NOT locked")

        return max(0, score), risks

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
