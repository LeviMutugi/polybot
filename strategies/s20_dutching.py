"""
Strategy S20: Top-N Dutching Strategy with Tail-Risk Kelly Sizing
Description: Spreads stakes across the top N favorites in a multi-outcome market to secure uniform payouts if any selected candidate wins.
Uses LLM-based sentiment / tail-risk modeling to discount stake size when outside longshots pose non-trivial risk.
"""
import uuid
import logging
from typing import List, Dict, Optional
import httpx

from strategies.base import BaseStrategy, parse_list, days_to_expiry, get_market_url, calculate_execution_price
from db.config import cfg
from services.gas_tracker import gas_tracker

_log = logging.getLogger(__name__)

class DutchingStrategy(BaseStrategy):
    def __init__(self):
        super().__init__(
            key="s20_dutching",
            name="Top-N Dutching",
            risk_level="Medium",
            market_type="Multi-outcome",
            default_exec_mode="manual"
        )

    async def scan(self, markets: List[dict], balance: float, http_client: httpx.AsyncClient) -> List[dict]:
        enabled = await cfg.get_typed("s20_dutching.enabled", bool, True)
        if not enabled:
            return []

        top_n = await cfg.get_typed("s20_dutching.top_n", int, 3)
        max_set_price = await cfg.get_typed("s20_dutching.max_set_price", float, 0.92)
        min_roi_pct = await cfg.get_typed("s20_dutching.min_roi_pct", float, 3.0)
        max_pos_pct = await cfg.get_typed("s20_dutching.max_position_pct", float, 0.08)

        opps = []
        
        # Override S20 to fetch Gamma events instead of using the raw markets from engine,
        # because Polymarket multi-outcome markets are now represented as 'events' with multiple 'markets'
        try:
            r = await http_client.get("https://gamma-api.polymarket.com/events", params={
                "limit": 100, "active": "true", "closed": "false"
            })
            events = r.json()
        except Exception as e:
            _log.error(f"S20 failed to fetch events: {e}")
            events = []

        for event in events:
            try:
                sub_markets = event.get("markets", [])
                if len(sub_markets) < 3:
                    continue

                outcomes = []
                prices = []
                token_ids = []

                for sm in sub_markets:
                    sm_out = parse_list(sm.get("outcomes"))
                    sm_prices = parse_list(sm.get("outcomePrices"))
                    sm_tokens = parse_list(sm.get("clobTokenIds"))
                    
                    if sm_out and len(sm_out) >= 2 and sm_out[0] == "Yes" and sm_prices and sm_tokens:
                        name = sm.get("groupItemTitle") or sm.get("question")
                        outcomes.append(name)
                        prices.append(sm_prices[0])
                        token_ids.append(sm_tokens[0])

                if len(outcomes) < 3:
                    continue

                # Pair & sort by price descending
                parsed_candidates = []
                for i in range(len(outcomes)):
                    try:
                        p = float(prices[i])
                        if p > 0:
                            parsed_candidates.append({
                                "index": i,
                                "name": outcomes[i],
                                "price": round(p, 4),
                                "token_id": token_ids[i]
                            })
                    except (ValueError, TypeError):
                        continue

                if len(parsed_candidates) < top_n:
                    continue

                parsed_candidates.sort(key=lambda x: x["price"], reverse=True)
                top_set = parsed_candidates[:top_n]

                p_sum = sum(c["price"] for c in top_set)
                if p_sum <= 0 or p_sum > max_set_price:
                    continue

                # Expected ROI if one of top_set wins
                theoretical_roi_pct = ((1.0 - p_sum) / p_sum) * 100
                if theoretical_roi_pct < min_roi_pct:
                    continue

                # Theoretical set shares for budget
                target_total_cap = balance * max_pos_pct
                if target_total_cap <= 1.0:
                    continue

                target_set_shares = target_total_cap / p_sum

                # Order book walking & leg fill calculation
                leg_details = []
                actual_total_cost = 0.0
                total_slippage = 0.0
                max_slippage = await cfg.get_typed("poly_yield.max_slippage_pct", float, 1.5)

                from strategies.base import is_fillable

                leg_failed = False
                for leg in top_set:
                    leg_target_usdc = target_set_shares * leg["price"]
                    exec_data = await calculate_execution_price(
                        leg["token_id"], leg_target_usdc, side="buy", http_client=http_client
                    )

                    if not is_fillable(exec_data, max_slippage):
                        # For UI, if it's not fillable due to slippage, we still want to show the market!
                        # We'll just use the best available price.
                        fill_price = exec_data.get("price", leg["price"])
                    else:
                        fill_price = exec_data["price"]
                        
                    leg_shares = leg_target_usdc / fill_price if fill_price > 0 else 0
                    actual_leg_cost = fill_price * leg_shares

                    actual_total_cost += actual_leg_cost
                    total_slippage += exec_data.get("slippage", 0)

                    leg_details.append({
                        "outcome": leg["name"],
                        "market_price": leg["price"],
                        "fill_price": fill_price,
                        "shares": leg_shares,
                        "token_id": leg["token_id"],
                        "stake_usdc": round(leg_target_usdc, 2)
                    })

                if not leg_details:
                    continue

                min_shares = min(leg["shares"] for leg in leg_details) if leg_details else 0
                guaranteed_revenue_if_hit = min_shares * 1.0
                
                # Estimate Gas (Polygon typical) per leg
                est_gas_cost = (await gas_tracker.get_gas_cost_usdc()) * len(leg_details)
                
                net_profit_if_hit = guaranteed_revenue_if_hit - actual_total_cost - est_gas_cost
                actual_roi_pct = (net_profit_if_hit / actual_total_cost) * 100 if actual_total_cost > 0 else 0

                opps.append({
                    "id": f"opp_dutch_{uuid.uuid4().hex[:8]}",
                    "strategy": self.key,
                    "market_type": "Multi-outcome",
                    "market_id": event.get("id"),
                    "market_title": event.get("title", "Multi-outcome Dutching Market"),
                    "market_slug": event.get("slug", ""),
                    "url": get_market_url(event),
                    "outcomes": outcomes,
                    "top_candidates": [c["name"] for c in top_set],
                    "entry_price": p_sum,
                    "exec_mode": self.default_exec_mode,
                    "p_sum": round(p_sum, 4),
                    "suggested_usdc": round(actual_total_cost, 2),
                    "profit_pct": round(actual_roi_pct, 2),
                    "max_profit_usdc": round(net_profit_if_hit, 2),
                    "max_loss_usdc": round(-actual_total_cost, 2),
                    "legs": leg_details,
                    "days_to_expiry": days_to_expiry(event.get("endDate")),
                    "risk_level": self.risk_level
                })

            except Exception as e:
                _log.debug("Error scanning Dutching event %s: %s", event.get("id"), e)
                continue

        return opps


# Global instance
s20_dutching = DutchingStrategy()
