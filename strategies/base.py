"""
Base Strategy Interface & Utilities
Provides the BaseStrategy class and shared utility functions (VWAP order book walking, date parsing).
"""
import json
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional
import httpx
from config import settings

_log = logging.getLogger(__name__)

# Constants
NOVELTY_TAGS = {
    "culture", "religion", "entertainment", "climate", "weather",
    "celebrity", "meme", "space", "science", "paranormal", "other",
    "pop culture", "society", "music", "movies"
}

class BaseStrategy:
    def __init__(self, key: str, name: str, risk_level: str, market_type: str, default_exec_mode: str = "manual"):
        self.key = key
        self.name = name
        self.risk_level = risk_level  # 'Low', 'Medium', 'High'
        self.market_type = market_type  # 'Binary', 'Multi-outcome', 'Event-based'
        self.default_exec_mode = default_exec_mode  # 'manual', 'semi', 'auto'

    async def scan(self, markets: List[dict], balance: float, http_client: httpx.AsyncClient) -> List[dict]:
        raise NotImplementedError("Strategy must implement scan()")

    async def execute(self, opportunity: dict, clob_client) -> dict:
        raise NotImplementedError("Strategy must implement execute()")


# --- Shared Quant Utility Functions ---

def parse_list(val) -> list:
    """Safely parse JSON lists or list variables."""
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except json.JSONDecodeError:
            return []
    return []

def days_to_expiry(end_date_str) -> Optional[float]:
    """Calculate the fractional number of days to expiration."""
    if not end_date_str:
        return None
    try:
        from dateutil import parser as dp
        end = dp.parse(str(end_date_str))
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = (end - now).total_seconds() / 86400.0
        return max(0.0, delta)
    except Exception as e:
        _log.debug("days_to_expiry parsing failed for %r: %s", end_date_str, e)
        return None

def get_market_url(market: dict) -> str:
    """Construct Polymarket deep links to prevent 404s via redirection."""
    base = settings.polymarket_web_url.rstrip("/")
    slug = market.get("slug")
    if not slug:
        return f"{base}/market/{market.get('id', '')}"
    return f"{base}/market/{slug}"

def sort_book_levels(levels: list, side: str) -> list:
    """
    Normalize CLOB book levels to best-price-first order regardless of API ordering.
    (Polymarket's /book returns bids ascending and asks descending — best price LAST —
    so naive iteration walks the WORST prices first.)
    Asks: ascending price (cheapest first). Bids: descending price (highest first).
    """
    def _price(lvl):
        try:
            return float(lvl["price"])
        except (TypeError, ValueError, KeyError):
            return 0.0
    return sorted(levels, key=_price, reverse=(side == "bids"))

async def calculate_execution_price(token_id: str, amount_usdc: float, side: str = "buy", http_client: httpx.AsyncClient = None) -> dict:
    """
    Volume-Weighted Average Price (VWAP) walking for L2 Order Book Depth.
    Calculates execution price and expected slippage for a specific USDC order size.
    """
    if not token_id or amount_usdc <= 0:
        return {"price": 0.5, "slippage": 0, "error": "Invalid params"}

    clob_url = settings.polymarket_clob_url.rstrip("/")
    close_client = False
    if http_client is None:
        http_client = httpx.AsyncClient(timeout=10.0)
        close_client = True

    try:
        r = await http_client.get(f"{clob_url}/book", params={"token_id": token_id})
        if r.status_code != 200:
            return {"price": 0.5, "slippage": 0, "error": "CLOB book unreachable"}

        data = r.json()
        bids = sort_book_levels(data.get("bids") or [], "bids")
        asks = sort_book_levels(data.get("asks") or [], "asks")
        levels = asks if side == "buy" else bids
        if not levels:
            return {"price": 0.0, "slippage": 100.0, "error": "No liquidity"}

        total_filled_usdc = 0.0
        total_shares = 0.0

        # Best prices after normalization: element 0 is top of book
        best_bid = float(bids[0]["price"]) if bids else 0.0
        best_ask = float(asks[0]["price"]) if asks else 1.0
        base_price = best_ask if side == "buy" else best_bid
        if base_price == 0:
            base_price = 0.5

        for lvl in levels:
            price = float(lvl["price"])
            size = float(lvl["size"])
            lvl_usdc = price * size
            
            remaining = amount_usdc - total_filled_usdc
            if lvl_usdc >= remaining:
                shares = remaining / price
                total_shares += shares
                total_filled_usdc += remaining
                break
            else:
                total_shares += size
                total_filled_usdc += lvl_usdc

        if total_filled_usdc < amount_usdc:
            avg_price = total_filled_usdc / total_shares if total_shares > 0 else 0
            return {
                "price": round(avg_price, 4),
                "slippage": 99.0,
                "warning": f"Insufficient liquidity (only ${total_filled_usdc:.2f} available)"
            }

        avg_price = total_filled_usdc / total_shares
        
        # Calculate slippage percentage
        if side == "buy":
            slippage = ((avg_price - base_price) / base_price) * 100
        else:
            slippage = ((base_price - avg_price) / base_price) * 100

        return {
            "price": round(avg_price, 4),
            "slippage": round(slippage, 2),
            "available_usdc": round(total_filled_usdc, 2)
        }
    except Exception as e:
        return {"price": 0.5, "slippage": 0, "error": str(e)}
    finally:
        if close_client:
            await http_client.aclose()


def is_fillable(exec_data: dict, max_slippage_pct: float) -> bool:
    """
    True when a VWAP book walk indicates the order can actually be filled
    within the slippage tolerance. Guards against both explicit errors and the
    'insufficient liquidity' warning path (which reports slippage 99).
    """
    if not exec_data or "error" in exec_data:
        return False
    if "warning" in exec_data:
        return False
    if float(exec_data.get("price") or 0) <= 0:
        return False
    if float(exec_data.get("slippage") or 0) > max_slippage_pct:
        return False
    return True

APY_CAP_PCT = 100000.0  # sanity ceiling so short-dated markets can't display absurd figures

def calculate_compounding_apy(net_yield: float, days: float) -> float:
    """
    Computes the annualized APY using compounding interest formula.
    APY = (1 + net_yield)^(365 / days) - 1
    Limits minimum days to 1.0 to prevent mathematical overflow for <1 day markets.
    """
    if days is None or days <= 0:
        return 0.0
    if net_yield is None:
        return 0.0
    if net_yield <= -1.0:
        return -100.0
    effective_days = max(1.0, float(days))

    # APY = (1 + Return)^(365/days) - 1
    try:
        apy = ((1.0 + net_yield) ** (365.0 / effective_days)) - 1.0
        return float(min(apy * 100.0, APY_CAP_PCT)) # Convert to percentage, capped
    except OverflowError:
        return APY_CAP_PCT

def calculate_simple_apy(net_yield_pct: float, days: float) -> float:
    """
    Simple (non-compounding) annualization of a percentage return.
    Floors days at 1.0 so intraday markets can't multiply by 3650x.
    """
    if days is None or days <= 0 or net_yield_pct is None:
        return 0.0
    effective_days = max(1.0, float(days))
    return float(min(net_yield_pct * (365.0 / effective_days), APY_CAP_PCT))
