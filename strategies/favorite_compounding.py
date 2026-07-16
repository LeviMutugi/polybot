"""
Favorite Compounding Strategy
Description: Compound high-probability favorite outcomes (priced >= $0.95) with short expiries (< 7 days).
Limits exposure duration to minimize tail risks while compounding consistent yields.
"""
import uuid
from typing import List
import httpx
from strategies.base import BaseStrategy, parse_list, days_to_expiry, get_market_url, calculate_execution_price
from db.config import cfg
from services.gas_tracker import gas_tracker

class FavoriteCompoundingStrategy(BaseStrategy):
    def __init__(self):
        super().__init__(
            key="favorite_compounding",
            name="Favorite Compounding",
            risk_level="Low",
            market_type="Binary",
            default_exec_mode="auto"
        )

    async def scan(self, markets: List[dict], balance: float, http_client: httpx.AsyncClient) -> List[dict]:
        enabled = await cfg.get_typed("favorite_compounding.enabled", bool, True)
        if not enabled:
            return []

        min_yes_price = await cfg.get_typed("favorite_compounding.min_yes_price", float, 0.95)
        max_days_left = await cfg.get_typed("favorite_compounding.max_days_left", float, 7.0)
        max_pos_pct = await cfg.get_typed("favorite_compounding.max_position_pct", float, 0.10)
        min_apy = await cfg.get_typed("favorite_compounding.min_apy", float, 5.0)
        exec_mode = await cfg.get_typed("favorite_compounding.exec_mode", str, "auto")

        opps = []
        scan_gas_usdc = await gas_tracker.get_gas_cost_usdc()

        for market in markets:
            try:
                outcomes = parse_list(market.get("outcomes"))
                prices = parse_list(market.get("outcomePrices"))
                token_ids = parse_list(market.get("clobTokenIds"))

                if len(outcomes) != 2 or len(prices) != len(outcomes):
                    continue

                end_dt = market.get("endDate") or market.get("end_date_iso")
                days = days_to_expiry(end_dt)
                if days is None or days <= 0 or days > max_days_left:
                    continue

                # Scan both YES and NO outcomes to see if either is priced >= min_yes_price
                for i, (outcome, price_str) in enumerate(zip(outcomes, prices)):
                    price = float(price_str)
                    if price < min_yes_price or price >= 0.999:
                        continue

                    token_id = token_ids[i] if i < len(token_ids) else None
                    if not token_id:
                        continue

                    # Sizing
                    suggested_usdc = max(0.50, balance * max_pos_pct)

                    # Walk book depth
                    exec_data = await calculate_execution_price(token_id, suggested_usdc, side="buy", http_client=http_client)
                    if "error" in exec_data:
                        continue

                    real_price = exec_data["price"]
                    slippage = exec_data.get("slippage", 0)

                    shares = suggested_usdc / real_price if real_price > 0 else 0
                    if shares <= 0:
                        continue

                    # Net APY calculation
                    net_gain_per_share = (1.0 - real_price) - (scan_gas_usdc / shares)
                    net_yield = net_gain_per_share / real_price
                    real_apy = net_yield * (365.0 / max(0.1, days)) * 100

                    if real_apy < min_apy:
                        continue

                    market_url = get_market_url(market)

                    opps.append({
                        "id": f"fav_{market.get('id','')}_{i}",
                        "strategy": self.key,
                        "market_id": str(market.get("id", "")),
                        "market_title": market.get("question", ""),
                        "market_url": market_url,
                        "outcome": outcome,
                        "entry_price": round(real_price, 4),
                        "yes_price": round(price if i == 0 else 1.0 - price, 4),
                        "no_price": round(1.0 - price if i == 0 else price, 4),
                        "implied_prob": round(real_price * 100, 2),
                        "slippage_bps": round(slippage * 100, 2),
                        "annualized_apy": round(real_apy, 2),
                        "profit_pct": round(net_yield * 100, 2),
                        "days_to_expiry": round(days, 1),
                        "action": "buy_yes" if i == 0 else "buy_no",
                        "exec_mode": exec_mode,
                        "suggested_usdc": round(suggested_usdc, 2),
                        "token_id": token_id,
                        "status": "open",
                        "notes": f"Compound favorite '{outcome}' at ${real_price:.4f} with {days:.1f} days left. Est APY: {real_apy:.1f}%.",
                        "instructions": [
                            f"Open: {market_url}",
                            f"Buy '{outcome}' shares for ${round(suggested_usdc, 2)} USDC.",
                            f"Yield compounds as contract resolves to $1.00 at expiry."
                        ]
                    })

            except Exception:
                continue

        opps.sort(key=lambda x: x["annualized_apy"], reverse=True)
        return opps

    async def execute(self, opportunity: dict, clob_client) -> dict:
        """Execute Compounding Buy order."""
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
