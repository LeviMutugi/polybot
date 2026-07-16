"""
Strategy S2: Split Share LP Farming
Description: Provides neutral liquidity (tight YES & NO limit orders) on high-reward pools.
Earns daily $POLY maker rewards while remaining delta-neutral.
"""
from typing import List
import httpx
from config import settings
from strategies.base import BaseStrategy, parse_list, get_market_url
from db.config import cfg

class SplitShareFarmingStrategy(BaseStrategy):
    def __init__(self):
        super().__init__(
            key="s2_split",
            name="Split Share Farm",
            risk_level="Low",
            market_type="Binary",
            default_exec_mode="semi"
        )

    async def scan(self, markets: List[dict], balance: float, http_client: httpx.AsyncClient) -> List[dict]:
        enabled = await cfg.get_typed("s2_split.enabled", bool, True)
        if not enabled:
            return []

        min_apy = await cfg.get_typed("s2_split.min_apy", float, 10.0)
        max_pos_pct = await cfg.get_typed("s2_split.max_position_pct", float, 0.05)
        exec_mode = await cfg.get_typed("s2_split.exec_mode", str, "semi")

        clob_url = settings.polymarket_clob_url.rstrip("/")
        opps = []

        try:
            # Query reward statistical API
            resp = await http_client.get(f"{clob_url}/rewards/markets-stats")
            if resp.status_code != 200:
                return []

            reward_markets = resp.json()
        except Exception:
            return []

        for rm in reward_markets:
            try:
                est_apy = float(rm.get("estimated_apy", rm.get("rewardDaily", 0)))
                if est_apy < min_apy:
                    continue

                # Find matching market details
                condition_id = rm.get("condition_id")
                market_meta = next(
                    (m for m in markets if m.get("conditionId") == condition_id or str(m.get("id")) == str(condition_id)),
                    None
                )
                if not market_meta:
                    continue

                prices = parse_list(market_meta.get("outcomePrices", ["0.5", "0.5"]))
                mid_price = float(prices[0]) if prices else 0.5
                max_spread = float(rm.get("max_spread", 0.05))
                min_size = float(rm.get("min_size", 5.0))

                suggested_usdc = max(min_size, balance * max_pos_pct)
                
                # Placement 1c inside spread for max rewards score
                yes_limit = round(mid_price - 0.01, 3)
                no_limit = round((1 - mid_price) - 0.01, 3)
                
                # Reward score math
                dist = 0.01
                reward_score = round(((max_spread - dist) / max_spread) ** 2 * suggested_usdc, 4) if max_spread > 0 else 0.0

                opps.append({
                    "id": f"s2_{condition_id}",
                    "strategy": self.key,
                    "market_id": str(condition_id),
                    "market_title": market_meta.get("question", f"Market {condition_id}"),
                    "market_url": get_market_url(market_meta),
                    "outcome": "YES + NO shares (Neutral LP)",
                    "entry_price": round(mid_price, 4),
                    "yes_price": round(mid_price, 4),
                    "no_price": round(1.0 - mid_price, 4),
                    "implied_prob": 50.0,
                    "annualized_apy": round(est_apy, 2),
                    "profit_pct": round(est_apy / 12, 2),  # Expected monthly return
                    "days_to_expiry": None,
                    "action": "manual",  # Requires manual order placement or custom quoting loops
                    "exec_mode": exec_mode,
                    "suggested_usdc": round(suggested_usdc, 2),
                    "reward_score": reward_score,
                    "notes": f"Est {est_apy:.1f}% APY daily rewards in $POLY. Place YES @ ${yes_limit} and NO @ ${no_limit}.",
                    "status": "open",
                    "instructions": [
                        f"Navigate to Polymarket LP: {get_market_url(market_meta)}",
                        f"Deploy ${round(suggested_usdc, 2)} USDC liquidity centered around mid-price.",
                        f"Place limit orders: YES @ ${yes_limit} and NO @ ${no_limit}.",
                        f"Cancels exposure to direction; harvests yield from trading spread & $POLY rewards."
                    ]
                })
            except Exception:
                continue

        opps.sort(key=lambda x: x["annualized_apy"], reverse=True)
        return opps

    async def execute(self, opportunity: dict, clob_client) -> dict:
        """S2 requires active limit quoting — not a one-shot execution."""
        return {"success": False, "error": "S2 Split Share requires manual limit order placement via Polymarket UI"}
