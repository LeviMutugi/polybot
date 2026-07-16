"""
Strategy S6: Longshot Bias Market Maker
Description: Systematically sells YES (buys NO) on highly overpriced longshot outcomes (YES < 8%).
Leverages the data-driven LongshotCalibrator to identify positive expected-value (EV) opportunities.
"""
import uuid
from typing import List
import httpx
from strategies.base import BaseStrategy, parse_list, days_to_expiry, get_market_url, calculate_execution_price
from strategies.calibration import longshot_calibrator
from db.config import cfg
from services.gas_tracker import gas_tracker

class LongshotMMStrategy(BaseStrategy):
    def __init__(self):
        super().__init__(
            key="s6_longshot",
            name="Longshot MM",
            risk_level="High",
            market_type="Binary",
            default_exec_mode="auto"
        )

    async def scan(self, markets: List[dict], balance: float, http_client: httpx.AsyncClient) -> List[dict]:
        enabled = await cfg.get_typed("s6_longshot.enabled", bool, True)
        if not enabled:
            return []

        max_yes_price = await cfg.get_typed("s6_longshot.max_yes_price", float, 0.08)
        max_positions = await cfg.get_typed("s6_longshot.max_positions", int, 10)
        position_pct = await cfg.get_typed("s6_longshot.position_pct", float, 0.02)
        exec_mode = await cfg.get_typed("s6_longshot.exec_mode", str, "auto")

        # Query open positions from database to check limits
        from db.database import get_sqlite, _sqlite_lock
        conn = get_sqlite()
        with _sqlite_lock:
            row = conn.execute(
                "SELECT COUNT(*) as c FROM poly_yield_positions WHERE strategy = ? AND status = 'open'",
                [self.key]
            ).fetchone()
        open_count = row["c"] if row else 0
        available_slots = max_positions - open_count

        if available_slots <= 0:
            return []

        opps = []
        scan_gas_usdc = await gas_tracker.get_gas_cost_usdc()

        for market in markets:
            if len(opps) >= available_slots:
                break

            try:
                outcomes = parse_list(market.get("outcomes"))
                prices = parse_list(market.get("outcomePrices"))
                token_ids = parse_list(market.get("clobTokenIds"))

                for i, (outcome, price_str) in enumerate(zip(outcomes, prices)):
                    if "yes" not in outcome.lower():
                        continue
                    yes_price = float(price_str)
                    if yes_price > max_yes_price or yes_price <= 0.001:
                        continue

                    # Check if we already have an open position in this market
                    with _sqlite_lock:
                        existing = conn.execute(
                            "SELECT id FROM poly_yield_positions WHERE market_id = ? AND outcome LIKE ? AND status = 'open'",
                            [str(market.get("id")), f"%{outcome}%"]
                        ).fetchone()
                    if existing:
                        continue

                    # Calibration calculation
                    implied_prob = yes_price
                    correction_factor = longshot_calibrator.get_correction(implied_prob)
                    est_true_prob = implied_prob * correction_factor
                    edge_pct = (implied_prob - est_true_prob) / implied_prob * 100

                    # Walk bid book to calculate true sell price (buying NO asks)
                    pos_usdc = max(0.50, balance * position_pct)
                    token_id = token_ids[i] if i < len(token_ids) else None
                    if not token_id:
                        continue

                    # Sell YES is equivalent to buying NO. CLOB client supports buy order on NO token.
                    # NO token index
                    no_idx = 1 - i
                    token_id_no = token_ids[no_idx] if no_idx < len(token_ids) else None
                    if not token_id_no:
                        continue

                    exec_data = await calculate_execution_price(token_id_no, pos_usdc, side="buy", http_client=http_client)
                    if "error" in exec_data:
                        continue

                    real_no_price = exec_data["price"]
                    slippage = exec_data.get("slippage", 0)

                    # Real implied sell price of YES
                    real_sell_price = 1.0 - real_no_price

                    shares = pos_usdc / real_no_price
                    net_gas_per_share = scan_gas_usdc / shares

                    # Expected Value math
                    ev_win_weight = (real_sell_price - net_gas_per_share) * (1 - est_true_prob)
                    ev_loss_weight = (1.0 - real_sell_price + net_gas_per_share) * est_true_prob
                    expected_value_usdc = (ev_win_weight - ev_loss_weight) * shares

                    if expected_value_usdc <= 0:
                        continue

                    days = days_to_expiry(market.get("endDate"))
                    
                    # Calculate APY for buying NO
                    net_yield = expected_value_usdc / pos_usdc
                    from strategies.base import calculate_compounding_apy
                    annualized_apy = calculate_compounding_apy(net_yield, days)

                    market_url = get_market_url(market)

                    opps.append({
                        "id": f"s6_{market.get('id','')}_{i}",
                        "strategy": self.key,
                        "market_id": str(market.get("id", "")),
                        "market_title": market.get("question", ""),
                        "market_url": market_url,
                        "outcome": f"SELL YES on '{outcome}'",
                        "entry_price": round(real_no_price, 4),
                        "yes_price": round(yes_price, 4),
                        "no_price": round(real_no_price, 4),
                        "implied_prob": round(real_no_price * 100, 2), # implied probability of success (NO)
                        "est_true_prob": round((1.0 - est_true_prob) * 100, 2), # true probability of success (NO)
                        "slippage_bps": round(slippage * 100, 2),
                        "expected_value_usdc": round(expected_value_usdc, 2),
                        "profit_pct": round(edge_pct, 2),
                        "annualized_apy": round(annualized_apy, 2),
                        "days_to_expiry": round(days, 1) if days else None,
                        "action": "sell_yes",
                        "exec_mode": exec_mode,
                        "suggested_usdc": round(pos_usdc, 2),
                        "token_id": token_id_no,
                        "status": "open",
                        "notes": f"YES implied at {implied_prob:.1%} but calibrated true prob is {est_true_prob:.1%}. Edge: {edge_pct:.1f}%.",
                        "instructions": [
                            f"Open: {market_url}",
                            f"Buy NO shares (equivalent to selling YES) for ${round(pos_usdc, 2)} USDC.",
                            f"Yield is derived from longshot overpricing bias. Hold to expiry."
                        ]
                    })
                    break  # One opportunity per market

            except Exception:
                continue

        opps.sort(key=lambda x: x["expected_value_usdc"], reverse=True)
        return opps

    async def execute(self, opportunity: dict, clob_client) -> dict:
        """S6 Order execution: Buy NO token."""
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
