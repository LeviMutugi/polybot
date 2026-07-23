"""
Strategy S14: Macro Event Correlation Hedger
Description: Hedged positions across correlated macro/economic binary pairs.

HONEST LIMITATION: no real macro/economic data feed (SPX, Fed funds futures, CPI,
etc.) is integrated anywhere in this codebase. What follows is an intra-platform
proxy, in the same safe spirit as S4 Correlation Arb: the user curates PAIRS of
Polymarket markets they've confirmed should trade at roughly the same YES price
(e.g. two different phrasings/venues of a related macro question). No loose
substring auto-matching between arbitrary markets is ever used to auto-trade —
a wrong pair means betting on two things that aren't actually related, so this
strategy produces ZERO opportunities until pairs are curated. Configure via
config key s14_macro_corr.correlation_pairs as JSON:
    [["keyword for market A", "keyword for market B"], ...]

When a curated pair's YES prices diverge beyond min_gap_pct, this buys YES on
whichever side is priced LOWER — the thesis being it's underpriced relative to
its confirmed-correlated partner and should converge. This is a single-leg
directional convergence bet, not a guaranteed arbitrage — correlation can break.
"""
import json
from typing import List
import httpx
from strategies.base import BaseStrategy, parse_list, days_to_expiry, get_market_url, calculate_execution_price, is_fillable, calculate_simple_apy
from db.config import cfg
from services.gas_tracker import gas_tracker

class MacroCorrStrategy(BaseStrategy):
    def __init__(self):
        super().__init__(
            key="s14_macro_corr",
            name="Macro Correlation",
            risk_level="Medium",
            market_type="Binary",
            default_exec_mode="manual"
        )

    async def scan(self, markets: List[dict], balance: float, http_client: httpx.AsyncClient) -> List[dict]:
        enabled = await cfg.get_typed(f"{self.key}.enabled", bool, True)
        if not enabled:
            return []

        min_gap_pct = await cfg.get_typed(f"{self.key}.min_gap_pct", float, 3.0)
        max_pos_pct = await cfg.get_typed(f"{self.key}.max_position_pct", float, 0.03)
        exec_mode = await cfg.get_typed(f"{self.key}.exec_mode", str, "manual")
        max_slippage = await cfg.get_typed("poly_yield.max_slippage_pct", float, 1.5)

        raw_pairs = await cfg.get_async(f"{self.key}.correlation_pairs", "[]")
        try:
            pairs = json.loads(raw_pairs)
            if not isinstance(pairs, list):
                pairs = []
            pairs = [p for p in pairs if isinstance(p, (list, tuple)) and len(p) == 2]
        except Exception:
            pairs = []
        if not pairs:
            return []

        mkt_index = {}
        for m in markets:
            q = (m.get("question") or "").lower()
            outcomes = parse_list(m.get("outcomes"))
            prices = parse_list(m.get("outcomePrices"))
            token_ids = parse_list(m.get("clobTokenIds"))
            if len(outcomes) == 2 and len(prices) == 2:
                yes_idx = next((i for i, o in enumerate(outcomes) if "yes" in o.lower()), 0)
                yes_price = float(prices[yes_idx])
                mkt_index[q] = {
                    "market": m,
                    "yes_price": yes_price,
                    "token_id": token_ids[yes_idx] if yes_idx < len(token_ids) else None,
                }

        opps = []
        scan_gas_usdc = await gas_tracker.get_gas_cost_usdc()

        for kw_a, kw_b in pairs:
            kw_a_l, kw_b_l = str(kw_a).lower(), str(kw_b).lower()
            a_mkts = [(q, v) for q, v in mkt_index.items() if kw_a_l in q]
            b_mkts = [(q, v) for q, v in mkt_index.items() if kw_b_l in q]

            for aq, av in a_mkts:
                for bq, bv in b_mkts:
                    try:
                        price_a, price_b = av["yes_price"], bv["yes_price"]
                        gap_pct = abs(price_a - price_b) * 100
                        if gap_pct < min_gap_pct:
                            continue

                        if price_a <= price_b:
                            target_q, target_v, peer_q, peer_v, peer_kw = aq, av, bq, bv, kw_b
                        else:
                            target_q, target_v, peer_q, peer_v, peer_kw = bq, bv, aq, av, kw_a

                        token_id = target_v["token_id"]
                        if not token_id:
                            continue

                        suggested_usdc = max(0.50, balance * max_pos_pct)
                        exec_data = await calculate_execution_price(token_id, suggested_usdc, side="buy", http_client=http_client)
                        if not is_fillable(exec_data, max_slippage):
                            continue

                        real_price = exec_data["price"]
                        slippage = exec_data.get("slippage", 0)
                        gas_impact_pct = (scan_gas_usdc / suggested_usdc) * 100
                        net_gap_pct = gap_pct - gas_impact_pct
                        if net_gap_pct < min_gap_pct:
                            continue

                        days = days_to_expiry(target_v["market"].get("endDate"))
                        market_url = get_market_url(target_v["market"])

                        opps.append({
                            "id": f"{self.key}_{target_v['market'].get('id', '')}_{abs(hash(peer_q)) % 100000}",
                            "strategy": self.key,
                            "market_id": str(target_v["market"].get("id", "")),
                            "market_title": target_v["market"].get("question", ""),
                            "market_url": market_url,
                            "outcome": "Yes",
                            "entry_price": round(real_price, 4),
                            "implied_prob": round(real_price * 100, 2),
                            "slippage_bps": round(slippage * 100, 2),
                            "annualized_apy": round(calculate_simple_apy(net_gap_pct, days), 2) if days else None,
                            "profit_pct": round(net_gap_pct, 2),
                            "days_to_expiry": round(days, 1) if days else None,
                            "action": "buy_yes",
                            "exec_mode": exec_mode,
                            "suggested_usdc": round(suggested_usdc, 2),
                            "token_id": token_id,
                            "status": "open",
                            "notes": (
                                f"Correlated-pair proxy (intra-platform, NOT real macro/economic data): "
                                f"'{target_q}' YES at {target_v['yes_price']:.1%} vs. correlated "
                                f"'{peer_q}' YES at {peer_v['yes_price']:.1%}. Gap {gap_pct:.2f}%."
                            ),
                            "instructions": [
                                f"Open: {market_url}",
                                f"Buy YES for ${round(suggested_usdc, 2)} USDC",
                                f"Thesis: underpriced vs. user-confirmed correlated partner ('{peer_kw}'). "
                                f"This is a directional convergence bet, not a guaranteed arb — correlation can break."
                            ]
                        })
                    except Exception:
                        continue

        opps.sort(key=lambda x: x["profit_pct"], reverse=True)
        return opps

    async def execute(self, opportunity: dict, clob_client) -> dict:
        """Buy YES on the underpriced side of the correlated pair."""
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
