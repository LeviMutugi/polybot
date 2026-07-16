"""
Strategy S11: Overreaction Reversal Scalping
Description: Detects rapid price spikes triggered by noise and scalps the mean reversion.
"""
from typing import List
import httpx
from strategies.base import BaseStrategy
from db.config import cfg

class OverreactionStrategy(BaseStrategy):
    def __init__(self):
        super().__init__(
            key="s11_overreaction",
            name="Overreaction Scalp",
            risk_level="Medium",
            market_type="Binary",
            default_exec_mode="semi"
        )

    async def scan(self, markets: List[dict], balance: float, http_client: httpx.AsyncClient) -> List[dict]:
        enabled = await cfg.get_typed(f"{self.key}.enabled", bool, True)
        if not enabled:
            return []
        return []

    async def execute(self, opportunity: dict, clob_client) -> dict:
        return {"success": False, "error": "Not fully implemented"}
