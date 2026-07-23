"""
Strategy S3: Buy-All Exhaustive Arbitrage
Description: Exploits multi-outcome markets where the sum of prices of all outcomes is less than $1.00.
Buying all outcomes in proportional shares guarantees a risk-free payout of $1.00 per share.

Also covers neg-risk baskets: most modern Polymarket multi-outcome events (e.g.
"who will win the election") are represented as several SEPARATE binary Yes/No
markets sharing a neg-risk group id, rather than one native multi-outcome market
object — the plain pass above (which needs >= 3 outcomes on a single market)
never sees these. HONEST CAVEAT: this bot cannot verify the exact Gamma field
name for that grouping against a live API from this environment; the lookup
in _neg_risk_group_key() is fully defensive (plain `.get()`, try/except per
group) so if the assumed field name is wrong this pass just finds zero extra
groups rather than trading on a wrong assumption — the existing native-market
pass is completely unaffected either way.
"""
import uuid
import asyncio
from typing import List, Optional
import httpx
from strategies.base import (
    BaseStrategy, parse_list, days_to_expiry, get_market_url,
    calculate_execution_price, calculate_simple_apy, is_fillable,
)
from db.config import cfg
from services.gas_tracker import gas_tracker

def _neg_risk_group_key(market: dict) -> Optional[str]:
    """Best-effort neg-risk group id lookup across the field-name variants seen
    in different Gamma API responses/versions. Returns None (skip) if absent."""
    for key in ("negRiskMarketID", "negRiskMarketId", "neg_risk_market_id"):
        val = market.get(key)
        if val:
            return str(val)
    return None

class BuyAllArbitrageStrategy(BaseStrategy):
    def __init__(self):
        super().__init__(
            key="s3_buy_all",
            name="Buy-All Arb",
            risk_level="Low",
            market_type="Multi-outcome",
            default_exec_mode="auto",
            payoff_type="guaranteed_arb"
        )

    async def scan(self, markets: List[dict], balance: float, http_client: httpx.AsyncClient) -> List[dict]:
        enabled = await cfg.get_typed("s3_buy_all.enabled", bool, True)
        if not enabled:
            return []

        min_profit_pct = await cfg.get_typed("s3_buy_all.min_profit_pct", float, 0.5)
        max_pos_pct = await cfg.get_typed("s3_buy_all.max_position_pct", float, 0.10)
        exec_mode = await cfg.get_typed("s3_buy_all.exec_mode", str, "auto")
        max_slippage = await cfg.get_typed("poly_yield.max_slippage_pct", float, 1.5)

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
                        "stake_usdc": round(leg_target_usdc, 2),
                        "market_id": str(market.get("id", ""))
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
                    "payoff_type": self.payoff_type,
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

        # --- Pass 2: neg-risk baskets (see module docstring) ---
        neg_risk_groups: dict = {}
        for m in markets:
            if len(parse_list(m.get("outcomes"))) != 2:
                continue
            group_key = _neg_risk_group_key(m)
            if group_key:
                neg_risk_groups.setdefault(group_key, []).append(m)

        for group_key, group_markets in neg_risk_groups.items():
            if len(group_markets) < 3:
                continue
            try:
                legs_raw = []
                for m in group_markets:
                    outcomes = parse_list(m.get("outcomes"))
                    prices = parse_list(m.get("outcomePrices"))
                    token_ids = parse_list(m.get("clobTokenIds"))
                    if len(outcomes) != 2 or len(prices) != 2:
                        continue
                    yes_idx = next((i for i, o in enumerate(outcomes) if "yes" in o.lower()), 0)
                    yes_price = float(prices[yes_idx])
                    yes_token = token_ids[yes_idx] if yes_idx < len(token_ids) else None
                    if yes_price <= 0 or not yes_token:
                        continue
                    legs_raw.append({"market": m, "price": yes_price, "token_id": yes_token})

                if len(legs_raw) < 3:
                    continue

                sum_prices = sum(l["price"] for l in legs_raw)
                if sum_prices >= 1.0:
                    continue

                theoretical_profit_pct = (1.0 - sum_prices) / sum_prices * 100
                if theoretical_profit_pct < min_profit_pct:
                    continue

                total_cap = balance * max_pos_pct
                shares_target = total_cap / sum_prices

                actual_total_cost = 0.0
                leg_details = []
                total_slippage = 0.0
                aborted = False

                for l in legs_raw:
                    leg_target_usdc = shares_target * l["price"]
                    exec_data = await calculate_execution_price(l["token_id"], leg_target_usdc, side="buy", http_client=http_client)
                    if not is_fillable(exec_data, max_slippage):
                        aborted = True
                        break

                    leg_fill_price = exec_data["price"]
                    leg_shares = leg_target_usdc / leg_fill_price if leg_fill_price > 0 else 0
                    actual_total_cost += leg_fill_price * leg_shares
                    total_slippage += exec_data.get("slippage", 0)

                    leg_details.append({
                        "outcome": "Yes",
                        "price": round(l["price"], 4),
                        "fill_price": leg_fill_price,
                        "shares": leg_shares,
                        "token_id": l["token_id"],
                        "stake_usdc": round(leg_target_usdc, 2),
                        # Each leg is its OWN market — settlement resolves it independently
                        # (same leg shape as S5's sub-basket / S20 Dutching).
                        "market_id": str(l["market"].get("id", "")),
                    })

                if aborted:
                    continue

                min_shares = min(leg["shares"] for leg in leg_details)
                net_guaranteed_revenue = min_shares * 1.0
                total_gas = len(leg_details) * scan_gas_usdc
                total_expense = actual_total_cost + total_gas
                real_profit_usdc = net_guaranteed_revenue - total_expense
                real_profit_pct = (real_profit_usdc / total_expense) * 100 if total_expense > 0 else 0

                if real_profit_pct < min_profit_pct:
                    continue

                rep_market = group_markets[0]
                rep_title = rep_market.get("eventTitle") or rep_market.get("groupTitle") or rep_market.get("question", "Neg-Risk Basket")
                days = days_to_expiry(rep_market.get("endDate"))
                avg_slippage = total_slippage / len(leg_details)

                opps.append({
                    "id": f"s3_negrisk_{group_key}",
                    "strategy": self.key,
                    "market_id": str(rep_market.get("id", group_key)),
                    "market_title": f"{rep_title} (Neg-Risk Basket, {len(leg_details)} outcomes)",
                    "market_url": get_market_url(rep_market),
                    "market_type": "Multi-outcome (neg-risk)",
                    "outcome": f"BASKET ({len(leg_details)} outcomes, neg-risk)",
                    "entry_price": round(sum_prices, 4),
                    "slippage_bps": round(avg_slippage * 100, 2),
                    "annualized_apy": round(calculate_simple_apy(real_profit_pct, days), 2) if days else None,
                    "profit_pct": round(real_profit_pct, 3),
                    "days_to_expiry": round(days, 1) if days else None,
                    "action": "buy_all",
                    "exec_mode": exec_mode,
                    "suggested_usdc": round(total_cap, 2),
                    "payoff_type": self.payoff_type,
                    "legs": leg_details,
                    "status": "open",
                    "notes": (
                        f"Neg-risk basket arb: {len(leg_details)} separate binary markets sharing group "
                        f"{group_key}, YES-price sum = {sum_prices:.4f} < 1.00. Net profit ${real_profit_usdc:.2f} "
                        f"after gas ${total_gas:.2f}."
                    ),
                    "instructions": [
                        f"This is a basket of {len(leg_details)} separate Polymarket markets (neg-risk group).",
                        f"Buy YES on ALL {len(leg_details)} outcomes proportionally.",
                        "Each leg lives on its own market — settlement resolves per-leg independently."
                    ]
                })
            except Exception:
                continue

        opps.sort(key=lambda x: x["profit_pct"], reverse=True)
        return opps

    async def execute(self, opportunity: dict, clob_client) -> dict:
        """S3 execution is handled by the engine's multi-leg parallel dispatch."""
        return {"success": False, "error": "S3 Buy-All must be executed via engine multi-leg dispatch"}
