"""
Portfolio Allocator and Capital Manager
Provides mathematically informed money management (Kelly Criterion & Sharpe optimization).
Includes in-memory locking to prevent self-competing orders and double-spending.
"""
import asyncio
import logging
import math
from db.database import get_sqlite, _sqlite_lock
from db.config import cfg

_log = logging.getLogger(__name__)

class AllocationDeniedError(Exception):
    """Exception raised when capital allocation is denied due to risk limits."""
    pass


class PortfolioAllocator:
    """Manages capital allocation, risk checks, and trade locking."""

    def __init__(self):
        # Maps market_id -> asyncio.Lock to prevent race conditions on the same market
        self._market_locks = {}
        self._global_lock = asyncio.Lock()

    async def get_market_lock(self, market_id: str) -> asyncio.Lock:
        """Retrieve or create a lock for a specific market to prevent competing orders."""
        async with self._global_lock:
            if market_id not in self._market_locks:
                self._market_locks[market_id] = asyncio.Lock()
            return self._market_locks[market_id]

    async def calculate_kelly_size(self, implied_price: float, true_prob: float, total_capital: float) -> float:
        """
        Kelly Criterion Sizing for Binary Markets.
        Formula: f* = fraction * (p - P) / (1 - P)
        where:
          - p = true probability
          - P = implied price
        """
        if true_prob <= implied_price:
            return 0.0

        kelly_frac = await cfg.get_typed("portfolio.kelly_fraction", float, 0.10)
        
        # Avoid division by zero if implied price is 1.0 (unlikely)
        denom = 1.0 - implied_price
        if denom <= 0:
            return 0.0

        f_star = (true_prob - implied_price) / denom
        allocated_pct = f_star * kelly_frac
        
        # Cap allocation at 20% of capital per single trade for safety
        allocated_pct = min(0.20, max(0.0, allocated_pct))
        
        return total_capital * allocated_pct

    async def check_circuit_breakers(self) -> bool:
        """
        Check if any circuit breakers are triggered.
        Returns True if a circuit breaker is triggered (trading should be halted).
        """
        reason = await self.get_circuit_breaker_reason()
        return reason is not None

    async def get_circuit_breaker_reason(self) -> str | None:
        """
        Check if any circuit breakers are active and return a description.
        """
        mode = await cfg.get_typed("poly_yield.active_mode", str, "paper")
        cb_mode = await cfg.get_typed(f"portfolio.circuit_breaker_active.{mode}", str, "false")
        cb_global = await cfg.get_typed("portfolio.circuit_breaker_active", str, "false")
        if cb_mode.lower() == "true" or cb_global.lower() == "true":
            return f"Circuit breaker is manually active for {mode} mode"

        conn = get_sqlite()
        with _sqlite_lock:
            # 1. Daily Loss Limit Check
            daily_limit_str = conn.execute("SELECT value FROM system_config WHERE key = 'portfolio.daily_loss_limit'").fetchone()
            daily_limit = float(daily_limit_str["value"]) if daily_limit_str and daily_limit_str["value"] else None
            if daily_limit:
                daily_pnl_row = conn.execute(
                    "SELECT SUM(realized_pnl) as pnl FROM poly_yield_positions WHERE status IN ('won', 'lost') AND settled_at >= date('now') AND mode = ?", [mode]
                ).fetchone()
                daily_loss = -daily_pnl_row["pnl"] if daily_pnl_row and daily_pnl_row["pnl"] is not None else 0.0
                if daily_loss >= daily_limit:
                    _log.critical("[Allocator] Daily loss limit exceeded ($%.2f >= $%.2f). Halting new allocations.", daily_loss, daily_limit)
                    # Auto disable for the current mode
                    conn.execute("INSERT OR REPLACE INTO system_config (key, value) VALUES (?, 'true')", [f"portfolio.circuit_breaker_active.{mode}"])
                    if mode == "live":
                        conn.execute("INSERT OR REPLACE INTO system_config (key, value) VALUES ('poly_yield.enabled', 'false')")
                    conn.commit()
                    from services.alerts import alert
                    asyncio.create_task(alert.send(f"CRITICAL: Daily loss limit exceeded (${daily_loss:.2f} >= ${daily_limit:.2f}). Circuit breaker activated for {mode} mode.", level="critical"))
                    return f"Daily loss limit reached (${daily_loss:.2f} >= ${daily_limit:.2f})"

            # 2. Consecutive Losses Circuit Breaker Check
            consec_limit_str = conn.execute("SELECT value FROM system_config WHERE key = 'portfolio.consecutive_loss_limit'").fetchone()
            consec_limit = int(consec_limit_str["value"]) if consec_limit_str and consec_limit_str["value"] else None
            if consec_limit:
                recent = conn.execute(
                    f"SELECT status FROM poly_yield_positions WHERE status IN ('won', 'lost') AND mode = ? ORDER BY settled_at DESC LIMIT {consec_limit}", [mode]
                ).fetchall()
                if len(recent) == consec_limit and all(r["status"] == "lost" for r in recent):
                    _log.critical("[Allocator] Circuit breaker triggered: %d consecutive losses. Disabling engine for %s mode.", consec_limit, mode)
                    # Auto disable for the current mode
                    conn.execute("INSERT OR REPLACE INTO system_config (key, value) VALUES (?, 'true')", [f"portfolio.circuit_breaker_active.{mode}"])
                    conn.execute("INSERT OR REPLACE INTO system_config (key, value) VALUES ('poly_yield.enabled', 'false')")
                    conn.commit()
                    from services.alerts import alert
                    asyncio.create_task(alert.send(f"CRITICAL: Bot disabled. Circuit breaker triggered after {consec_limit} consecutive losses in {mode} mode.", level="critical"))
                    return f"Consecutive loss limit reached ({consec_limit} consecutive losses)"
        return None

    async def request_allocation(self, strategy_key: str, market_id: str, suggested_usdc: float, 
                                 implied_price: float = None, true_prob: float = None) -> float:
        """
        Validates capital availability, checks drawdown limits, and returns allowed USDC.
        Raises AllocationDeniedError if allocation is denied.
        """
        is_live = (await cfg.get_typed("poly_yield.active_mode", str, "paper")).lower() == "live"
        mode = "live" if is_live else "paper"
        
        enabled = await cfg.get_typed("poly_yield.enabled", bool, True)
        if not enabled:
            raise AllocationDeniedError("Trading engine is disabled in settings")

        # Check circuit breakers
        cb_reason = await self.get_circuit_breaker_reason()
        if cb_reason:
            raise AllocationDeniedError(f"Circuit breaker active: {cb_reason}")

        # Query wallet balance (simulated or real)
        total_balance = await self._get_wallet_balance()
        if total_balance <= 0:
            raise AllocationDeniedError(f"Wallet balance is empty or unconfigured (${total_balance:.2f})")

        # Calculate current total exposure & check safety limits
        conn = get_sqlite()
        with _sqlite_lock:

            # 3. Exposure Calculation
            row = conn.execute(
                "SELECT SUM(cost_usdc) as exposure FROM poly_yield_positions WHERE status = 'open' AND mode = ?", [mode]
            ).fetchone()
            current_exposure = row["exposure"] if row and row["exposure"] is not None else 0.0

        # Drawdown check
        drawdown_limit_pct = await cfg.get_typed("poly_yield.auto_exec_drawdown_limit", float, 50.0)
        max_allowed_exposure = total_balance * (drawdown_limit_pct / 100.0)

        # Check execution mode of strategy (manual, semi, auto)
        if strategy_key in ("manual", "manual_trade"):
            exec_mode = "manual"
        else:
            exec_mode = await cfg.get_typed(f"{strategy_key}.exec_mode", str, "auto")
        
        # Sanitize Kelly inputs: both MUST be fractions in (0, 1). Anything else
        # (e.g. a percent passed by mistake) falls back to fixed-fraction sizing
        # instead of silently producing a max-size Kelly bet.
        kelly_inputs_valid = (
            implied_price is not None and true_prob is not None
            and 0.0 < float(implied_price) < 1.0
            and 0.0 < float(true_prob) <= 1.0
        )
        if (implied_price is not None and true_prob is not None) and not kelly_inputs_valid:
            _log.warning("[Allocator] Kelly inputs out of range (implied=%s, true=%s) — falling back to fixed sizing",
                         implied_price, true_prob)

        # Capital Allocation calculation
        if exec_mode == "manual":
            # Direct manual capital allocated
            allocated_usdc = suggested_usdc
        else:
            # Automatic / Semi-Automatic Kelly sizing
            if kelly_inputs_valid:
                if true_prob <= implied_price:
                    raise AllocationDeniedError(
                        f"Kelly sizing denied: True probability ({true_prob*100:.1f}%) <= Implied price ({implied_price*100:.1f}%)"
                    )
                allocated_usdc = await self.calculate_kelly_size(implied_price, true_prob, total_balance)
                if allocated_usdc <= 0.0:
                    raise AllocationDeniedError("Kelly sizing calculated size is $0.00")
            else:
                # Default fallback sizing
                max_pos_pct = await cfg.get_typed(f"{strategy_key}.max_position_pct", float, 0.05)
                allocated_usdc = total_balance * max_pos_pct
            # Never allocate more than the strategy suggested for this opportunity
            if suggested_usdc is not None and suggested_usdc >= 0:
                allocated_usdc = min(allocated_usdc, suggested_usdc)

        # Clip allocation to available room inside drawdown limits
        remaining_room = max_allowed_exposure - current_exposure
        if remaining_room <= 0:
            raise AllocationDeniedError(
                f"Drawdown limit reached. Exposure: ${current_exposure:.2f}, "
                f"Limit: ${max_allowed_exposure:.2f} ({drawdown_limit_pct}% of total balance ${total_balance:.2f})"
            )

        final_allocation = min(allocated_usdc, remaining_room)
        
        # Floor trade sizes at $0.50 (minimum Polymarket trade size)
        if final_allocation < 0.50:
            if remaining_room < 0.50:
                raise AllocationDeniedError(
                    f"Insufficient remaining drawdown room: only ${remaining_room:.2f} left (min $0.50)"
                )
            else:
                raise AllocationDeniedError(
                    f"Allocated size ${final_allocation:.2f} is under Polymarket minimum of $0.50"
                )
            
        return round(final_allocation, 2)

    async def _get_wallet_balance(self) -> float:
        """Query USDC wallet balance based on active mode."""
        mode = (await cfg.get_typed("poly_yield.active_mode", str, "paper")).lower()
        if mode == "paper":
            return await cfg.get_typed("portfolio.paper_balance", float, 1000.0)

        # Live Mode
        tradeable_limit = await cfg.get_typed("portfolio.tradeable_limit", float, 100.0)
        actual_balance = 0.0
        try:
            from services.keystore import keystore
            pk = keystore.get_decrypted("polymarket_wallet")
            if pk:
                from eth_account import Account
                from config import settings
                import httpx
                acct = Account.from_key(pk)
                rpc = settings.polygon_rpc_url
                usdc_contract = settings.polygon_usdc_contract
                async with httpx.AsyncClient() as client:
                    r = await client.post(rpc, json={
                        "jsonrpc": "2.0", "method": "eth_call", "id": 1,
                        "params": [{
                            "to": usdc_contract,
                            # ERC20 balanceOf function selector is 0x70a08231
                            "data": f"0x70a08231000000000000000000000000{acct.address[2:].lower()}"
                        }, "latest"]
                    })
                    if r.status_code == 200:
                        res = r.json().get("result", "0x0")
                        actual_balance = int(res, 16) / 1e6  # 6 decimals
        except Exception as e:
            _log.error("[Allocator] Failed to fetch live wallet balance: %s", e)
            
        if actual_balance <= 0.0:
            return 0.0
            
        # Bound the available balance by the user's tradeable limit
        return min(actual_balance, tradeable_limit)

# Singleton instance
portfolio_allocator = PortfolioAllocator()
