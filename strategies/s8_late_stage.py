"""
Strategy S8: Late-Stage Resolution Yield
Description: Scans for short-duration binary markets where the outcome is practically decided (>98%) but not yet resolved.
"""
from typing import List
import httpx
from strategies.base import BaseStrategy, parse_list, days_to_expiry, get_market_url
from db.config import cfg

class LateStageStrategy(BaseStrategy):
    def __init__(self):
        super().__init__(
            key="s8_late_stage",
            name="Late Stage Yield",
            risk_level="Low",
            market_type="Binary",
            default_exec_mode="auto"
        )

    async def scan(self, markets: List[dict], balance: float, http_client: httpx.AsyncClient) -> List[dict]:
        enabled = await cfg.get_typed(f"{self.key}.enabled", bool, True)
        if not enabled:
            return []
        
        # Scans for markets priced >= 0.98 with < 3 days left
        opps = []
        for market in markets:
            end_dt = market.get("endDate") or market.get("end_date_iso")
            days = days_to_expiry(end_dt)
            if days is None or days <= 0 or days > 3:
                continue
            
            prices = parse_list(market.get("outcomePrices"))
            outcomes = parse_list(market.get("outcomes"))
            if len(prices) != 2:
                continue
                
            for i, p_str in enumerate(prices):
                p = float(p_str)
                if 0.98 <= p < 0.999:
                    opps.append({
                        "id": f"{self.key}_{market.get('id', '')}",
                        "strategy": self.key,
                        "market_id": str(market.get("id", "")),
                        "market_title": market.get("question", ""),
                        "market_url": get_market_url(market),
                        "outcome": outcomes[i] if i < len(outcomes) else "YES",
                        "entry_price": p,
                        "implied_prob": round(p * 100, 2),
                        "annualized_apy": round(((1.0 / p) ** (365 / max(1, days)) - 1) * 100, 2),
                        "days_to_expiry": round(days, 1),
                        "risk_level": self.risk_level,
                        "exec_mode": await cfg.get_typed(f"{self.key}.exec_mode", str, "auto"),
                        "suggested_usdc": 10.0,
                        "action": "BUY",
                        "status": "open"
                    })
        return opps

    async def execute(self, opportunity: dict, clob_client) -> dict:
        return {"success": False, "error": "Not fully implemented"}
