"""
Strategy S3: Buy-All Exhaustive Arbitrage
Description: Exploits multi-outcome markets where the sum of prices of all outcomes is less than $1.00.
Buying all outcomes in proportional shares guarantees a risk-free payout of $1.00 per share.
"""
import uuid
import asyncio
from typing import List
import httpx
from strategies.base import BaseStrategy, parse_list, days_to_expiry, get_market_url, calculate_execution_price
from db.config import cfg
from services.gas_tracker import gas_tracker

class BuyAllArbitrageStrategy(BaseStrategy):
    def __init__(self):
        super().__init__(
            key="s3_buy_all",
            name="Buy-All Arb",
            risk_level="Low",
            market_type="Multi-outcome",
            default_exec_mode="auto"
        )

    async def scan(self, markets: List[dict], balance: float, http_client: httpx.AsyncClient) -> List[dict]:
        enabled = await cfg.get_typed("s3_buy_all.enabled", bool, True)
        if not enabled:
            return []

        min_profit_pct = await cfg.get_typed("s3_buy_all.min_profit_pct", float, 0.5)
        max_pos_pct = await cfg.get_typed("s3_buy_all.max_position_pct", float, 0.10)
        exec_mode = await cfg.get_typed("s3_buy_all.exec_mode", str, "auto")

        opps = []
        scan_gas_usdc = await gas_tracker.get_gas_cost_usdc()

        for market in markets:
            try:
                outcomes = parse_list(market.get("outcomes"))
                prices = parse_list(market.get("outcomePrices"))
                token_ids = parse_list(market.get("clobTokenIds"))

                if len(outcomes) < 3 or len(prices) != len(outcomes):
                    continue

                float_prices = [float(p) for p in prices]
                if any(p <= 0 for p in float_prices):
                    continue

                sum_prices = sum(float_prices)
                if sum_prices >= 1.0:
                    continue

                # Theoretical profit percentage before slippage and gas
                theoretical_profit_pct = (1.0 - sum_prices) / sum_prices * 100
                if theoretical_profit_pct < min_profit_pct:
                    continue

                # Sizing — buy EQUAL shares of each outcome
                total_cap = balance * max_pos_pct
                # shares_per_unit = total_cap / sum_prices gives us how many complete sets we can afford
                shares_target = total_cap / sum_prices
                
                # Check execution cost for each leg
                actual_total_cost = 0.0
                leg_details = []
                total_slippage = 0.0

                from strategies.base import is_fillable
                max_slippage = await cfg.get_typed("poly_yield.max_slippage_pct", float, 1.5)

                for i, o_price in enumerate(float_prices):
                    leg_target_usdc = shares_target * o_price  # Equal shares × leg price
                    token_id = token_ids[i] if i < len(token_ids) else None
                    if not token_id:
                        raise ValueError("Missing token ID")

                    exec_data = await calculate_execution_price(token_id, leg_target_usdc, side="buy", http_client=http_client)
                    if not is_fillable(exec_data, max_slippage):
                        raise ValueError(exec_data.get("error") or exec_data.get("warning") or "Leg not fillable within slippage tolerance")

                    leg_fill_price = exec_data["price"]
                    # Actual shares we get at this fill price
                    leg_shares = leg_target_usdc / leg_fill_price if leg_fill_price > 0 else 0
                    actual_leg_cost = leg_fill_price * leg_shares
                    actual_total_cost += actual_leg_cost
                    total_slippage += exec_data.get("slippage", 0)

                    leg_details.append({
                        "outcome": outcomes[i],
                        "price": round(o_price, 4),
                        "fill_price": leg_fill_price,
                        "shares": leg_shares,
                        "token_id": token_id,
                        "stake_usdc": round(leg_target_usdc, 2)
                    })

                # Bottle-neck leg determines the payout floor
                min_shares = min(leg["shares"] for leg in leg_details)
                net_guaranteed_revenue = min_shares * 1.0
                
                # Deduct gas expenses (each outcome requires a transaction)
                total_gas = len(outcomes) * scan_gas_usdc
                total_expense = actual_total_cost + total_gas
                
                real_profit_usdc = net_guaranteed_revenue - total_expense
                real_profit_pct = (real_profit_usdc / total_expense) * 100

                if real_profit_pct < min_profit_pct:
                    continue

                days = days_to_expiry(market.get("endDate"))
                from strategies.base import calculate_simple_apy
                annualized_apy = calculate_simple_apy(real_profit_pct, days) if days else None

                market_url = get_market_url(market)
                avg_slippage = total_slippage / len(outcomes)

                opps.append({
                    "id": f"s3_{market.get('id', uuid.uuid4().hex[:8])}",
                    "strategy": self.key,
                    "market_id": str(market.get("id", "")),
                    "market_title": market.get("question", ""),
                    "market_url": market_url,
                    "outcome": f"BASKET ({len(outcomes)} outcomes)",
                    "entry_price": round(sum_prices, 4),
                    "slippage_bps": round(avg_slippage * 100, 2),
                    "annualized_apy": round(annualized_apy, 2) if annualized_apy else None,
                    "profit_pct": round(real_profit_pct, 3),
                    "days_to_expiry": round(days, 1) if days else None,
                    "action": "buy_all",
                    "exec_mode": exec_mode,
                    "suggested_usdc": round(total_cap, 2),
                    "legs": leg_details,
                    "status": "open",
                    "notes": f"Arbitrage sum = {sum_prices:.4f} < 1.00. Net profit ${real_profit_usdc:.2f} after gas ${total_gas:.2f}.",
                    "instructions": [
                        f"Open: {market_url}",
                        f"Buy ALL {len(outcomes)} outcomes simultaneously.",
                        f"Proportional allocation of capital ensures equal shares payout resolving to $1.00."
                    ]
                })

            except Exception:
                continue

        opps.sort(key=lambda x: x["profit_pct"], reverse=True)
        return opps

    async def execute(self, opportunity: dict, clob_client) -> dict:
        """S3 execution is handled by the engine's multi-leg parallel dispatch."""
        return {"success": False, "error": "S3 Buy-All must be executed via engine multi-leg dispatch"}
