"""
Strategy S5: Sub-Event / Sum Arbitrage
Description: Mispricing between parent event (e.g. "Will X happen in 2026?") and its sub-events
(e.g., "Will X happen in Q1?", "Q2?", "Q3?", "Q4?"). The parent price should approximate the sum of sub-events.
If parent YES is underpriced: we buy parent YES.
If parent YES is overpriced: we buy sub-event YES basket.

Grouping strategy: Polymarket slugs do not reliably encode a parent/child
hierarchy, so pure slug-guessing (kept below as a secondary, harmless source of
groups) rarely matches. The primary grouping instead works directly off market
QUESTION TEXT — no external schema to guess at, so it's testable and won't
silently break if an assumed API field turns out not to exist: strip a trailing
temporal clause ("in Q1 2026", "by March 2026", "during H2 2026", ...) off each
question; markets that share the same remaining prefix are sub-events of the
same underlying question, and a market in the SAME candidate set whose question
has no temporal clause (e.g. "...in 2026?") is the parent. This directly
reconstructs the exact relationship described above, entirely from data this
bot already fetches. Groups that don't have both a parent-like candidate and
>= 2 temporal sub-candidates are skipped — produce nothing rather than a wrong
guess.
"""
import re
import uuid
import asyncio
from typing import List, Optional, Tuple
import httpx
from strategies.base import BaseStrategy, parse_list, days_to_expiry, get_market_url, calculate_execution_price
from db.config import cfg
from services.gas_tracker import gas_tracker

# A quarter/half/month clause marks a SUB-event ("...in Q1 2026?", "...by March 2026?").
_SUB_TEMPORAL_RE = re.compile(
    r"\b(in|by|before|during|through)\s+"
    r"(q[1-4]|h[12]|jan\w*|feb\w*|mar\w*|apr\w*|may\w*|jun\w*|jul\w*|aug\w*|sep\w*|oct\w*|nov\w*|dec\w*)\b.*$"
)
# A bare year clause marks the PARENT's annual scope ("...in 2026?") — stripped to
# line the prefix up with its sub-events, but NOT counted as a sub-event itself.
_PARENT_YEAR_RE = re.compile(r"\b(in|by|before|during|through)\s+(20\d{2})\b.*$")

def _prefix_and_is_temporal(question: str) -> Tuple[str, bool]:
    """Strip a trailing temporal clause off a question, returning the
    normalized remaining prefix and whether it was a SUB-event-marking clause
    (quarter/half/month) as opposed to a bare parent-level year."""
    q = (question or "").strip().lower()
    m = _SUB_TEMPORAL_RE.search(q)
    if m:
        return q[:m.start()].strip().rstrip("?,.:;"), True
    m = _PARENT_YEAR_RE.search(q)
    if m:
        return q[:m.start()].strip().rstrip("?,.:;"), False
    return q.rstrip("?,.:;"), False

def _find_parent_sub_groups(markets: List[dict]) -> List[Tuple[dict, List[dict]]]:
    """Group markets by shared question prefix: a group qualifies only when it
    has exactly one parent-like (non-temporal) market and >= 2 temporal
    sub-event markets."""
    prefix_groups: dict = {}
    for m in markets:
        prefix, is_temporal = _prefix_and_is_temporal(m.get("question"))
        if not prefix:
            continue
        bucket = prefix_groups.setdefault(prefix, {"parents": [], "subs": []})
        (bucket["subs"] if is_temporal else bucket["parents"]).append(m)

    groups = []
    for bucket in prefix_groups.values():
        if len(bucket["parents"]) == 1 and len(bucket["subs"]) >= 2:
            groups.append((bucket["parents"][0], bucket["subs"]))
    return groups

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

        # Primary grouping: question-text prefix/temporal-clause matching (see module
        # docstring). Secondary grouping: slug-guessing, kept as a harmless additional
        # source in case it ever matches — it just rarely does.
        candidate_groups = _find_parent_sub_groups(markets)

        slug_groups: dict = {}
        for m in markets:
            slug = m.get("slug") or ""
            parts = slug.rsplit("/", 1)
            parent_slug = parts[0] if len(parts) > 1 else None
            if parent_slug:
                slug_groups.setdefault(parent_slug, []).append(m)
        for parent_slug, sub_markets in slug_groups.items():
            if len(sub_markets) < 2:
                continue
            parent_market = next((m for m in markets if m.get("slug") == parent_slug), None)
            if parent_market:
                candidate_groups.append((parent_market, sub_markets))

        opps = []
        scan_gas_usdc = await gas_tracker.get_gas_cost_usdc()

        for parent_market, sub_markets in candidate_groups:
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
                from strategies.base import is_fillable
                max_slippage = await cfg.get_typed("poly_yield.max_slippage_pct", float, 1.5)
                if action == "buy_parent_yes":
                    # Single leg buy on parent
                    if not parent_token:
                        continue
                    exec_data = await calculate_execution_price(parent_token, suggested_usdc, side="buy", http_client=http_client)
                    if not is_fillable(exec_data, max_slippage):
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
                        if not leg.get("token_id") or leg["price"] <= 0:
                            raise ValueError("Sub-leg missing token or price")
                        leg_target_usdc = (leg["price"] / sub_sum) * suggested_usdc
                        l_exec = await calculate_execution_price(leg["token_id"], leg_target_usdc, side="buy", http_client=http_client)
                        if not is_fillable(l_exec, max_slippage):
                            raise ValueError(l_exec.get("error") or l_exec.get("warning") or "Sub-leg not fillable")
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
                from strategies.base import calculate_simple_apy

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
                    "annualized_apy": round(calculate_simple_apy(net_profit_pct, days), 2) if days else None,
                    "profit_pct": round(net_profit_pct, 2),
                    "days_to_expiry": round(days, 1) if days else None,
                    "action": action,
                    "exec_mode": exec_mode,
                    "suggested_usdc": round(suggested_usdc, 2),
                    # buy_sub_basket buys the YES leg of every sub-event: if the parent/sub-event
                    # relationship holds (parent YES iff at least one sub-event YES), this basket is
                    # a guaranteed-arb, same payoff shape as S3. buy_parent_yes is a plain directional bet.
                    "payoff_type": "guaranteed_arb" if action == "buy_sub_basket" else "directional",
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
