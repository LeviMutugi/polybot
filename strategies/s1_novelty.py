"""
Strategy S1: Novelty Yield
Description: Buys NO outcomes on low-probability meme, entertainment, or celebrity markets.
Captures a reliable yield premium when retail speculative interest overprices longshot YES outcomes.
"""
from typing import List
import httpx
from strategies.base import BaseStrategy, parse_list, days_to_expiry, get_market_url, calculate_execution_price, NOVELTY_TAGS
from db.config import cfg
from services.gas_tracker import gas_tracker

class NoveltyYieldStrategy(BaseStrategy):
    def __init__(self):
        super().__init__(
            key="s1_novelty",
            name="Novelty Yield",
            risk_level="High",
            market_type="Binary",
            default_exec_mode="semi"
        )

    async def scan(self, markets: List[dict], balance: float, http_client: httpx.AsyncClient) -> List[dict]:
        enabled = await cfg.get_typed("s1_novelty.enabled", bool, True)
        if not enabled:
            return []

        max_yes_price = await cfg.get_typed("s1_novelty.max_yes_price", float, 0.08)
        min_apy = await cfg.get_typed("s1_novelty.min_apy", float, 4.0)
        max_pos_pct = await cfg.get_typed("s1_novelty.max_position_pct", float, 0.02)
        exec_mode = await cfg.get_typed("s1_novelty.exec_mode", str, "semi")
        
        opps = []
        scan_gas_usdc = await gas_tracker.get_gas_cost_usdc()

        for market in markets:
            try:
                # Check tags for novelty categories
                tags = [t.get("label", "").lower() for t in (market.get("tags") or [])]
                if not any(tag in NOVELTY_TAGS for tag in tags):
                    continue

                outcomes = parse_list(market.get("outcomes"))
                prices = parse_list(market.get("outcomePrices"))
                token_ids = parse_list(market.get("clobTokenIds"))

                if len(outcomes) != 2 or len(prices) != 2:
                    continue  # Only binary YES/NO

                yes_idx = next((i for i, o in enumerate(outcomes) if "yes" in o.lower()), 0)
                no_idx = 1 - yes_idx

                yes_price = float(prices[yes_idx])
                no_price = float(prices[no_idx])

                if yes_price > max_yes_price or yes_price <= 0.001:
                    continue

                # Parse expiry
                end_dt = market.get("endDate") or market.get("end_date_iso")
                days = days_to_expiry(end_dt)
                if days is None or days <= 0:
                    continue

                # Sizing
                suggested_usdc = max(0.50, balance * max_pos_pct)
                token_id_no = token_ids[no_idx] if no_idx < len(token_ids) else None
                if not token_id_no:
                    continue

                # Walk book L2 depth
                exec_data = await calculate_execution_price(token_id_no, suggested_usdc, side="buy", http_client=http_client)
                if "error" in exec_data:
                    continue

                real_no_price = exec_data["price"]
                slippage = exec_data.get("slippage", 0)

                shares = suggested_usdc / real_no_price if real_no_price > 0 else 0
                if shares <= 0:
                    continue

                # Recalculate net APY deducting transaction gas
                net_gain_per_share = (1.0 - real_no_price) - (scan_gas_usdc / shares)
                net_hold_yield = net_gain_per_share / real_no_price
                from strategies.base import calculate_compounding_apy
                real_apy = calculate_compounding_apy(net_hold_yield, days)

                if real_apy < min_apy:
                    continue

                # Calibrate probability of success (since we buy NO, success is YES not happening)
                from strategies.calibration import longshot_calibrator
                est_true_yes_prob = yes_price * longshot_calibrator.get_correction(yes_price)
                est_true_no_prob = 1.0 - est_true_yes_prob

                market_url = get_market_url(market)
                opps.append({
                    "id": f"s1_{market.get('id', '')}",
                    "strategy": self.key,
                    "market_id": str(market.get("id", "")),
                    "market_title": market.get("question", ""),
                    "market_url": market_url,
                    "outcome": outcomes[no_idx],
                    "entry_price": round(real_no_price, 4),
                    "yes_price": round(yes_price, 4),
                    "no_price": round(no_price, 4),
                    "implied_prob": round(real_no_price * 100, 2), # Probability of success = prob of NO
                    "est_true_prob": round(est_true_no_prob * 100, 2),
                    "slippage_bps": round(slippage * 100, 2),
                    "annualized_apy": round(real_apy, 2),
                    "profit_pct": round(net_hold_yield * 100, 2),
                    "days_to_expiry": round(days, 1),
                    "action": "buy_no",
                    "exec_mode": exec_mode,
                    "suggested_usdc": round(suggested_usdc, 2),
                    "token_id": token_id_no,
                    "notes": f"YES at {yes_price:.1%}. Buy NO at ${real_no_price:.4f} VWAP. Gas ${scan_gas_usdc:.3f}, slippage {slippage:.2f}%.",
                    "status": "open",
                    "instructions": [
                        f"Open: {market_url}",
                        f"Buy NO for ${round(suggested_usdc, 2)} USDC",
                        f"Hold to expiry for a risk-adjusted {round(real_apy, 1)}% APY."
                    ]
                })

            except Exception:
                continue

        return opps

    async def execute(self, opportunity: dict, clob_client) -> dict:
        """S1 Order execution: Buy NO token."""
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
