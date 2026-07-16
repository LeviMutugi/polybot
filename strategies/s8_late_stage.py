"""
Strategy S8: Late-Stage Resolution Yield
Description: Scans for short-duration binary markets where the outcome is practically decided (>=98%)
but not yet resolved. Buys the near-certain outcome and holds the final days to expiry.
"""
from typing import List
import httpx
from strategies.base import (
    BaseStrategy, parse_list, days_to_expiry, get_market_url,
    calculate_execution_price, calculate_simple_apy, is_fillable,
)
from db.config import cfg
from services.gas_tracker import gas_tracker

class LateStageStrategy(BaseStrategy):
    def __init__(self):
        super().__init__(
            key="s8_late_stage",
            name="Late Stage Yield",
            risk_level="Low",
            market_type="Binary",
            default_exec_mode="semi"
        )

    async def scan(self, markets: List[dict], balance: float, http_client: httpx.AsyncClient) -> List[dict]:
        enabled = await cfg.get_typed(f"{self.key}.enabled", bool, True)
        if not enabled:
            return []

        min_price = await cfg.get_typed(f"{self.key}.min_price", float, 0.98)
        max_days = await cfg.get_typed(f"{self.key}.max_days_left", float, 3.0)
        max_pos_pct = await cfg.get_typed(f"{self.key}.max_position_pct", float, 0.05)
        min_apy = await cfg.get_typed(f"{self.key}.min_apy", float, 3.0)
        exec_mode = await cfg.get_typed(f"{self.key}.exec_mode", str, "semi")
        max_slippage = await cfg.get_typed("poly_yield.max_slippage_pct", float, 1.5)

        opps = []
        scan_gas_usdc = await gas_tracker.get_gas_cost_usdc()

        for market in markets:
            try:
                end_dt = market.get("endDate") or market.get("end_date_iso")
                days = days_to_expiry(end_dt)
                if days is None or days <= 0 or days > max_days:
                    continue

                prices = parse_list(market.get("outcomePrices"))
                outcomes = parse_list(market.get("outcomes"))
                token_ids = parse_list(market.get("clobTokenIds"))
                if len(prices) != 2 or len(outcomes) != 2:
                    continue

                for i, p_str in enumerate(prices):
                    p = float(p_str)
                    if not (min_price <= p < 0.999):
                        continue

                    token_id = token_ids[i] if i < len(token_ids) else None
                    if not token_id:
                        continue

                    suggested_usdc = max(1.0, balance * max_pos_pct)

                    # Verify the real fill price against the live order book
                    exec_data = await calculate_execution_price(token_id, suggested_usdc, side="buy", http_client=http_client)
                    if not is_fillable(exec_data, max_slippage):
                        continue

                    real_price = exec_data["price"]
                    slippage = exec_data.get("slippage", 0)
                    if not (0 < real_price < 0.999):
                        continue

                    shares = suggested_usdc / real_price
                    net_gain_per_share = (1.0 - real_price) - (scan_gas_usdc / shares)
                    net_yield_pct = (net_gain_per_share / real_price) * 100
                    real_apy = calculate_simple_apy(net_yield_pct, days)
                    if real_apy < min_apy or net_yield_pct <= 0:
                        continue

                    market_url = get_market_url(market)
                    opps.append({
                        "id": f"{self.key}_{market.get('id', '')}_{i}",
                        "strategy": self.key,
                        "market_id": str(market.get("id", "")),
                        "market_title": market.get("question", ""),
                        "market_url": market_url,
                        "outcome": outcomes[i],
                        "entry_price": round(real_price, 4),
                        "implied_prob": round(real_price * 100, 2),
                        "slippage_bps": round(slippage * 100, 2),
                        "annualized_apy": round(real_apy, 2),
                        "profit_pct": round(net_yield_pct, 2),
                        "days_to_expiry": round(days, 1),
                        "risk_level": self.risk_level,
                        "action": "buy",
                        "exec_mode": exec_mode,
                        "suggested_usdc": round(suggested_usdc, 2),
                        "token_id": token_id,
                        "status": "open",
                        "notes": f"Near-decided outcome '{outcomes[i]}' at ${real_price:.4f}, {days:.1f}d to expiry. Net yield {net_yield_pct:.2f}% after gas.",
                        "instructions": [
                            f"Open: {market_url}",
                            f"Buy '{outcomes[i]}' for ${round(suggested_usdc, 2)} USDC at ~${real_price:.4f}",
                            f"Hold to resolution ({days:.1f} days) for {net_yield_pct:.2f}% yield."
                        ]
                    })
                    break  # One opportunity per market
            except Exception:
                continue

        opps.sort(key=lambda x: x["annualized_apy"], reverse=True)
        return opps

    async def execute(self, opportunity: dict, clob_client) -> dict:
        """Buy the near-certain outcome token."""
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
