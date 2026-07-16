"""
Strategy S4: Correlation Subset-Superset Arbitrage
Description: Exploits logical probability contradictions between related prediction markets.
If Event A is a subset of Event B (A ⊆ B), then P(A) must be <= P(B).
If child (A) YES price is higher than parent (B) YES price, we buy the underpriced parent YES.
"""
import uuid
import json
from typing import List
import httpx
from strategies.base import BaseStrategy, parse_list, days_to_expiry, get_market_url, calculate_execution_price
from db.config import cfg
from services.gas_tracker import gas_tracker

class CorrelationArbitrageStrategy(BaseStrategy):
    def __init__(self):
        super().__init__(
            key="s4_corr",
            name="Correlation Arb",
            risk_level="Low",
            market_type="Binary",
            default_exec_mode="auto"
        )

    async def scan(self, markets: List[dict], balance: float, http_client: httpx.AsyncClient) -> List[dict]:
        enabled = await cfg.get_typed("s4_corr.enabled", bool, True)
        if not enabled:
            return []

        min_gap_pct = await cfg.get_typed("s4_corr.min_gap_pct", float, 1.0)
        max_pos_pct = await cfg.get_typed("s4_corr.max_position_pct", float, 0.05)
        exec_mode = await cfg.get_typed("s4_corr.exec_mode", str, "auto")

        # Load correlation rules
        raw_rules = await cfg.get_async("s4_corr.correlation_rules", "[]")
        try:
            rules = json.loads(raw_rules)
            if not isinstance(rules, list) or len(rules) == 0:
                raise ValueError()
        except Exception:
            rules = [
                ("afc wins", "chiefs win"),
                ("nfc wins", "cowboys win"),
                ("nfc wins", "eagles win"),
                ("will happen in 2026", "will happen before july 2026"),
                ("will happen in 2026", "will happen in h1 2026"),
                ("will happen in 2026", "will happen in q1 2026"),
                ("republican wins", "trump wins"),
                ("democrat wins", "biden wins"),
                ("democrat wins", "harris wins"),
            ]

        # Index markets by question
        mkt_index = {}
        for m in markets:
            q = (m.get("question") or "").lower()
            outcomes = parse_list(m.get("outcomes"))
            prices = parse_list(m.get("outcomePrices"))
            token_ids = parse_list(m.get("clobTokenIds"))
            if len(outcomes) == 2 and len(prices) == 2:
                yes_idx = next((i for i, o in enumerate(outcomes) if "yes" in o.lower()), 0)
                yes_price = float(prices[yes_idx])
                mkt_index[q] = {
                    "market": m,
                    "yes_price": yes_price,
                    "token_id": token_ids[yes_idx] if yes_idx < len(token_ids) else None,
                }

        opps = []
        scan_gas_usdc = await gas_tracker.get_gas_cost_usdc()

        for parent_kw, child_kw in rules:
            parent_mkts = [(q, v) for q, v in mkt_index.items() if parent_kw in q]
            child_mkts = [(q, v) for q, v in mkt_index.items() if child_kw in q]

            for pq, pv in parent_mkts:
                for cq, cv in child_mkts:
                    parent_yes = pv["yes_price"]
                    child_yes = cv["yes_price"]

                    # Violation check: child YES > parent YES
                    if child_yes <= parent_yes:
                        continue

                    gap_pct = (child_yes - parent_yes) * 100
                    if gap_pct < min_gap_pct:
                        continue

                    # Sizing & Execution book walk on the underpriced parent market
                    suggested_usdc = max(0.50, balance * max_pos_pct)
                    token_id_parent = pv["token_id"]
                    if not token_id_parent:
                        continue

                    exec_data = await calculate_execution_price(token_id_parent, suggested_usdc, side="buy", http_client=http_client)
                    if "error" in exec_data:
                        continue

                    real_parent_yes = exec_data["price"]
                    slippage = exec_data.get("slippage", 0)

                    # Recalculate gap using actual fill price
                    actual_gap_pct = (child_yes - real_parent_yes) * 100
                    gas_impact = (scan_gas_usdc / suggested_usdc) * 100
                    net_gap_pct = actual_gap_pct - gas_impact

                    if net_gap_pct < min_gap_pct:
                        continue

                    days = days_to_expiry(pv["market"].get("endDate"))
                    market_url = get_market_url(pv["market"])

                    opps.append({
                        "id": f"s4_{uuid.uuid4().hex[:8]}",
                        "strategy": self.key,
                        "market_id": str(pv["market"].get("id", "")),
                        "market_title": pv["market"].get("question", ""),
                        "market_url": market_url,
                        "outcome": "Yes",
                        "entry_price": round(real_parent_yes, 4),
                        "yes_price": round(parent_yes, 4),
                        "no_price": round(1.0 - parent_yes, 4),
                        "implied_prob": round(real_parent_yes * 100, 2),
                        "slippage_bps": round(slippage * 100, 2),
                        "annualized_apy": round(net_gap_pct * (365.0 / max(0.1, days)), 2) if days else None,
                        "profit_pct": round(net_gap_pct, 2),
                        "days_to_expiry": round(days, 1) if days else None,
                        "action": "buy_yes",
                        "exec_mode": exec_mode,
                        "suggested_usdc": round(suggested_usdc, 2),
                        "token_id": token_id_parent,
                        "status": "open",
                        "notes": f"Child '{child_kw}' YES at {child_yes:.1%} > Parent '{parent_kw}' YES at {parent_yes:.1%}. Gap: {gap_pct:.2f}%.",
                        "instructions": [
                            f"Open parent: {market_url}",
                            f"Buy YES on parent market for ${round(suggested_usdc, 2)} USDC",
                            f"Child market '{cq}' is priced higher, implying a logical anomaly. Hold until gap converges."
                        ]
                    })

        opps.sort(key=lambda x: x["profit_pct"], reverse=True)
        return opps

    async def execute(self, opportunity: dict, clob_client) -> dict:
        """S4 Order execution: Buy parent YES token."""
        from py_clob_client.clob_types import OrderArgs
        try:
            token_id = opportunity.get("token_id")
            price = float(opportunity["entry_price"])
            usdc = float(opportunity["suggested_usdc"])
            shares = round(usdc / price, 2)

            order = clob_client.create_order(OrderArgs(
                price=price, size=shares, side="BUY", token_id=token_id
            ))
            resp = clob_client.post_order(order)
            return {"success": True, "order_id": resp.get("orderID")}
        except Exception as e:
            return {"success": False, "error": str(e)}
