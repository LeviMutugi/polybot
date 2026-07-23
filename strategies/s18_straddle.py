"""
Strategy S18: Catalyst Straddle Arbitrage
Description: Straddles binary events by buying both sides just prior to news releases.

Real implementation, and an important honesty note: buying BOTH sides of a binary
market and holding to resolution is, by construction, a small GUARANTEED LOSS (the
combined ask price is essentially always >= $1.00 plus gas — there is no free
lunch here; when it dips below $1.00 that's S17 Sniper's job, not this one). The
actual edge in a "catalyst straddle" comes from buying while uncertainty (and
therefore convexity) is at its peak — both sides priced near 50/50 — then
EXITING THE WINNING LEG EARLY once a scheduled catalyst (a debate, a ruling, an
earnings-style event) resolves the uncertainty and repriced it favorably, well
before final settlement. This strategy only automates the ENTRY (with a bounded
worst-case carry cost if held to resolution); the exit requires a human to notice
the catalyst has landed, which is exactly why it stays in 'manual' mode by
default — there is no reliable data source in this codebase for "when does the
catalyst happen," so timing the exit is a genuinely human judgment call.
"""
from typing import List
import httpx
from strategies.base import (
    BaseStrategy, parse_list, days_to_expiry, get_market_url,
    calculate_execution_price, is_fillable,
)
from db.config import cfg
from services.gas_tracker import gas_tracker

class CatalystStraddleStrategy(BaseStrategy):
    def __init__(self):
        super().__init__(
            key="s18_straddle",
            name="Catalyst Straddle",
            risk_level="High",
            market_type="Binary",
            default_exec_mode="manual",
            payoff_type="guaranteed_arb"
        )

    async def scan(self, markets: List[dict], balance: float, http_client: httpx.AsyncClient) -> List[dict]:
        enabled = await cfg.get_typed(f"{self.key}.enabled", bool, True)
        if not enabled:
            return []

        min_leg_price = await cfg.get_typed(f"{self.key}.min_leg_price", float, 0.40)
        max_leg_price = await cfg.get_typed(f"{self.key}.max_leg_price", float, 0.60)
        # Guaranteed-floor-if-held-to-resolution vs. cost ratio ceiling — bounds the
        # "insurance premium" this straddle is allowed to cost. 1.05 = at most a 5%
        # guaranteed carry cost.
        max_cost_ratio = await cfg.get_typed(f"{self.key}.max_cost_ratio", float, 1.05)
        max_pos_pct = await cfg.get_typed(f"{self.key}.max_position_pct", float, 0.03)
        exec_mode = await cfg.get_typed(f"{self.key}.exec_mode", str, "manual")
        max_slippage = await cfg.get_typed("poly_yield.max_slippage_pct", float, 1.5)

        opps = []
        scan_gas_usdc = await gas_tracker.get_gas_cost_usdc()

        for market in markets:
            try:
                outcomes = parse_list(market.get("outcomes"))
                prices = parse_list(market.get("outcomePrices"))
                token_ids = parse_list(market.get("clobTokenIds"))
                if len(outcomes) != 2 or len(prices) != 2:
                    continue

                float_prices = [float(p) for p in prices]
                if not all(min_leg_price <= p <= max_leg_price for p in float_prices):
                    continue  # only maximal-uncertainty (near 50/50) markets qualify

                yes_idx = next((i for i, o in enumerate(outcomes) if "yes" in o.lower()), 0)
                no_idx = 1 - yes_idx

                total_cap = balance * max_pos_pct
                sum_prices = sum(float_prices)
                shares_target = total_cap / sum_prices

                leg_details = []
                actual_total_cost = 0.0
                total_slippage = 0.0
                aborted = False

                for i, o_price in enumerate(float_prices):
                    leg_target_usdc = shares_target * o_price
                    token_id = token_ids[i] if i < len(token_ids) else None
                    if not token_id:
                        aborted = True
                        break

                    exec_data = await calculate_execution_price(token_id, leg_target_usdc, side="buy", http_client=http_client)
                    if not is_fillable(exec_data, max_slippage):
                        aborted = True
                        break

                    leg_fill_price = exec_data["price"]
                    leg_shares = leg_target_usdc / leg_fill_price if leg_fill_price > 0 else 0
                    actual_total_cost += leg_fill_price * leg_shares
                    total_slippage += exec_data.get("slippage", 0)

                    leg_details.append({
                        "outcome": outcomes[i],
                        "price": round(o_price, 4),
                        "fill_price": leg_fill_price,
                        "shares": leg_shares,
                        "token_id": token_id,
                        "stake_usdc": round(leg_target_usdc, 2),
                        "market_id": str(market.get("id", ""))
                    })

                if aborted:
                    continue

                min_shares = min(leg["shares"] for leg in leg_details)
                total_gas = len(outcomes) * scan_gas_usdc
                total_expense = actual_total_cost + total_gas
                guaranteed_floor = min_shares * 1.0
                if guaranteed_floor <= 0:
                    continue

                cost_ratio = total_expense / guaranteed_floor
                if cost_ratio > max_cost_ratio:
                    continue  # too expensive an "insurance premium" for this straddle

                carry_cost_pct = ((guaranteed_floor - total_expense) / total_expense) * 100 if total_expense > 0 else 0

                days = days_to_expiry(market.get("endDate"))
                market_url = get_market_url(market)
                avg_slippage = total_slippage / len(outcomes)

                opps.append({
                    "id": f"{self.key}_{market.get('id', '')}",
                    "strategy": self.key,
                    "market_id": str(market.get("id", "")),
                    "market_title": market.get("question", ""),
                    "market_url": market_url,
                    "outcome": "STRADDLE (YES + NO)",
                    "entry_price": round(sum_prices, 4),
                    "slippage_bps": round(avg_slippage * 100, 2),
                    "annualized_apy": None,  # not a yield play — see notes
                    "profit_pct": round(carry_cost_pct, 3),
                    "days_to_expiry": round(days, 1) if days else None,
                    "action": "straddle",
                    "exec_mode": exec_mode,
                    "suggested_usdc": round(total_cap, 2),
                    "payoff_type": self.payoff_type,
                    "legs": leg_details,
                    "status": "open",
                    "notes": (
                        f"Max-uncertainty straddle: YES {float_prices[yes_idx]:.1%} / NO {float_prices[no_idx]:.1%}. "
                        f"Guaranteed floor if held to resolution is ${guaranteed_floor:.2f} vs ${total_expense:.2f} cost "
                        f"({carry_cost_pct:+.2f}% carry). The real play is exiting the WINNING leg manually once a "
                        f"catalyst resolves the uncertainty, before final settlement — NOT a hold-to-resolution "
                        f"yield strategy, and requires active monitoring."
                    ),
                    "instructions": [
                        f"Open: {market_url}",
                        "Buy BOTH YES and NO now, while uncertainty (and thus convexity) is highest.",
                        "After a scheduled catalyst resolves the uncertainty, manually exit the winning leg at its improved price — do not hold both to final settlement."
                    ]
                })
            except Exception:
                continue

        opps.sort(key=lambda x: x["profit_pct"], reverse=True)
        return opps

    async def execute(self, opportunity: dict, clob_client) -> dict:
        """S18 execution is handled by the engine's multi-leg parallel dispatch."""
        return {"success": False, "error": "S18 Catalyst Straddle must be executed via engine multi-leg dispatch"}
