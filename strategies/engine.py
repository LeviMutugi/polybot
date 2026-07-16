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
        scanned_ids: dict = {}

        for strat in strategies:
            # Check if strategy is enabled in DB
            strat_enabled = await cfg.get_typed(f"{strat.key}.enabled", bool, True)
            if not strat_enabled:
                # Strategy disabled: anything it previously found is no longer maintained
                scanned_ids[strat.key] = set()
                continue

            try:
                opps = await strat.scan(markets, balance, self._http)
                found_ids = set()
                for opp in opps:
                    opp.setdefault("risk_level", strat.risk_level)
                    all_opps.append(opp)
                    found_ids.add(opp.get("id"))
                    # Upsert opportunity to DB (for UI display)
                    await self._upsert_opportunity(opp)
                scanned_ids[strat.key] = found_ids
            except Exception as ex:
                _log.error("Strategy %s scan failed: %s", strat.key, ex)

        # Opportunities not re-confirmed by this scan are stale — never show or execute them
        try:
            await asyncio.to_thread(self._mark_missing_stale, scanned_ids)
        except Exception as ex:
            _log.error("Failed to mark stale opportunities: %s", ex)

        # Filter for auto-execution and rank by priority metric
        # EV = (Return / Risk) * Prob of Success. We use expected_value_usdc or annualized_apy as proxy
        auto_opps = [opp for opp in all_opps if opp.get("exec_mode") == "auto" and not self._killswitch]

        # Sort opportunities (descending) prioritizing expected value or APY if EV not available
        auto_opps.sort(key=lambda x: (
            (x.get("expected_value_usdc") or 0) > 0, # Has positive EV calculation
            (x.get("expected_value_usdc") or 0),     # The EV amount itself
            (x.get("annualized_apy") or 0)           # Fallback to APY
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

    def _mark_missing_stale(self, scanned_ids: dict):
        """Mark previously-open opportunities that were NOT found in this scan as stale,
        and purge old non-executed rows so the UI never shows dead entries."""
        conn = get_sqlite()
        with _sqlite_lock:
            for strat_key, ids in scanned_ids.items():
                ids = {i for i in ids if i}
                if ids:
                    placeholders = ",".join("?" * len(ids))
                    conn.execute(
                        f"UPDATE poly_yield_opportunities SET status = 'stale' "
                        f"WHERE strategy = ? AND status = 'open' AND id NOT IN ({placeholders})",
                        [strat_key, *ids]
                    )
                else:
                    conn.execute(
                        "UPDATE poly_yield_opportunities SET status = 'stale' "
                        "WHERE strategy = ? AND status = 'open'",
                        [strat_key]
                    )
            # Purge dead rows after a day; keep 'executed' rows for position audit trail
            conn.execute(
                "DELETE FROM poly_yield_opportunities "
                "WHERE status IN ('open', 'stale') AND updated_at < datetime('now', '-1 day')"
            )
            conn.commit()

    async def _upsert_opportunity(self, opp: dict):
        try:
            conn = get_sqlite()
            with _sqlite_lock:
                conn.execute("""
                    INSERT OR REPLACE INTO poly_yield_opportunities
                    (id, strategy, risk_level, execution_type, market_type, reward_score,
                     slippage_bps, market_id, market_title, market_url, token_id, outcome, entry_price,
                     implied_prob, yes_price, no_price, annualized_apy, profit_pct, days_to_expiry,
                     action, exec_mode, suggested_usdc, status, notes, instructions, legs, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
                """, [
                    opp.get("id"), opp.get("strategy"), opp.get("risk_level"), opp.get("exec_mode"),
                    opp.get("market_type", "Binary"), opp.get("reward_score", 0.0), opp.get("slippage_bps", 0.0),
                    opp.get("market_id"), opp.get("market_title"), opp.get("market_url"), opp.get("token_id"),
                    opp.get("outcome"),
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

    async def _opportunity_freshness_error(self, opp: dict) -> str:
        """Return an error string if the stored opportunity is stale or already consumed, else None.
        Prevents executing against prices from an old scan."""
        opp_id = opp.get("id")
        if not opp_id:
            return "Opportunity has no id"
        conn = get_sqlite()
        with _sqlite_lock:
            row = conn.execute(
                "SELECT status, updated_at FROM poly_yield_opportunities WHERE id = ?", [opp_id]
            ).fetchone()
        if not row:
            return None  # not persisted yet (came straight from the scanner) — inherently fresh
        if row["status"] != "open":
            return f"Opportunity is no longer available (status: {row['status']}). Wait for the next scan."
        interval = await cfg.get_typed("poly_yield.scan_interval_s", int, 120)
        max_age_s = max(300, interval * 3)
        try:
            updated = datetime.strptime(str(row["updated_at"]), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            age_s = (datetime.now(timezone.utc) - updated).total_seconds()
            if age_s > max_age_s:
                with _sqlite_lock:
                    conn.execute("UPDATE poly_yield_opportunities SET status = 'stale' WHERE id = ?", [opp_id])
                    conn.commit()
                return f"Opportunity data is stale ({int(age_s)}s old, max {max_age_s}s). Wait for the next scan."
        except (ValueError, TypeError):
            pass
        return None

    async def execute_opportunity(self, opp: dict) -> dict:
        """Centralized execution entry point with pre-flight, parallel dispatch, and rollback guards."""
        opp_id = opp.get("id")
        strat_key = opp.get("strategy")
        market_id = opp.get("market_id")
        is_manual_trade = strat_key in ("manual", "manual_trade")
        try:
            suggested_usdc = float(opp.get("suggested_usdc") or 0.0)
        except (TypeError, ValueError):
            return {"success": False, "error": "Invalid suggested_usdc"}

        if self._killswitch:
            return {"success": False, "error": "Killswitch is active — trading is frozen pending manual review"}

        # Strategy-level 'manual' exec mode = instructions only, never executable through the engine.
        # (The Manual Trade panel uses the dedicated 'manual' strategy and is exempt.)
        if not is_manual_trade:
            strat_exec_mode = await cfg.get_typed(f"{strat_key}.exec_mode", str, "manual")
            if strat_exec_mode == "manual":
                return {"success": False, "error": f"Strategy {strat_key} is in MANUAL mode (instructions only). Switch it to semi or auto to allow execution."}

        # Freshness guard — never execute against data from an old scan
        freshness_error = await self._opportunity_freshness_error(opp)
        if freshness_error:
            return {"success": False, "error": freshness_error}

        # 1. Obtain market lock to prevent competing orders on the same market
        lock = await portfolio_allocator.get_market_lock(market_id)

        async with lock:
            # Idempotency guard (inside the lock so concurrent executes serialize correctly)
            conn = get_sqlite()
            with _sqlite_lock:
                existing = conn.execute(
                    "SELECT id FROM poly_yield_positions WHERE opportunity_id = ? AND status = 'open'",
                    [opp_id]
                ).fetchone()
            if existing:
                return {"success": False, "error": "Opportunity already has an open position (duplicate)"}

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

            mode = await cfg.get_typed("poly_yield.active_mode", str, "paper")
            max_slippage = await cfg.get_typed("poly_yield.max_slippage_pct", float, 1.5)

            legs = opp.get("legs") or []
            if legs and isinstance(legs, list):
                return await self._execute_multi_leg(opp, legs, allocated_usdc, mode, max_slippage)
            return await self._execute_single_leg(opp, allocated_usdc, mode, max_slippage, is_manual_trade)

    def _paper_open(self, allocated_usdc: float, description: str):
        """Debit the paper wallet for a new position. Returns (pos_id, error)."""
        from services.wallet import wallet_service, InsufficientFundsError
        pos_id = f"pos_{uuid.uuid4().hex[:8]}"
        try:
            wallet_service.debit("paper", allocated_usdc, "trade_open",
                                 position_id=pos_id,
                                 description=description,
                                 idempotency_key=f"open_{pos_id}")
        except InsufficientFundsError as e:
            return None, str(e)
        except Exception as e:
            return None, f"Paper wallet debit failed: {e}"
        return pos_id, None

    def _paper_refund(self, pos_id: str, amount: float, reason: str):
        """Refund a paper debit when position recording fails (money conservation)."""
        try:
            from services.wallet import wallet_service
            wallet_service.credit("paper", amount, "trade_refund",
                                  position_id=pos_id,
                                  description=f"Refund: {reason}"[:120],
                                  idempotency_key=f"refund_{pos_id}")
        except Exception as e:
            _log.error("CRITICAL: Paper refund failed for %s: %s", pos_id, e)

    async def _execute_multi_leg(self, opp: dict, legs: list, allocated_usdc: float, mode: str, max_slippage: float) -> dict:
        """Multi-leg basket execution (S3 buy-all, S5 sub-event basket) with per-leg
        live-book verification, price-drift guard, and partial-fill rollback."""
        from strategies.base import calculate_execution_price, is_fillable

        try:
            denom = sum(float(l.get("price") or 0) for l in legs)
        except (TypeError, ValueError):
            denom = 0.0
        if denom <= 0:
            return {"success": False, "error": "Invalid multi-leg prices"}

        # Pre-flight: walk EVERY leg's live book, verify liquidity, slippage, and drift vs scan price
        leg_fills = []
        for leg in legs:
            token_id = leg.get("token_id")
            try:
                leg_price = float(leg.get("price") or 0)
            except (TypeError, ValueError):
                leg_price = 0.0
            if not token_id or leg_price <= 0:
                return {"success": False, "error": f"Leg '{leg.get('outcome')}' missing token_id or price"}

            leg_stake = (leg_price / denom) * allocated_usdc
            leg["stake_usdc"] = round(leg_stake, 2)

            walk = await calculate_execution_price(token_id, leg_stake, side="buy", http_client=self._http)
            if not is_fillable(walk, max_slippage):
                reason = walk.get("error") or walk.get("warning") or f"slippage {walk.get('slippage')}% > {max_slippage}%"
                _log.warning("[Engine] Pre-flight aborted. Leg %s failed: %s", leg.get("outcome"), reason)
                return {"success": False, "error": f"Pre-flight failed on leg '{leg.get('outcome')}': {reason}"}

            drift_pct = abs(walk["price"] - leg_price) / leg_price * 100.0
            if drift_pct > max_slippage:
                return {"success": False,
                        "error": f"Price moved on leg '{leg.get('outcome')}': ${leg_price:.4f} at scan vs ${walk['price']:.4f} now "
                                 f"({drift_pct:.2f}% drift). Wait for the next scan."}
            leg_fills.append({"leg": leg, "fill_price": walk["price"]})

        if mode == "paper":
            pos_id, err = self._paper_open(allocated_usdc, f"Buy basket: {opp.get('market_title', '')[:80]}")
            if err:
                return {"success": False, "error": err}
            total_cost = 0.0
            min_shares = None
            for lf in leg_fills:
                shares = lf["leg"]["stake_usdc"] / lf["fill_price"] if lf["fill_price"] > 0 else 0.0
                total_cost += lf["leg"]["stake_usdc"]
                min_shares = shares if min_shares is None else min(min_shares, shares)
            try:
                await self._record_position(opp, min_shares or 0.0, allocated_usdc, "paper_basket_order", fill_price=opp.get("entry_price"), pos_id=pos_id)
            except Exception as e:
                self._paper_refund(pos_id, allocated_usdc, "basket position record failed")
                return {"success": False, "error": f"Failed to record position (funds refunded): {e}"}
            await alert.send(f"Paper basket trade executed: {opp['market_title']} for ${allocated_usdc:.2f} USDC", level="success")
            return {"success": True, "mode": "paper", "position_id": pos_id}

        if not self._clob_client:
            return {"success": False, "error": "CLOB client not configured for live trading"}

        # Parallel dispatch at the verified current prices
        tasks = [self._place_live_order(lf["leg"]["token_id"], lf["leg"]["stake_usdc"], lf["fill_price"]) for lf in leg_fills]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        placed = [r for r in results if isinstance(r, dict) and r.get("success")]
        failed = [r for r in results if isinstance(r, Exception) or (isinstance(r, dict) and not r.get("success"))]

        if failed:
            # Trigger rollback cancellation loop & Killswitch
            _log.critical("[Engine] Multi-leg partial fill detected! Rolling back placed legs.")
            self._killswitch = True
            await cfg.set_async("poly_yield.enabled", "false")

            for p in placed:
                order_id = p.get("order_id")
                if not order_id:
                    continue
                try:
                    await asyncio.to_thread(self._clob_client.cancel, order_id)
                except Exception as ce:
                    _log.error("Failed to cancel leg order %s: %s", order_id, ce)

            await alert.send("CRITICAL: Multi-leg partial fill failed! Killswitch activated. Manual intervention required.", level="critical")
            return {"success": False, "error": "Partial fill occurred. Rolled back and activated system killswitch."}

        # Success - record total position
        total_cost = sum(p.get("fill_cost", 0) for p in placed)
        min_shares = min(p.get("fill_shares", 0) for p in placed) if placed else 0
        order_ids_str = ",".join(str(p.get("order_id", "")) for p in placed)
        await self._record_position(opp, min_shares, total_cost or allocated_usdc, order_ids_str, fill_price=opp.get("entry_price"))
        await alert.send(f"Live Basket trade success: {opp['market_title']} for ${total_cost or allocated_usdc:.2f} USDC", level="success")
        return {"success": True, "placed": len(placed)}

    async def _execute_single_leg(self, opp: dict, allocated_usdc: float, mode: str, max_slippage: float, is_manual_trade: bool) -> dict:
        """Single-leg execution with live price re-verification against the order book."""
        strat_key = opp.get("strategy")
        token_id = opp.get("token_id")
        try:
            entry_price = float(opp.get("entry_price") or 0)
        except (TypeError, ValueError):
            entry_price = 0.0
        if not (0 < entry_price < 1):
            return {"success": False, "error": f"Invalid entry price {entry_price} (must be between 0 and 1)"}

        exec_price = entry_price
        # Re-verify against the live book (skipped for manual trades: the user sets an explicit limit price)
        if not is_manual_trade:
            if not token_id:
                return {"success": False, "error": "Opportunity is missing token_id — cannot verify live price before execution"}
            from strategies.base import calculate_execution_price, is_fillable
            walk = await calculate_execution_price(token_id, allocated_usdc, side="buy", http_client=self._http)
            if not is_fillable(walk, max_slippage):
                reason = walk.get("error") or walk.get("warning") or f"slippage {walk.get('slippage')}% > {max_slippage}%"
                return {"success": False, "error": f"Live book check failed: {reason}"}
            drift_pct = abs(walk["price"] - entry_price) / entry_price * 100.0
            if drift_pct > max_slippage:
                return {"success": False,
                        "error": f"Price moved since scan: ${entry_price:.4f} → ${walk['price']:.4f} "
                                 f"({drift_pct:.2f}% drift). Wait for the next scan."}
            exec_price = walk["price"]
            opp["entry_price"] = exec_price  # execute at the verified current price

        if mode == "paper":
            pos_id, err = self._paper_open(allocated_usdc, f"Buy: {opp.get('market_title', '')[:80]}")
            if err:
                return {"success": False, "error": err}
            shares = allocated_usdc / exec_price
            try:
                await self._record_position(opp, shares, allocated_usdc, "paper_order_id", fill_price=exec_price, pos_id=pos_id)
            except Exception as e:
                self._paper_refund(pos_id, allocated_usdc, "position record failed")
                return {"success": False, "error": f"Failed to record position (funds refunded): {e}"}
            await alert.send(f"Paper trade executed: {opp['market_title']} -> {opp['outcome']} for ${allocated_usdc:.2f} USDC at ${exec_price:.4f}", level="success")
            return {"success": True, "mode": "paper", "position_id": pos_id}

        if not self._clob_client:
            return {"success": False, "error": "CLOB client not configured for live trading"}

        if is_manual_trade:
            # Execute manual order directly
            from py_clob_client.clob_types import OrderArgs
            price = exec_price
            shares = round(allocated_usdc / price, 2)
            try:
                if not token_id:
                    return {"success": False, "error": "Manual live trade requires a CLOB token_id"}
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
            # Verify fill status; always carry the order_id so rollback/recording can reference it
            fill_res = await self._verify_order_fill(order_id)
            fill_res["order_id"] = order_id
            return fill_res
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _record_position(self, opp: dict, shares: float, cost_usdc: float, order_id: str, fill_price: float = None, pos_id: str = None):
        from strategies.base import days_to_expiry
        pos_id = pos_id or f"pos_{uuid.uuid4().hex[:8]}"
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
                    (id, opportunity_id, strategy, market_id, market_title, token_id, outcome,
                     shares, entry_price, cost_usdc, order_id, status, entry_at,
                     predicted_apy, predicted_profit_pct, predicted_days_to_expiry,
                     actual_fill_price, actual_gas_usdc, risk_level, fill_slippage_bps,
                     quality_at_entry, predicted_pnl_usdc, mode,
                     stop_loss_price, take_profit_price, trailing_stop_pct, highest_price)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, [
                    pos_id, opp.get("id"), opp.get("strategy"), opp.get("market_id"),
                    opp.get("market_title"), opp.get("token_id"), opp.get("outcome"),
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
        if not (0 < current_price <= 1.0):
            return {"success": False, "error": f"Invalid exit price {current_price} (must be between 0 and 1)"}

        token_id = pos.get("token_id")  # persisted at entry time
        mode = pos["mode"]

        # Fallback: source CLOB token ID from Gamma API if we are in live mode without a stored one
        if mode == "live" and not token_id:
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

        if mode == "live" and not token_id:
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
