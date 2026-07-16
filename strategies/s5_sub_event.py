"""
Strategy S5: Sub-Event / Sum Arbitrage
Description: Mispricing between parent event (e.g. "Will X happen in 2026?") and its sub-events
(e.g., "Will X happen in Q1?", "Q2?", "Q3?", "Q4?"). The parent price should approximate the sum of sub-events.
If parent YES is underpriced: we buy parent YES.
If parent YES is overpriced: we buy sub-event YES basket.
"""
import uuid
import asyncio
from typing import List
import httpx
from strategies.base import BaseStrategy, parse_list, days_to_expiry, get_market_url, calculate_execution_price
from db.config import cfg
from services.gas_tracker import gas_tracker

class SubEventArbitrageStrategy(BaseStrategy):
    def __init__(self):
        super().__init__(
            key="s5_sub_event",
            name="Sub-Event Arb",
            risk_level="Medium",
            market_type="Event-based",
            default_exec_mode="manual"
        )

    async def scan(self, markets: List[dict], balance: float, http_client: httpx.AsyncClient) -> List[dict]:
        enabled = await cfg.get_typed("s5_sub_event.enabled", bool, True)
        if not enabled:
            return []

        min_gap_pct = await cfg.get_typed("s5_sub_event.min_gap_pct", float, 1.5)
        max_pos_pct = await cfg.get_typed("s5_sub_event.max_position_pct", float, 0.05)
        exec_mode = await cfg.get_typed("s5_sub_event.exec_mode", str, "manual")

        # Group markets by parent slug
        slug_groups = {}
        for m in markets:
            slug = m.get("slug") or ""
            parts = slug.rsplit("/", 1)
            parent_slug = parts[0] if len(parts) > 1 else None
            if parent_slug:
                slug_groups.setdefault(parent_slug, []).append(m)

        opps = []
        scan_gas_usdc = await gas_tracker.get_gas_cost_usdc()

        for parent_slug, sub_markets in slug_groups.items():
            if len(sub_markets) < 2:
                continue

            # Find matching parent market
            parent_market = next((m for m in markets if m.get("slug") == parent_slug), None)
            if not parent_market:
                continue

            parent_outcomes = parse_list(parent_market.get("outcomes"))
            parent_prices = parse_list(parent_market.get("outcomePrices"))
            parent_token_ids = parse_list(parent_market.get("clobTokenIds"))
            if len(parent_prices) < 2:
                continue

            parent_yes_idx = next((i for i, o in enumerate(parent_outcomes) if "yes" in o.lower()), 0)
            parent_yes = float(parent_prices[parent_yes_idx])
            parent_token = parent_token_ids[parent_yes_idx] if parent_yes_idx < len(parent_token_ids) else None

            # Sub-events sum
            sub_yes_prices = []
            sub_legs = []
            try:
                for sm in sub_markets:
                    sm_outcomes = parse_list(sm.get("outcomes"))
                    sm_prices = parse_list(sm.get("outcomePrices"))
                    sm_tokens = parse_list(sm.get("clobTokenIds"))
                    if len(sm_prices) >= 2:
                        yes_idx = next((i for i, o in enumerate(sm_outcomes) if "yes" in o.lower()), 0)
                        px = float(sm_prices[yes_idx])
                        sub_yes_prices.append(px)
                        sub_legs.append({
                            "market_id": sm.get("id"),
                            "market_title": sm.get("question"),
                            "outcome": "Yes",
                            "price": round(px, 4),
                            "token_id": sm_tokens[yes_idx] if yes_idx < len(sm_tokens) else None
                        })
            except Exception:
                continue

            if not sub_yes_prices:
                continue

            sub_sum = sum(sub_yes_prices)
            gap = abs(parent_yes - sub_sum)
            gap_pct = gap * 100

            if gap_pct < min_gap_pct:
                continue

            # Trade direction
            if parent_yes > sub_sum:
                action = "buy_sub_basket"
            else:
                action = "buy_parent_yes"

            suggested_usdc = max(0.50, balance * max_pos_pct)
            actual_gap_pct = 0.0
            total_slippage = 0.0
            total_gas = 0.0

            try:
                if action == "buy_parent_yes":
                    # Single leg buy on parent
                    if not parent_token:
                        continue
                    exec_data = await calculate_execution_price(parent_token, suggested_usdc, side="buy", http_client=http_client)
                    if "error" in exec_data:
                        continue
                    fill_parent_yes = exec_data["price"]
                    total_slippage = exec_data["slippage"]
                    total_gas = scan_gas_usdc
                    
                    actual_gap_pct = (sub_sum - fill_parent_yes) * 100
                else:
                    # Basket buy on sub-events
                    actual_basket_cost = 0.0
                    total_gas = len(sub_legs) * scan_gas_usdc
                    
                    for leg in sub_legs:
                        leg_target_usdc = (leg["price"] / sub_sum) * suggested_usdc
                        l_exec = await calculate_execution_price(leg["token_id"], leg_target_usdc, side="buy", http_client=http_client)
                        if "error" in l_exec:
                            raise ValueError(l_exec["error"])
                        actual_basket_cost += (l_exec["price"] * (leg_target_usdc / leg["price"]))
                        total_slippage += l_exec["slippage"]

                    fill_sub_sum = actual_basket_cost / (suggested_usdc / sub_sum) if suggested_usdc > 0 else sub_sum
                    actual_gap_pct = (parent_yes - fill_sub_sum) * 100
                    total_slippage /= len(sub_legs)

                # Deduct gas friction
                gas_impact_pct = (total_gas / suggested_usdc) * 100
                net_profit_pct = actual_gap_pct - gas_impact_pct

                if net_profit_pct < min_gap_pct:
                    continue

                days = days_to_expiry(parent_market.get("endDate"))
                parent_url = get_market_url(parent_market)

                opps.append({
                    "id": f"s5_{parent_market.get('id', '')}",
                    "strategy": self.key,
                    "market_id": str(parent_market.get("id", "")),
                    "market_title": parent_market.get("question", ""),
                    "market_url": parent_url,
                    "outcome": action.replace("_", " ").title(),
                    "entry_price": round(parent_yes, 4),
                    "yes_price": round(parent_yes, 4),
                    "no_price": round(1.0 - parent_yes, 4),
                    "implied_prob": round(parent_yes * 100, 2),
                    "slippage_bps": round(total_slippage * 100, 2),
                    "annualized_apy": round(net_profit_pct * (365.0 / max(0.1, days)), 2) if days else None,
                    "profit_pct": round(net_profit_pct, 2),
                    "days_to_expiry": round(days, 1) if days else None,
                    "action": action,
                    "exec_mode": exec_mode,
                    "suggested_usdc": round(suggested_usdc, 2),
                    "legs": sub_legs if action == "buy_sub_basket" else None,
                    "token_id": parent_token if action == "buy_parent_yes" else None,
                    "status": "open",
                    "notes": f"Parent YES ({parent_yes:.2%}) vs Sub-events sum ({sub_sum:.2%}). Edge: {net_profit_pct:.2f}% after gas ${total_gas:.2f}.",
                    "instructions": [
                        f"Parent URL: {parent_url}",
                        f"Action: {action.upper()}",
                        f"Deploy ${round(suggested_usdc, 2)} USDC on mispricing to capture arbitrage spread."
                    ]
                })

            except Exception:
                continue

        opps.sort(key=lambda x: x["profit_pct"], reverse=True)
        return opps

    async def execute(self, opportunity: dict, clob_client) -> dict:
        """S5 execution. Multi-leg is handled by engine, but single leg is handled here."""
        if opportunity.get("action") == "buy_parent_yes":
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
        return {"success": False, "error": "S5 Sub-Event multi-leg must be executed via engine dispatch"}
