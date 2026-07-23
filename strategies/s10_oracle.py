"""
Strategy S10: Oracle Discrepancy Exploitation
Description: Monitors real-time API feeds (e.g. live sports, elections) to front-run CLOB updates.

Real implementation using only data this bot already has (Gamma's endDate/closed
fields — no external live-feed integration exists here): Polymarket markets are
only formally settled once UMA's optimistic-oracle dispute window closes, which
regularly lags the market's own stated endDate by hours to a few days. During that
lag, `closed` is still `false` even though the event has already happened and the
CLOB price has already converged to near-certainty. That gap — a market the crowd
has effectively already resolved, but the oracle hasn't formally caught up to yet —
is the real, distinct "oracle discrepancy" this strategy buys into (distinct from
S8 Late-Stage, which targets near-certain markets that HAVEN'T reached their end
date yet).
"""
from datetime import datetime, timezone
from typing import List, Optional
import httpx
from strategies.base import (
    BaseStrategy, parse_list, get_market_url,
    calculate_execution_price, calculate_simple_apy, is_fillable,
)
from db.config import cfg
from services.gas_tracker import gas_tracker


def _days_past_expiry(end_date_str) -> Optional[float]:
    """Unlike strategies.base.days_to_expiry (which clamps at 0), this returns a
    POSITIVE number of days already elapsed since endDate, or None if endDate is
    missing/unparseable/still in the future."""
    if not end_date_str:
        return None
    try:
        from dateutil import parser as dp
        end = dp.parse(str(end_date_str))
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        elapsed_days = (now - end).total_seconds() / 86400.0
        return elapsed_days if elapsed_days > 0 else None
    except Exception:
        return None


class OracleStrategy(BaseStrategy):
    def __init__(self):
        super().__init__(
            key="s10_oracle",
            name="Oracle Discrepancy",
            risk_level="Medium",
            market_type="Event-based",
            default_exec_mode="semi"
        )

    async def scan(self, markets: List[dict], balance: float, http_client: httpx.AsyncClient) -> List[dict]:
        enabled = await cfg.get_typed(f"{self.key}.enabled", bool, True)
        if not enabled:
            return []

        min_price = await cfg.get_typed(f"{self.key}.min_price", float, 0.97)
        max_days_past = await cfg.get_typed(f"{self.key}.max_days_past_expiry", float, 14.0)
        # No direct API tells us WHEN UMA will finish settling — this is the assumed
        # holding period used only to annualize the yield estimate for display/ranking.
        assumed_settlement_days = await cfg.get_typed(f"{self.key}.assumed_settlement_days", float, 2.0)
        max_pos_pct = await cfg.get_typed(f"{self.key}.max_position_pct", float, 0.03)
        min_apy = await cfg.get_typed(f"{self.key}.min_apy", float, 2.0)
        exec_mode = await cfg.get_typed(f"{self.key}.exec_mode", str, "semi")
        max_slippage = await cfg.get_typed("poly_yield.max_slippage_pct", float, 1.5)

        opps = []
        scan_gas_usdc = await gas_tracker.get_gas_cost_usdc()

        for market in markets:
            try:
                # closed=true means Gamma already reflects the settlement — nothing
                # left to front-run. Only markets still "active" past their own
                # endDate are the lag window this strategy targets.
                if market.get("closed"):
                    continue

                days_past = _days_past_expiry(market.get("endDate") or market.get("end_date_iso"))
                if days_past is None or days_past > max_days_past:
                    continue

                outcomes = parse_list(market.get("outcomes"))
                prices = parse_list(market.get("outcomePrices"))
                token_ids = parse_list(market.get("clobTokenIds"))
                if len(outcomes) != 2 or len(prices) != 2:
                    continue

                for i, p_str in enumerate(prices):
                    p = float(p_str)
                    if not (min_price <= p < 0.999):
                        continue

                    token_id = token_ids[i] if i < len(token_ids) else None
                    if not token_id:
                        continue

                    suggested_usdc = max(1.0, balance * max_pos_pct)
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
                    real_apy = calculate_simple_apy(net_yield_pct, assumed_settlement_days)
                    if real_apy < min_apy or net_yield_pct <= 0:
                        continue

                    market_url = get_market_url(market)
                    opps.append({
                        "id": f"{self.key}_{market.get('id', '')}_{i}",
                        "strategy": self.key,
                        "market_id": str(market.get("id", "")),
                        "market_title": market.get("question", ""),
                        "market_url": market_url,
                        "outcome": outcomes[i],
                        "entry_price": round(real_price, 4),
                        "implied_prob": round(real_price * 100, 2),
                        "slippage_bps": round(slippage * 100, 2),
                        "annualized_apy": round(real_apy, 2),
                        "profit_pct": round(net_yield_pct, 2),
                        # Not a time-TO-expiry position (it's already past expiry) — the
                        # UI's "days to expiry" column shows the assumed wait for settlement.
                        "days_to_expiry": round(assumed_settlement_days, 1),
                        "action": "buy",
                        "exec_mode": exec_mode,
                        "suggested_usdc": round(suggested_usdc, 2),
                        "token_id": token_id,
                        "status": "open",
                        "notes": (
                            f"Market ended {days_past:.1f}d ago but is not yet formally closed/settled. "
                            f"'{outcomes[i]}' already priced at ${real_price:.4f}, implying the oracle "
                            f"settlement is a formality. Net yield {net_yield_pct:.2f}% assuming "
                            f"{assumed_settlement_days:.1f}d to settlement."
                        ),
                        "instructions": [
                            f"Open: {market_url}",
                            f"Buy '{outcomes[i]}' for ${round(suggested_usdc, 2)} USDC at ~${real_price:.4f}",
                            f"Hold until UMA formally settles (market ended {days_past:.1f}d ago already)."
                        ]
                    })
                    break  # One opportunity per market
            except Exception:
                continue

        opps.sort(key=lambda x: x["annualized_apy"], reverse=True)
        return opps

    async def execute(self, opportunity: dict, clob_client) -> dict:
        """Buy the already-decided outcome token pending formal oracle settlement."""
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
