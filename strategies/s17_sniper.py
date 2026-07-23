"""
Strategy S17: Liquidity Sniper
Description: Snipes mispriced limit orders during thin liquidity market sessions.

Real implementation: the binary-market special case of S3 Buy-All's guaranteed
arbitrage. S3 explicitly requires >= 3 outcomes (`len(outcomes) < 3: continue`),
so ordinary 2-outcome Yes/No markets are never covered by it — exactly the gap
this strategy fills. When a thinly-traded binary market's YES-ask + NO-ask sum
drops below $1.00 (a transient mispricing that shows up specifically when a book
is thin), buying both sides in proportion guarantees the bottleneck leg's shares
pay out $1.00 regardless of resolution — a real, distinct arbitrage, executed via
the engine's existing multi-leg dispatch (same mechanism as S3/S5).
"""
from typing import List
import httpx
from strategies.base import (
    BaseStrategy, parse_list, days_to_expiry, get_market_url,
    calculate_execution_price, calculate_simple_apy, is_fillable,
)
from db.config import cfg
from services.gas_tracker import gas_tracker

class SniperStrategy(BaseStrategy):
    def __init__(self):
        super().__init__(
            key="s17_sniper",
            name="Liquidity Sniper",
            risk_level="High",
            market_type="Binary",
            default_exec_mode="auto",
            payoff_type="guaranteed_arb"
        )

    async def scan(self, markets: List[dict], balance: float, http_client: httpx.AsyncClient) -> List[dict]:
        enabled = await cfg.get_typed(f"{self.key}.enabled", bool, True)
        if not enabled:
            return []

        min_profit_pct = await cfg.get_typed(f"{self.key}.min_profit_pct", float, 0.5)
        max_pos_pct = await cfg.get_typed(f"{self.key}.max_position_pct", float, 0.05)
        exec_mode = await cfg.get_typed(f"{self.key}.exec_mode", str, "auto")
        max_slippage = await cfg.get_typed("poly_yield.max_slippage_pct", float, 1.5)

        opps = []
        scan_gas_usdc = await gas_tracker.get_gas_cost_usdc()

        for market in markets:
            try:
                outcomes = parse_list(market.get("outcomes"))
                prices = parse_list(market.get("outcomePrices"))
                token_ids = parse_list(market.get("clobTokenIds"))
                # S3 Buy-All already owns markets with >= 3 outcomes — this is the
                # 2-outcome (ordinary Yes/No) case S3 explicitly skips.
                if len(outcomes) != 2 or len(prices) != 2:
                    continue

                float_prices = [float(p) for p in prices]
                if any(p <= 0 for p in float_prices):
                    continue

                sum_prices = sum(float_prices)
                if sum_prices >= 1.0:
                    continue  # no mispricing at the last-trade snapshot

                theoretical_profit_pct = (1.0 - sum_prices) / sum_prices * 100
                if theoretical_profit_pct < min_profit_pct:
                    continue

                total_cap = balance * max_pos_pct
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
                net_guaranteed_revenue = min_shares * 1.0
                total_gas = len(outcomes) * scan_gas_usdc
                total_expense = actual_total_cost + total_gas
                real_profit_usdc = net_guaranteed_revenue - total_expense
                real_profit_pct = (real_profit_usdc / total_expense) * 100 if total_expense > 0 else 0

                if real_profit_pct < min_profit_pct:
                    continue

                days = days_to_expiry(market.get("endDate"))
                market_url = get_market_url(market)
                avg_slippage = total_slippage / len(outcomes)

                opps.append({
                    "id": f"{self.key}_{market.get('id', '')}",
                    "strategy": self.key,
                    "market_id": str(market.get("id", "")),
                    "market_title": market.get("question", ""),
                    "market_url": market_url,
                    "outcome": "BASKET (YES + NO)",
                    "entry_price": round(sum_prices, 4),
                    "slippage_bps": round(avg_slippage * 100, 2),
                    "annualized_apy": round(calculate_simple_apy(real_profit_pct, days), 2) if days else None,
                    "profit_pct": round(real_profit_pct, 3),
                    "days_to_expiry": round(days, 1) if days else None,
                    "action": "buy_both",
                    "exec_mode": exec_mode,
                    "suggested_usdc": round(total_cap, 2),
                    "payoff_type": self.payoff_type,
                    "legs": leg_details,
                    "status": "open",
                    "notes": f"Thin-book mispricing: YES+NO ask sum = {sum_prices:.4f} < 1.00. Net profit ${real_profit_usdc:.2f} after gas ${total_gas:.2f}.",
                    "instructions": [
                        f"Open: {market_url}",
                        "Buy BOTH YES and NO simultaneously (thin-liquidity mispricing).",
                        "Proportional allocation guarantees the bottleneck leg's shares resolve to $1.00 regardless of outcome."
                    ]
                })
            except Exception:
                continue

        opps.sort(key=lambda x: x["profit_pct"], reverse=True)
        return opps

    async def execute(self, opportunity: dict, clob_client) -> dict:
        """S17 execution is handled by the engine's multi-leg parallel dispatch."""
        return {"success": False, "error": "S17 Liquidity Sniper must be executed via engine multi-leg dispatch"}
