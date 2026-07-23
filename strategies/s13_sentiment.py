"""
Strategy S13: Sentiment Index Tracker
Description: Scrapes social channels and news to weight sentiment and predict shifts.

HONEST LIMITATION: this bot does not integrate any news/social-media sentiment
feed (no Twitter/Reddit/news API is wired up anywhere in this codebase). What
follows is a MARKET-DERIVED SENTIMENT PROXY built only from data this bot already
has — CLOB order-book depth skew (more resting buy-side depth than sell-side depth
is read as bullish positioning) combined with recent price momentum (via
strategies.base's rolling in-process price history, same mechanism as S12) — NOT
true multi-source sentiment. It is deliberately kept in 'manual' mode by default:
review each read before trusting it.

Coming soon: true multi-source sentiment (GDELT global news/event data plus
other complementary signals) is on the roadmap to replace/augment this proxy —
tracked in the UI as a separate "coming soon" item so the two are never conflated.
"""
from typing import List, Optional
import httpx
from strategies.base import (
    BaseStrategy, parse_list, days_to_expiry, get_market_url, sort_book_levels,
    calculate_execution_price, calculate_simple_apy, is_fillable,
    record_price_sample, price_change_pct,
)
from config import settings
from db.config import cfg
from services.gas_tracker import gas_tracker


async def _book_imbalance(http_client: httpx.AsyncClient, token_id: str, depth_levels: int = 5) -> Optional[float]:
    """Resting bid depth vs. ask depth (USDC-weighted, top N levels), scaled to
    [-1, 1]. Positive = more buy-side depth (bullish skew), negative = more
    sell-side depth (bearish skew). None if the book is empty/unreachable."""
    clob_url = settings.polymarket_clob_url.rstrip("/")
    try:
        r = await http_client.get(f"{clob_url}/book", params={"token_id": token_id})
        if r.status_code != 200:
            return None
        data = r.json()
        bids = sort_book_levels(data.get("bids") or [], "bids")[:depth_levels]
        asks = sort_book_levels(data.get("asks") or [], "asks")[:depth_levels]
        bid_depth = sum(float(l["price"]) * float(l["size"]) for l in bids)
        ask_depth = sum(float(l["price"]) * float(l["size"]) for l in asks)
        total = bid_depth + ask_depth
        if total <= 0:
            return None
        return (bid_depth - ask_depth) / total
    except Exception:
        return None


class SentimentStrategy(BaseStrategy):
    def __init__(self):
        super().__init__(
            key="s13_sentiment",
            name="Sentiment Tracker",
            risk_level="Medium",
            market_type="Binary",
            default_exec_mode="manual"
        )

    async def scan(self, markets: List[dict], balance: float, http_client: httpx.AsyncClient) -> List[dict]:
        enabled = await cfg.get_typed(f"{self.key}.enabled", bool, True)
        if not enabled:
            return []

        lookback_s = await cfg.get_typed(f"{self.key}.lookback_s", float, 1800.0)
        momentum_norm_pct = await cfg.get_typed(f"{self.key}.momentum_norm_pct", float, 10.0)
        score_threshold = await cfg.get_typed(f"{self.key}.score_threshold", float, 0.35)
        max_pos_pct = await cfg.get_typed(f"{self.key}.max_position_pct", float, 0.02)
        min_apy = await cfg.get_typed(f"{self.key}.min_apy", float, 3.0)
        assumed_hold_days = await cfg.get_typed(f"{self.key}.assumed_hold_days", float, 2.0)
        exec_mode = await cfg.get_typed(f"{self.key}.exec_mode", str, "manual")
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
                yes_token = token_ids[yes_idx] if yes_idx < len(token_ids) else None
                if not yes_token:
                    continue

                imbalance = await _book_imbalance(http_client, yes_token)

                market_id = str(market.get("id", ""))
                history = record_price_sample(f"{self.key}:{market_id}", yes_price)
                momentum_pct = price_change_pct(history, lookback_s)
                momentum_norm = None
                if momentum_pct is not None and momentum_norm_pct > 0:
                    momentum_norm = max(-1.0, min(1.0, momentum_pct / momentum_norm_pct))

                signals = [s for s in (imbalance, momentum_norm) if s is not None]
                if not signals:
                    continue  # warm-up / no order book data yet
                composite_score = sum(signals) / len(signals)

                if abs(composite_score) < score_threshold:
                    continue

                target_idx = yes_idx if composite_score > 0 else no_idx
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
                        f"Market-derived sentiment PROXY (order-flow depth skew + price momentum), "
                        f"score {composite_score:+.2f} (threshold {score_threshold:.2f}) favors "
                        f"'{outcomes[target_idx]}' at ${real_price:.4f}. This is NOT news/social "
                        f"sentiment — true multi-source sentiment (GDELT etc.) is coming soon. "
                        f"Manual mode by default: review before trusting."
                    ),
                    "instructions": [
                        f"Open: {market_url}",
                        f"Review the composite sentiment proxy score ({composite_score:+.2f}) before acting.",
                        f"If convinced, buy '{outcomes[target_idx]}' for ${round(suggested_usdc, 2)} USDC at ~${real_price:.4f}."
                    ]
                })
            except Exception:
                continue

        opps.sort(key=lambda x: abs(x.get("annualized_apy") or 0), reverse=True)
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
