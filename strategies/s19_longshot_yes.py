"""
Strategy S19: Longshot YES Sniping
Description: Places small bets on ultra-low-probability YES outcomes (<2%) for asymmetric payoffs.

This is deliberately the OPPOSITE side of S1 Novelty and S6 Longshot MM, which
both bet AGAINST ultra-longshot YES outcomes (buying NO) on the thesis that
retail speculation overprices them. S19 takes the other side on purpose: small,
tightly-capped "lottery ticket" bets on the longshot YES itself, embracing
negative expected value in exchange for asymmetric convex payoffs on the rare
hit. Because the whole premise is accepting bad-EV bets in small size, this
strategy enforces its own total-exposure cap across ALL open S19 positions (not
just a per-position size limit) so the "small bets" framing can't be violated by
simply opening many of them.
"""
from typing import List
import httpx
from strategies.base import (
    BaseStrategy, parse_list, days_to_expiry, get_market_url,
    calculate_execution_price, is_fillable,
)
from db.config import cfg

class LongshotYesStrategy(BaseStrategy):
    def __init__(self):
        super().__init__(
            key="s19_longshot_yes",
            name="Longshot YES",
            risk_level="High",
            market_type="Binary",
            default_exec_mode="semi"
        )

    async def scan(self, markets: List[dict], balance: float, http_client: httpx.AsyncClient) -> List[dict]:
        enabled = await cfg.get_typed(f"{self.key}.enabled", bool, True)
        if not enabled:
            return []

        max_yes_price = await cfg.get_typed(f"{self.key}.max_yes_price", float, 0.02)
        max_positions = await cfg.get_typed(f"{self.key}.max_positions", int, 15)
        position_pct = await cfg.get_typed(f"{self.key}.position_pct", float, 0.003)
        max_total_allocation_pct = await cfg.get_typed(f"{self.key}.max_total_allocation_pct", float, 0.03)
        exec_mode = await cfg.get_typed(f"{self.key}.exec_mode", str, "semi")
        max_slippage = await cfg.get_typed("poly_yield.max_slippage_pct", float, 1.5)

        active_mode = await cfg.get_typed("poly_yield.active_mode", str, "paper")
        from db.database import get_sqlite, _sqlite_lock
        conn = get_sqlite()
        with _sqlite_lock:
            row = conn.execute(
                "SELECT COUNT(*) as c, COALESCE(SUM(cost_usdc), 0) as total_cost "
                "FROM poly_yield_positions WHERE strategy = ? AND status = 'open' AND mode = ?",
                [self.key, active_mode]
            ).fetchone()
        open_count = row["c"] if row else 0
        total_exposure = row["total_cost"] if row else 0.0
        available_slots = max_positions - open_count
        remaining_budget = max(0.0, balance * max_total_allocation_pct - total_exposure)

        if available_slots <= 0 or remaining_budget < 0.5:
            return []

        opps = []
        for market in markets:
            if len(opps) >= available_slots or remaining_budget < 0.5:
                break
            try:
                outcomes = parse_list(market.get("outcomes"))
                prices = parse_list(market.get("outcomePrices"))
                token_ids = parse_list(market.get("clobTokenIds"))
                if len(outcomes) != 2 or len(prices) != 2:
                    continue

                yes_idx = next((i for i, o in enumerate(outcomes) if "yes" in o.lower()), 0)
                yes_price = float(prices[yes_idx])
                if yes_price > max_yes_price or yes_price <= 0.0005:
                    continue

                days = days_to_expiry(market.get("endDate") or market.get("end_date_iso"))
                if days is None or days <= 0:
                    continue

                with _sqlite_lock:
                    existing = conn.execute(
                        "SELECT id FROM poly_yield_positions WHERE market_id = ? AND strategy = ? AND status = 'open'",
                        [str(market.get("id")), self.key]
                    ).fetchone()
                if existing:
                    continue

                token_id = token_ids[yes_idx] if yes_idx < len(token_ids) else None
                if not token_id:
                    continue

                suggested_usdc = min(max(0.5, balance * position_pct), remaining_budget)
                if suggested_usdc < 0.5:
                    continue

                exec_data = await calculate_execution_price(token_id, suggested_usdc, side="buy", http_client=http_client)
                if not is_fillable(exec_data, max_slippage):
                    continue

                real_price = exec_data["price"]
                slippage = exec_data.get("slippage", 0)
                # Sanity guard: slippage on a thin longshot book can walk the fill well
                # past the intended band — don't chase it further than 1.5x the ceiling.
                if not (0 < real_price <= max_yes_price * 1.5):
                    continue

                payout_multiple = 1.0 / real_price if real_price > 0 else 0

                market_url = get_market_url(market)
                opps.append({
                    "id": f"{self.key}_{market.get('id', '')}",
                    "strategy": self.key,
                    "market_id": str(market.get("id", "")),
                    "market_title": market.get("question", ""),
                    "market_url": market_url,
                    "outcome": outcomes[yes_idx],
                    "entry_price": round(real_price, 4),
                    "yes_price": round(yes_price, 4),
                    "implied_prob": round(real_price * 100, 2),
                    "slippage_bps": round(slippage * 100, 2),
                    "annualized_apy": None,  # deliberately not a yield play
                    "profit_pct": round((payout_multiple - 1) * 100, 1),
                    "days_to_expiry": round(days, 1),
                    "action": "buy_yes",
                    "exec_mode": exec_mode,
                    "suggested_usdc": round(suggested_usdc, 2),
                    "token_id": token_id,
                    "status": "open",
                    "notes": (
                        f"Deliberate asymmetric tail bet: '{outcomes[yes_idx]}' at ${real_price:.4f} pays "
                        f"~{payout_multiple:.0f}x if it hits. Sized tiny on purpose — a lottery-ticket "
                        f"allocation, not a positive-EV yield strategy (S1/S6 bet AGAINST this same kind of "
                        f"longshot on the calibrated thesis that it's overpriced; S19 deliberately takes the "
                        f"other side in small, capped size)."
                    ),
                    "instructions": [
                        f"Open: {market_url}",
                        f"Buy '{outcomes[yes_idx]}' for ${round(suggested_usdc, 2)} USDC at ~${real_price:.4f}",
                        f"Hold to expiry — potential ~{payout_multiple:.0f}x payout; expect a total loss most of the time."
                    ]
                })
                remaining_budget -= suggested_usdc
            except Exception:
                continue

        opps.sort(key=lambda x: x["profit_pct"], reverse=True)
        return opps

    async def execute(self, opportunity: dict, clob_client) -> dict:
        """Buy the longshot YES token directly."""
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
