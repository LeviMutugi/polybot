"""
Strategy S12: Trend Following Momentum
Description: Rides high-volume, strong-momentum directional shifts in active markets.

Real implementation: same in-process rolling price history as S11 (see that file's
docstring for the honest caveat — this is the bot's own observed samples, not an
external time-series feed), but the opposite thesis: instead of betting a spike
reverts, S12 requires a SUSTAINED, monotonic move over a longer lookback window
before betting the trend continues. The longer window + monotonicity requirement
(strategies.base.monotonic_trend) is what keeps this from just re-detecting the
same single spike S11 would already scalp.
"""
from typing import List
import httpx
from strategies.base import (
    BaseStrategy, parse_list, days_to_expiry, get_market_url,
    calculate_execution_price, calculate_simple_apy, is_fillable,
    record_price_sample, price_change_pct, monotonic_trend,
)
from db.config import cfg
from services.gas_tracker import gas_tracker

class MomentumStrategy(BaseStrategy):
    def __init__(self):
        super().__init__(
            key="s12_momentum",
            name="Trend Momentum",
            risk_level="Medium",
            market_type="Binary",
            default_exec_mode="semi"
        )

    async def scan(self, markets: List[dict], balance: float, http_client: httpx.AsyncClient) -> List[dict]:
        enabled = await cfg.get_typed(f"{self.key}.enabled", bool, True)
        if not enabled:
            return []

        trend_threshold_pct = await cfg.get_typed(f"{self.key}.trend_threshold_pct", float, 5.0)
        lookback_s = await cfg.get_typed(f"{self.key}.lookback_s", float, 3600.0)
        min_samples = await cfg.get_typed(f"{self.key}.min_samples", int, 4)
        max_pos_pct = await cfg.get_typed(f"{self.key}.max_position_pct", float, 0.02)
        min_apy = await cfg.get_typed(f"{self.key}.min_apy", float, 5.0)
        assumed_hold_days = await cfg.get_typed(f"{self.key}.assumed_hold_days", float, 3.0)
        exec_mode = await cfg.get_typed(f"{self.key}.exec_mode", str, "semi")
        max_slippage = await cfg.get_typed("poly_yield.max_slippage_pct", float, 1.5)
        min_price = await cfg.get_typed(f"{self.key}.min_price", float, 0.10)
        max_price = await cfg.get_typed(f"{self.key}.max_price", float, 0.90)

        opps = []
        scan_gas_usdc = await gas_tracker.get_gas_cost_usdc()

        for market in markets:
            try:
                days = days_to_expiry(market.get("endDate") or market.get("end_date_iso"))
                if days is None or days <= 0:
                    continue

                outcomes = parse_list(market.get("outcomes"))
                prices = parse_list(market.get("outcomePrices"))
                token_ids = parse_list(market.get("clobTokenIds"))
                if len(outcomes) != 2 or len(prices) != 2:
                    continue

                yes_idx = next((i for i, o in enumerate(outcomes) if "yes" in o.lower()), 0)
                no_idx = 1 - yes_idx
                yes_price = float(prices[yes_idx])
                if not (min_price <= yes_price <= max_price):
                    continue

                market_id = str(market.get("id", ""))
                history = record_price_sample(f"{self.key}:{market_id}", yes_price)
                trend = monotonic_trend(history, lookback_s, min_samples)
                if trend is None:
                    continue  # warm-up, or no sustained trend yet

                change_pct = price_change_pct(history, lookback_s)
                if change_pct is None or abs(change_pct) < trend_threshold_pct:
                    continue

                if trend == "up":
                    target_idx = yes_idx
                    thesis = f"YES trended up {change_pct:.1f}% over the last {lookback_s/60:.0f}m"
                else:
                    target_idx = no_idx
                    thesis = f"YES trended down {abs(change_pct):.1f}% over the last {lookback_s/60:.0f}m (NO trending up)"

                token_id = token_ids[target_idx] if target_idx < len(token_ids) else None
                if not token_id:
                    continue

                suggested_usdc = max(0.5, balance * max_pos_pct)
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
                real_apy = calculate_simple_apy(net_yield_pct, assumed_hold_days)
                if real_apy < min_apy or net_yield_pct <= 0:
                    continue

                market_url = get_market_url(market)
                opps.append({
                    "id": f"{self.key}_{market_id}",
                    "strategy": self.key,
                    "market_id": market_id,
                    "market_title": market.get("question", ""),
                    "market_url": market_url,
                    "outcome": outcomes[target_idx],
                    "entry_price": round(real_price, 4),
                    "implied_prob": round(real_price * 100, 2),
                    "slippage_bps": round(slippage * 100, 2),
                    "annualized_apy": round(real_apy, 2),
                    "profit_pct": round(net_yield_pct, 2),
                    "days_to_expiry": round(assumed_hold_days, 1),
                    "action": "buy",
                    "exec_mode": exec_mode,
                    "suggested_usdc": round(suggested_usdc, 2),
                    "token_id": token_id,
                    "status": "open",
                    "notes": (
                        f"Momentum continuation: {thesis} — riding the trend by buying "
                        f"'{outcomes[target_idx]}' at ${real_price:.4f}. Signal derived from this "
                        f"bot's own rolling price samples, not an external tick feed."
                    ),
                    "instructions": [
                        f"Open: {market_url}",
                        f"Buy '{outcomes[target_idx]}' for ${round(suggested_usdc, 2)} USDC at ~${real_price:.4f}",
                        "Trend-following play — monitor for reversal and consider exiting manually if the trend breaks."
                    ]
                })
            except Exception:
                continue

        opps.sort(key=lambda x: x["annualized_apy"], reverse=True)
        return opps

    async def execute(self, opportunity: dict, clob_client) -> dict:
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
