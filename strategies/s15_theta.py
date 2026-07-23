"""
Strategy S15: Time Decay Theta Harvester
Description: Sells/shorts speculative outcomes that decay over time.

Real implementation: targets a MID-band speculative YES price (10%-30% by default)
with a meaningful runway left (min_days_left), buying NO — i.e. selling the
speculative YES — on the thesis that absent a catalyst, time decay pulls
low-conviction speculative longshots toward zero as their window closes. This
band is deliberately distinct from S1/S6 (ultra-longshot YES <= 8%, tag-scoped or
auto-EV-driven) and S19 (ultra-longshot YES < 2%, the opposite side of the trade)
so none of these strategies compete for the same markets.
"""
from typing import List
import httpx
from strategies.base import (
    BaseStrategy, parse_list, days_to_expiry, get_market_url,
    calculate_execution_price, calculate_compounding_apy, is_fillable,
)
from db.config import cfg
from services.gas_tracker import gas_tracker

class ThetaHarvesterStrategy(BaseStrategy):
    def __init__(self):
        super().__init__(
            key="s15_theta",
            name="Theta Harvester",
            risk_level="Medium",
            market_type="Binary",
            default_exec_mode="manual"
        )

    async def scan(self, markets: List[dict], balance: float, http_client: httpx.AsyncClient) -> List[dict]:
        enabled = await cfg.get_typed(f"{self.key}.enabled", bool, True)
        if not enabled:
            return []

        min_yes_price = await cfg.get_typed(f"{self.key}.min_yes_price", float, 0.10)
        max_yes_price = await cfg.get_typed(f"{self.key}.max_yes_price", float, 0.30)
        min_days_left = await cfg.get_typed(f"{self.key}.min_days_left", float, 14.0)
        max_pos_pct = await cfg.get_typed(f"{self.key}.max_position_pct", float, 0.03)
        min_apy = await cfg.get_typed(f"{self.key}.min_apy", float, 4.0)
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

                yes_idx = next((i for i, o in enumerate(outcomes) if "yes" in o.lower()), 0)
                no_idx = 1 - yes_idx
                yes_price = float(prices[yes_idx])
                if not (min_yes_price <= yes_price <= max_yes_price):
                    continue

                days = days_to_expiry(market.get("endDate") or market.get("end_date_iso"))
                if days is None or days < min_days_left:
                    continue

                token_id_no = token_ids[no_idx] if no_idx < len(token_ids) else None
                if not token_id_no:
                    continue

                suggested_usdc = max(0.5, balance * max_pos_pct)
                exec_data = await calculate_execution_price(token_id_no, suggested_usdc, side="buy", http_client=http_client)
                if not is_fillable(exec_data, max_slippage):
                    continue

                real_no_price = exec_data["price"]
                slippage = exec_data.get("slippage", 0)
                shares = suggested_usdc / real_no_price if real_no_price > 0 else 0
                if shares <= 0:
                    continue

                net_gain_per_share = (1.0 - real_no_price) - (scan_gas_usdc / shares)
                net_hold_yield = net_gain_per_share / real_no_price
                real_apy = calculate_compounding_apy(net_hold_yield, days)
                if real_apy < min_apy:
                    continue

                from strategies.calibration import longshot_calibrator
                correction = longshot_calibrator.get_correction(yes_price)
                est_true_yes_prob = yes_price * correction
                est_true_no_prob = 1.0 - est_true_yes_prob

                market_url = get_market_url(market)
                opps.append({
                    "id": f"{self.key}_{market.get('id', '')}",
                    "strategy": self.key,
                    "market_id": str(market.get("id", "")),
                    "market_title": market.get("question", ""),
                    "market_url": market_url,
                    "outcome": outcomes[no_idx],
                    "entry_price": round(real_no_price, 4),
                    "yes_price": round(yes_price, 4),
                    "no_price": round(real_no_price, 4),
                    "implied_prob": round(real_no_price * 100, 2),
                    "est_true_prob": round(est_true_no_prob, 4),
                    "slippage_bps": round(slippage * 100, 2),
                    "annualized_apy": round(real_apy, 2),
                    "profit_pct": round(net_hold_yield * 100, 2),
                    "days_to_expiry": round(days, 1),
                    "action": "buy_no",
                    "exec_mode": exec_mode,
                    "suggested_usdc": round(suggested_usdc, 2),
                    "token_id": token_id_no,
                    "status": "open",
                    "notes": (
                        f"Speculative YES at {yes_price:.1%} with {days:.1f}d left — buying NO to harvest "
                        f"time decay as the catalyst window closes. Correction factor {correction:.2f} "
                        f"(the calibrator's empirical buckets only cover 0-10% YES; outside that this uses "
                        f"its {correction:.2f} default, not empirically calibrated for this price band)."
                    ),
                    "instructions": [
                        f"Open: {market_url}",
                        f"Buy NO for ${round(suggested_usdc, 2)} USDC",
                        f"Hold to expiry for a risk-adjusted {round(real_apy, 1)}% APY as time decay works against the speculative YES."
                    ]
                })
            except Exception:
                continue

        opps.sort(key=lambda x: x["annualized_apy"], reverse=True)
        return opps

    async def execute(self, opportunity: dict, clob_client) -> dict:
        """Buy NO (sell the speculative YES)."""
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
