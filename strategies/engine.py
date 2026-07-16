"""
PolyYield Core Strategy Engine
Coordinates scanning loops, order book evaluations, capital allocation, and transaction safety guards.
"""
import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
import httpx

from config import settings
from db.database import get_sqlite, _sqlite_lock
from db.config import cfg
from services.gas_tracker import gas_tracker
from services.alerts import alert
from services.portfolio_allocator import portfolio_allocator
from strategies.registry import load_strategies, get_all_strategies, get_strategy

_log = logging.getLogger(__name__)

GAMMA_URL = settings.polymarket_gamma_url.rstrip("/")
CLOB_URL = settings.polymarket_clob_url.rstrip("/")

# WebSocket connections registry for broadcasting updates
active_websockets = []

class PolyYieldEngine:
    def __init__(self):
        self._running = False
        self._task = None
        self._http = None
        self._clob_client = None
        self._scan_count = 0
        self._killswitch = False

    async def start(self):
        """Start the background strategy scanning loop."""
        if self._running:
            return
        self._running = True
        self._killswitch = False
        self._http = httpx.AsyncClient(timeout=20.0)
        await self._init_clob()
        
        # Load strategies dynamically
        load_strategies()
        
        self._task = asyncio.create_task(self._scan_loop(), name="poly_yield_engine")
        print("[PolyYieldEngine] Core scanning loop started")

    async def stop(self):
        """Gracefully stop scanning and close HTTP client."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._http:
            await self._http.aclose()
        print("[PolyYieldEngine] Core scanning loop stopped")

    async def _init_clob(self):
        """Initialize Polymarket CLOB client if credentials exist."""
        try:
            from services.keystore import keystore
            pk = keystore.get_decrypted("polymarket_wallet")
            if pk:
                from py_clob_client.client import ClobClient
                chain_id = await cfg.get_typed("polygon_chain_id", int, 137)
                clob_url = CLOB_URL
                if chain_id == 80002:
                    clob_url = "https://clob-testnet.polymarket.com"
                self._clob_client = ClobClient(
                    host=clob_url,
                    key=pk,
                    chain_id=chain_id,
                )
                try:
                    self._clob_client.set_api_creds(
                        self._clob_client.create_or_derive_api_creds()
                    )
                    print("[PolyYieldEngine] CLOB Client authenticated successfully")
                except Exception as e:
                    print(f"[PolyYieldEngine] CLOB credentials warning (read-only mode): {e}")
            else:
                print("[PolyYieldEngine] No wallet key stored — running in read-only/paper mode")
        except Exception as e:
            print(f"[PolyYieldEngine] CLOB Client initialization failed: {e}")

    async def _scan_loop(self):
        await asyncio.sleep(2.0)
        while self._running:
            if self._killswitch:
                await asyncio.sleep(5.0)
                continue

            try:
                enabled = await cfg.get_typed("poly_yield.enabled", bool, True)
                if enabled:
                    await self._run_scans()
            except Exception as e:
                _log.error("Scan loop error: %s", e)

            # Retrieve scan interval
            interval = await cfg.get_typed("poly_yield.scan_interval_s", int, 120)
            await asyncio.sleep(max(10, interval))

    async def _run_scans(self):
        self._scan_count += 1
        print(f"[PolyYieldEngine] Running Scan #{self._scan_count}...")

        # Fetch active markets from Gamma API
        markets = await self._fetch_markets()
        if not markets:
            return

        # Query dynamic wallet balance
        balance = await self._get_wallet_balance()
        if balance <= 0:
            mode = await cfg.get_typed("poly_yield.active_mode", str, "paper")
            if mode == "live":
                _log.warning("[Engine] Live mode but wallet balance is $0. Skipping scan.")
                return
            balance = 1000.0  # Simulated paper balance

        # Run scans across all registered strategies
        strategies = get_all_strategies()
        all_opps = []

        for strat in strategies:
            # Check if strategy is enabled in DB
            strat_enabled = await cfg.get_typed(f"{strat.key}.enabled", bool, True)
            if not strat_enabled:
                continue

            try:
                opps = await strat.scan(markets, balance, self._http)
                for opp in opps:
                    all_opps.append(opp)
                    # Upsert opportunity to DB (for UI display)
                    await self._upsert_opportunity(opp)
            except Exception as ex:
                _log.error("Strategy %s scan failed: %s", strat.key, ex)
                
        # Filter for auto-execution and rank by priority metric
        # EV = (Return / Risk) * Prob of Success. We use expected_value_usdc or annualized_apy as proxy
        auto_opps = [opp for opp in all_opps if opp.get("exec_mode") == "auto" and not self._killswitch]
        
        # Sort opportunities (descending) prioritizing expected value or APY if EV not available
        auto_opps.sort(key=lambda x: (
            x.get("expected_value_usdc", 0) > 0, # Has positive EV calculation
            x.get("expected_value_usdc", 0),     # The EV amount itself
            x.get("annualized_apy", 0)           # Fallback to APY
        ), reverse=True)
        
        # Execute top-down
        for opp in auto_opps:
            if self._killswitch:
                break
            try:
                await self.execute_opportunity(opp)
            except Exception as ex:
                _log.error("Failed to execute opportunity %s: %s", opp.get("id"), ex)

        # Broadcast update to web clients
        await self._broadcast({"type": "scan_complete", "scan_count": self._scan_count, "opps_found": len(all_opps)})
        print(f"[PolyYieldEngine] Scan #{self._scan_count} finished. {len(all_opps)} opportunities found.")

    async def _fetch_markets(self, limit: int = 200) -> list:
        try:
            # Load current network chain ID
            chain_id = await cfg.get_typed("polygon_chain_id", int, settings.polygon_chain_id)
            # Use Gamma API params
            r = await self._http.get(f"{GAMMA_URL}/markets", params={
                "limit": limit,
                "active": "true",
                "closed": "false",
                "order": "liquidity",
                "ascending": "false",
            })
            if r.status_code == 200:
                return r.json()
        except Exception as e:
            _log.error("Error fetching markets from Gamma: %s", e)
        return []

    async def _get_wallet_balance(self) -> float:
        return await portfolio_allocator._get_wallet_balance()

    async def _upsert_opportunity(self, opp: dict):
        try:
            conn = get_sqlite()
            with _sqlite_lock:
                conn.execute("""
                    INSERT OR REPLACE INTO poly_yield_opportunities
                    (id, strategy, risk_level, execution_type, market_type, reward_score,
                     slippage_bps, market_id, market_title, market_url, outcome, entry_price,
                     implied_prob, yes_price, no_price, annualized_apy, profit_pct, days_to_expiry,
                     action, exec_mode, suggested_usdc, status, notes, instructions, legs, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
                """, [
                    opp.get("id"), opp.get("strategy"), opp.get("risk_level"), opp.get("exec_mode"),
                    opp.get("market_type", "Binary"), opp.get("reward_score", 0.0), opp.get("slippage_bps", 0.0),
                    opp.get("market_id"), opp.get("market_title"), opp.get("market_url"), opp.get("outcome"),
                    opp.get("entry_price"), opp.get("implied_prob"), opp.get("yes_price"), opp.get("no_price"),
                    opp.get("annualized_apy"), opp.get("profit_pct"), opp.get("days_to_expiry"),
                    opp.get("action"), opp.get("exec_mode"), opp.get("suggested_usdc"),
                    opp.get("status", "open"), opp.get("notes"),
                    json.dumps(opp.get("instructions")), json.dumps(opp.get("legs"))
                ])
                conn.commit()
            await self._broadcast({"type": "opportunity_update", "data": opp})
        except Exception as e:
            _log.error("Failed to upsert opportunity: %s", e)

    async def execute_opportunity(self, opp: dict) -> dict:
        """Centralized execution entry point with pre-flight, parallel dispatch, and rollback guards."""
        opp_id = opp.get("id")
        strat_key = opp.get("strategy")
        market_id = opp.get("market_id")
        suggested_usdc = float(opp.get("suggested_usdc", 1.0))
        
        # Idempotency guard: check if opportunity already has an open position
        conn = get_sqlite()
        with _sqlite_lock:
            existing = conn.execute(
                "SELECT id FROM poly_yield_positions WHERE opportunity_id = ? AND status = 'open'",
                [opp_id]
            ).fetchone()
        if existing:
            return {"success": False, "error": "Opportunity already has an open position (duplicate)"}

        # 1. Obtain market lock to prevent competing orders on the same market
        lock = await portfolio_allocator.get_market_lock(market_id)

        async with lock:
            # 2. Check risk & money allocation (Kelly sizing / drawdown limit check)
            from services.portfolio_allocator import AllocationDeniedError
            try:
                allocated_usdc = await portfolio_allocator.request_allocation(
                    strat_key, market_id, suggested_usdc,
                    implied_price=opp.get("entry_price"),
                    true_prob=opp.get("est_true_prob", None)
                )
            except AllocationDeniedError as e:
                return {"success": False, "error": f"Risk check denied: {str(e)}"}
            except Exception as e:
                return {"success": False, "error": f"Allocation check failed: {str(e)}"}

            # Update sizing in opportunity
            opp["suggested_usdc"] = allocated_usdc
            
            # Check mode (paper vs live)
            mode = await cfg.get_typed("poly_yield.active_mode", str, "paper")
            if mode == "paper":
                # Debit paper wallet BEFORE recording position
                from services.wallet import wallet_service, InsufficientFundsError, DuplicateTransactionError
                idem_key = f"open_{opp_id}"
                try:
                    wallet_service.debit("paper", allocated_usdc, "trade_open",
                                         position_id=None,
                                         description=f"Buy: {opp.get('market_title', '')[:80]}",
                                         idempotency_key=idem_key)
                except InsufficientFundsError as e:
                    return {"success": False, "error": str(e)}
                except DuplicateTransactionError:
                    return {"success": False, "error": "Trade already executed (duplicate)"}

                # Simulated execution success
                shares = allocated_usdc / opp["entry_price"]
                await self._record_position(opp, shares, allocated_usdc, "paper_order_id")
                await alert.send(f"Paper trade executed: {opp['market_title']} -> {opp['outcome']} for ${allocated_usdc} USDC", level="success")
                return {"success": True, "mode": "paper"}

            if not self._clob_client:
                return {"success": False, "error": "CLOB client not configured for live trading"}

            # 3. Handle multi-leg strategies (S3, S5 basket, etc.) with pre-flight walks & parallel dispatch
            legs = opp.get("legs", [])
            if legs and isinstance(legs, list):
                # Multi-leg S3 / S5 Buy-All Basket
                preflight_ok = True
                max_slippage = await cfg.get_typed("poly_yield.max_slippage_pct", float, 1.5)
                
                # Pre-flight check
                for leg in legs:
                    token_id = leg.get("token_id")
                    leg_stake = (leg.get("price", 0.3) / opp["entry_price"]) * allocated_usdc
                    leg["stake_usdc"] = round(leg_stake, 2)
                    
                    # Walk book for each leg
                    from strategies.base import calculate_execution_price
                    walk = await calculate_execution_price(token_id, leg_stake, side="buy", http_client=self._http)
                    if "error" in walk or walk.get("slippage", 0) > max_slippage:
                        preflight_ok = False
                        _log.warning("[Engine] Pre-flight aborted. Leg %s failed slippage/liquidity check.", leg.get("outcome"))
                        break
                
                if not preflight_ok:
                    return {"success": False, "error": "Multi-leg pre-flight check failed"}

                # Parallel dispatch
                tasks = [self._place_live_order(leg["token_id"], leg["stake_usdc"], leg["price"]) for leg in legs]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                placed = [r for r in results if isinstance(r, dict) and r.get("success")]
                failed = [r for r in results if isinstance(r, Exception) or (isinstance(r, dict) and not r.get("success"))]

                if failed:
                    # Trigger rollback cancellation loop & Killswitch
                    _log.critical("[Engine] Multi-leg partial fill detected! Rolling back placed legs.")
                    self._killswitch = True
                    await cfg.set_async("poly_yield.enabled", "false")
                    
                    for p in placed:
                        try:
                            self._clob_client.cancel(p["order_id"])
                        except Exception as ce:
                            _log.error("Failed to cancel leg order %s: %s", p["order_id"], ce)

                    await alert.send("CRITICAL: Multi-leg partial fill failed! Killswitch activated. Manual intervention required.", level="critical")
                    return {"success": False, "error": "Partial fill occurred. Rolled back and activated system killswitch."}

                # Success - record total position
                # Success - record total position
                total_cost = sum(p.get("fill_cost", 0) for p in placed)
                min_shares = min(p.get("fill_shares", 0) for p in placed) if placed else 0
                order_ids_str = ",".join(p["order_id"] for p in placed)
                await self._record_position(opp, min_shares, total_cost or allocated_usdc, order_ids_str, fill_price=opp.get("entry_price"))
                await alert.send(f"Live Basket trade success: {opp['market_title']} for ${total_cost or allocated_usdc:.2f} USDC", level="success")
                return {"success": True, "placed": len(placed)}

            else:
                # Single leg execution
                if strat_key in ("manual", "manual_trade"):
                    # Execute manual order directly
                    from py_clob_client.clob_types import OrderArgs
                    token_id = opp.get("token_id")
                    price = float(opp["entry_price"])
                    shares = round(allocated_usdc / price, 2)
                    try:
                        if mode == "live" and self._clob_client and token_id:
                            order = self._clob_client.create_order(OrderArgs(
                                price=price, size=shares, side="BUY", token_id=token_id
                            ))
                            resp = self._clob_client.post_order(order)
                            order_id = resp.get("orderID")
                            if not order_id:
                                return {"success": False, "error": f"No orderID returned: {resp}"}
                            fill_res = await self._verify_order_fill(order_id)
                            if not fill_res.get("success"):
                                return {"success": False, "error": f"Order placed but fill failed: {fill_res.get('error')}"}
                            shares = fill_res["fill_shares"]
                            allocated_usdc = fill_res["fill_cost"]
                            price = fill_res["fill_price"]
                        else:
                            order_id = "paper_manual_order_id"
                        
                        await self._record_position(opp, shares, allocated_usdc, order_id, fill_price=price)
                        await alert.send(f"Manual trade executed: {opp['market_title']} -> {opp['outcome']} for ${allocated_usdc:.2f} USDC", level="success")
                        return {"success": True, "order_id": order_id}
                    except Exception as e:
                        return {"success": False, "error": str(e)}

                strat = get_strategy(strat_key)
                if not strat:
                    return {"success": False, "error": f"Strategy {strat_key} not registered"}

                res = await strat.execute(opp, self._clob_client)
                if res.get("success"):
                    order_id = res.get("order_id")
                    # Poll for order fill!
                    fill_res = await self._verify_order_fill(order_id)
                    if fill_res.get("success"):
                        shares = fill_res["fill_shares"]
                        cost_usdc = fill_res["fill_cost"]
                        fill_price = fill_res["fill_price"]
                        await self._record_position(opp, shares, cost_usdc, order_id, fill_price=fill_price)
                        await alert.send(f"Live trade executed & filled: {opp['market_title']} -> {opp['outcome']} for ${cost_usdc:.2f} USDC at ${fill_price:.4f}", level="success")
                        return {"success": True, "order_id": order_id}
                    else:
                        return {"success": False, "error": f"Order was not filled: {fill_res.get('error')}"}
                else:
                    return {"success": False, "error": res.get("error")}

    async def _verify_order_fill(self, order_id: str, timeout_s: float = 10.0) -> dict:
        """Poll order status until FILLED, CANCELED, or timeout."""
        if not self._clob_client:
            # Paper trading / read-only fallback
            return {"success": True, "fill_shares": 0.0, "fill_cost": 0.0, "fill_price": 0.0}
        
        start_time = asyncio.get_event_loop().time()
        poll_interval = 1.0
        
        while asyncio.get_event_loop().time() - start_time < timeout_s:
            try:
                # get_order is a blocking network call, run in executor
                order = await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: self._clob_client.get_order(order_id)
                )
                if not order:
                    await asyncio.sleep(poll_interval)
                    continue

                status = str(order.get("status") or "").upper()
                size = float(order.get("size") or 0)
                matched_size = float(order.get("matchedSize") or 0)
                price = float(order.get("price") or 0)
                
                _log.info("[Engine] Order %s status: %s (matched %s/%s)", order_id, status, matched_size, size)
                
                if status == "FILLED":
                    return {
                        "success": True,
                        "status": "FILLED",
                        "fill_shares": matched_size,
                        "fill_price": price,
                        "fill_cost": matched_size * price
                    }
                elif status == "CANCELED":
                    return {
                        "success": False,
                        "status": "CANCELED",
                        "error": "Order was canceled"
                    }
                
            except Exception as e:
                _log.debug("Error polling order %s: %s", order_id, e)
                
            await asyncio.sleep(poll_interval)
            
        # Timeout reached, cancel remaining order size
        _log.warning("[Engine] Order %s timed out. Cancelling remaining...", order_id)
        try:
            await asyncio.to_thread(self._clob_client.cancel, order_id)
        except Exception as e:
            _log.error("Failed to cancel timed out order %s: %s", order_id, e)
            
        try:
            order = await asyncio.to_thread(self._clob_client.get_order, order_id)
            matched_size = float(order.get("matchedSize") or 0)
            price = float(order.get("price") or 0)
            if matched_size > 0:
                return {
                    "success": True,
                    "status": "PARTIALLY_FILLED_CANCELED",
                    "fill_shares": matched_size,
                    "fill_price": price,
                    "fill_cost": matched_size * price
                }
        except Exception:
            pass
            
        return {"success": False, "status": "TIMED_OUT", "error": "Order execution timed out"}

    async def _place_live_order(self, token_id: str, stake_usdc: float, price: float) -> dict:
        """Place a live buy limit order and verify its fill status."""
        from py_clob_client.clob_types import OrderArgs
        try:
            shares = round(stake_usdc / price, 2)
            order = self._clob_client.create_order(OrderArgs(
                price=price, size=shares, side="BUY", token_id=token_id
            ))
            resp = self._clob_client.post_order(order)
            order_id = resp.get("orderID")
            if not order_id:
                return {"success": False, "error": f"No orderID returned: {resp}"}
            # Verify fill status
            return await self._verify_order_fill(order_id)
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _record_position(self, opp: dict, shares: float, cost_usdc: float, order_id: str, fill_price: float = None):
        from strategies.base import days_to_expiry
        pos_id = f"pos_{uuid.uuid4().hex[:8]}"
        mode = await cfg.get_typed("poly_yield.active_mode", str, "paper")
        
        # Retrieve default stop-loss / take-profit percentages from config
        sl_pct_str = await cfg.get_async("portfolio.default_stop_loss_pct", "")
        tp_pct_str = await cfg.get_async("portfolio.default_take_profit_pct", "")
        ts_pct_str = await cfg.get_async("portfolio.default_trailing_stop_pct", "")
        
        entry_price = fill_price or opp.get("entry_price") or 0.5
        
        sl_price = None
        tp_price = None
        ts_pct = None
        
        if sl_pct_str:
            sl_price = round(entry_price * (1.0 - float(sl_pct_str) / 100.0), 4)
        if tp_pct_str:
            tp_price = round(entry_price * (1.0 + float(tp_pct_str) / 100.0), 4)
        if ts_pct_str:
            ts_pct = float(ts_pct_str)

        # Manual placement specific parameters (if supplied)
        if "stop_loss_price" in opp:
            sl_price = opp["stop_loss_price"]
        if "take_profit_price" in opp:
            tp_price = opp["take_profit_price"]
        if "trailing_stop_pct" in opp:
            ts_pct = opp["trailing_stop_pct"]

        try:
            conn = get_sqlite()
            with _sqlite_lock:
                conn.execute("""
                    INSERT INTO poly_yield_positions
                    (id, opportunity_id, strategy, market_id, market_title, outcome,
                     shares, entry_price, cost_usdc, order_id, status, entry_at,
                     predicted_apy, predicted_profit_pct, predicted_days_to_expiry,
                     actual_fill_price, actual_gas_usdc, risk_level, fill_slippage_bps,
                     quality_at_entry, predicted_pnl_usdc, mode,
                     stop_loss_price, take_profit_price, trailing_stop_pct, highest_price)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now'),?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, [
                    pos_id, opp.get("id"), opp.get("strategy"), opp.get("market_id"),
                    opp.get("market_title"), opp.get("outcome"),
                    shares, opp.get("entry_price"), cost_usdc, str(order_id), "open",
                    opp.get("annualized_apy"), opp.get("profit_pct"), opp.get("days_to_expiry"),
                    entry_price, 0.005, opp.get("risk_level"), opp.get("slippage_bps"),
                    opp.get("reward_score"), (cost_usdc * opp.get("profit_pct", 0.0) / 100.0 if opp.get("profit_pct") else 0.0),
                    mode, sl_price, tp_price, ts_pct, entry_price
                ])
                # Update opportunity status to closed/executed
                conn.execute("UPDATE poly_yield_opportunities SET status = 'executed' WHERE id = ?", [opp.get("id")])
                
                # Increment stats open positions count
                conn.execute("""
                    INSERT OR IGNORE INTO poly_yield_stats (strategy, mode) VALUES (?, ?)
                """, [opp["strategy"], mode])
                conn.execute("""
                    UPDATE poly_yield_stats SET open_positions = open_positions + 1 WHERE strategy = ? AND mode = ?
                """, [opp["strategy"], mode])
                conn.commit()
            await self._broadcast({"type": "position_opened", "pos_id": pos_id, "market": opp["market_title"]})
        except Exception as e:
            _log.error("CRITICAL: Failed to record position: %s", e)
            if mode == "live":
                self._killswitch = True
                await alert.send(
                    f"CRITICAL: Position recording failed after live order. "
                    f"Killswitch activated. Order ID: {order_id}",
                    level="critical"
                )
            raise  # Propagate — don't silently swallow

    async def exit_position(self, pos_id: str, current_price: float, reason: str = "Stop Loss") -> dict:
        """Sell/Exit an open position immediately, locking in PnL."""
        conn = get_sqlite()
        with _sqlite_lock:
            row = conn.execute("SELECT * FROM poly_yield_positions WHERE id = ? AND status = 'open'", [pos_id]).fetchone()
        if not row:
            return {"success": False, "error": "Open position not found"}
        
        pos = dict(row)
        token_id = None
        mode = pos["mode"]

        # Sourcing CLOB token ID from Gamma API if we are in live mode
        if mode == "live":
            try:
                r = await self._http.get(f"{GAMMA_URL}/markets/{pos['market_id']}")
                if r.status_code == 200:
                    market = r.json()
                    from strategies.base import parse_list
                    token_ids = parse_list(market.get("clobTokenIds"))
                    outcomes = parse_list(market.get("outcomes"))
                    for t_id, out in zip(token_ids, outcomes):
                        if out.strip().lower() == pos["outcome"].strip().lower():
                            token_id = t_id
                            break
            except Exception as e:
                _log.error("Failed to fetch token ID for exit: %s", e)

            if not token_id:
                return {"success": False, "error": "Could not resolve CLOB token ID for live exit"}

        shares = float(pos["shares"])
        cost = float(pos["cost_usdc"])
        
        realized_pnl = 0.0
        exit_price = current_price

        if mode == "live" and self._clob_client and token_id:
            from py_clob_client.clob_types import OrderArgs
            try:
                # Place limit sell order slightly lower to cross the spread and sell instantly
                order_price = max(0.01, round(current_price * 0.98, 4))
                order = self._clob_client.create_order(OrderArgs(
                    price=order_price, size=shares, side="SELL", token_id=token_id
                ))
                resp = self._clob_client.post_order(order)
                order_id = resp.get("orderID")
                if order_id:
                    fill_res = await self._verify_order_fill(order_id)
                    if fill_res.get("success"):
                        exit_price = fill_res["fill_price"]
                        realized_pnl = fill_res["fill_cost"] - cost - 0.005 # deduct gas estimate
                    else:
                        return {"success": False, "error": f"Sell order not filled: {fill_res.get('error')}"}
                else:
                    return {"success": False, "error": f"Failed to place sell order: {resp}"}
            except Exception as e:
                _log.error("Live exit execution failed: %s", e)
                return {"success": False, "error": str(e)}
        else:
            # Paper trading / Read-only exit
            realized_pnl = (shares * current_price) - cost

        status = "won" if realized_pnl >= 0 else "lost"

        # Credit paper wallet with returned capital (cost + pnl, floored at 0)
        if mode == "paper":
            from services.wallet import wallet_service
            return_amount = max(0.0, shares * current_price)
            wallet_service.credit("paper", return_amount, "trade_exit",
                                  position_id=pos_id,
                                  description=f"Exit ({reason}): {pos['market_title'][:80]}",
                                  idempotency_key=f"exit_{pos_id}")

        # total_returned = capital that flowed back to wallet (cost + pnl, floored at 0)
        return_amount = max(0.0, cost + realized_pnl)
        
        with _sqlite_lock:
            conn.execute("""
                UPDATE poly_yield_positions
                SET status = ?, settled_at = datetime('now'), realized_pnl = ?,
                    settlement_outcome = ?, actual_fill_price = ?
                WHERE id = ?
            """, [status, realized_pnl, f"exit_{reason.lower().replace(' ', '_')}", exit_price, pos_id])
            
            # Update strategy performance stats
            conn.execute("INSERT OR IGNORE INTO poly_yield_stats (strategy, mode) VALUES (?, ?)", [pos["strategy"], mode])
            win_inc = 1 if status == "won" else 0
            loss_inc = 1 if status == "lost" else 0
            conn.execute("""
                UPDATE poly_yield_stats
                SET total_pnl = total_pnl + ?,
                    total_returned = total_returned + ?,
                    win_count = win_count + ?,
                    loss_count = loss_count + ?,
                    open_positions = MAX(0, open_positions - 1),
                    updated_at = datetime('now')
                WHERE strategy = ? AND mode = ?
            """, [realized_pnl, return_amount, win_inc, loss_inc, pos["strategy"], mode])
            conn.commit()

        await alert.send(f"Position Exited ({reason}): {pos['market_title']} for realized PnL of ${realized_pnl:.2f} USDC", level="warning" if status == "lost" else "success")
        await self._broadcast({"type": "position_settled", "pos_id": pos_id, "status": status, "pnl": realized_pnl})
        
        return {"success": True, "realized_pnl": realized_pnl, "exit_price": exit_price}

    async def _broadcast(self, msg: dict):
        """Broadcast helper to push events to active dashboard WS connections."""
        dead = []
        for ws in active_websockets:
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in active_websockets:
                active_websockets.remove(ws)

# Global singleton
poly_yield_engine = PolyYieldEngine()
