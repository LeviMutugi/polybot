"""
PolyYield Settlement Worker
Periodically polls Gamma API for resolved markets, resolves open positions,
calculates realized PnL, and updates cumulative strategy performance statistics.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, Tuple
import httpx
from config import settings
from db.database import get_sqlite, _sqlite_lock
from strategies.base import parse_list
import json

_log = logging.getLogger(__name__)

GAMMA_URL = settings.polymarket_gamma_url.rstrip("/")

class PolyYieldSettlement:
    def __init__(self, poll_interval_s: int = 300):
        self._poll_interval_s = poll_interval_s
        self._running = False
        self._task = None
        self._http = None

    def start(self):
        """Start the background settlement checker loop."""
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="poly_yield_settlement")
        print(f"[PolyYieldSettlement] Worker started — polling every {self._poll_interval_s}s")

    async def stop(self):
        """Stop the settlement checker worker."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._http:
            await self._http.aclose()
        print("[PolyYieldSettlement] Worker stopped")

    async def _loop(self):
        self._http = httpx.AsyncClient(timeout=20.0)
        await asyncio.sleep(5.0)  # Brief delay on startup
        
        tick = 0
        while self._running:
            # 1. Run stop-loss and limit evaluations every 30 seconds
            try:
                await self._check_stops_and_limits()
            except Exception as e:
                _log.error("Error in stop checks: %s", e)

            # 2. Run resolved market settlement checks every 10 ticks (~5 minutes)
            if tick % 10 == 0:
                try:
                    settled = await self._settle_open_positions()
                    if settled > 0:
                        print(f"[PolyYieldSettlement] Successfully resolved {settled} positions.")
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    _log.error("Settlement error: %s", e)

            tick += 1
            await asyncio.sleep(30.0)

    async def _check_stops_and_limits(self):
        """Evaluate open positions for Stop Loss, Take Profit, and Trailing Stop triggers."""
        conn = get_sqlite()
        with _sqlite_lock:
            rows = conn.execute("SELECT * FROM poly_yield_positions WHERE status = 'open'").fetchall()
        
        for row in rows:
            pos = dict(row)
            pos_id = pos["id"]
            
            # Skip if no stops/limits configured
            if not pos.get("stop_loss_price") and not pos.get("take_profit_price") and not pos.get("trailing_stop_pct"):
                continue

            # Sourcing token ID for the outcome
            token_id = pos.get("token_id")
            
            if not token_id:
                try:
                    r = await self._http.get(f"{GAMMA_URL}/markets/{pos['market_id']}")
                    if r.status_code == 200:
                        market = r.json()
                        token_ids = parse_list(market.get("clobTokenIds"))
                        outcomes = parse_list(market.get("outcomes"))
                        for t_id, out in zip(token_ids, outcomes):
                            if out.strip().lower() == pos["outcome"].strip().lower():
                                token_id = t_id
                                break
                except Exception:
                    pass

            if not token_id:
                continue

            # Fetch current BEST bid price from order book (levels must be sorted —
            # the raw API returns bids ascending, so bids[0] would be the WORST bid)
            clob_url = settings.polymarket_clob_url.rstrip("/")
            try:
                r = await self._http.get(f"{clob_url}/book", params={"token_id": token_id})
                if r.status_code != 200:
                    continue
                book = r.json()
                from strategies.base import sort_book_levels
                bids = sort_book_levels(book.get("bids") or [], "bids")
                if not bids:
                    continue
                current_price = float(bids[0]["price"])
                if not (0 < current_price <= 1.0):
                    continue
            except Exception as e:
                _log.debug("Failed to fetch price for stop check on %s: %s", pos_id, e)
                continue

            # Evaluate trailing stop: update highest price if appropriate
            highest_price = pos.get("highest_price") or pos.get("entry_price") or current_price
            if current_price > highest_price:
                highest_price = current_price
                with _sqlite_lock:
                    conn.execute("UPDATE poly_yield_positions SET highest_price = ? WHERE id = ?", [highest_price, pos_id])
                    conn.commit()

            sl_price = pos.get("stop_loss_price")
            tp_price = pos.get("take_profit_price")
            ts_pct = pos.get("trailing_stop_pct")

            triggered = False
            reason = ""
            ts_threshold = None

            if sl_price and current_price <= float(sl_price):
                triggered = True
                reason = "Stop Loss"
            elif tp_price and current_price >= float(tp_price):
                triggered = True
                reason = "Take Profit"
            elif ts_pct:
                ts_threshold = highest_price * (1.0 - float(ts_pct) / 100.0)
                if current_price <= ts_threshold:
                    triggered = True
                    reason = f"Trailing Stop ({ts_pct}%)"

            if triggered:
                _log.warning("[Settlement] Position %s triggered %s (current: $%s, threshold: $%s)", 
                             pos_id, reason, current_price, sl_price or tp_price or ts_threshold)
                from strategies.engine import poly_yield_engine
                await poly_yield_engine.exit_position(pos_id, current_price, reason=reason)

    async def _fetch_market(self, market_id: str) -> Optional[dict]:
        try:
            r = await self._http.get(f"{GAMMA_URL}/markets/{market_id}")
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        return None

    async def _settle_open_positions(self) -> int:
        conn = get_sqlite()
        with _sqlite_lock:
            rows = conn.execute("SELECT * FROM poly_yield_positions WHERE status = 'open'").fetchall()
        
        settled_count = 0
        for row in rows:
            pos = dict(row)
            market_id = pos.get("market_id")
            if not market_id:
                continue

            market = await self._fetch_market(str(market_id))
            if not market or not market.get("closed"):
                continue

            realized_pnl, settlement_outcome, status = self._compute_pnl(pos, market)
            if status == "open":
                continue  # Resolution is not finalized on Gamma yet

            apy_delta = self._compute_apy_delta(pos, realized_pnl)

            # Attempt live redemption if the position was a winner
            if status == "won" and pos.get("mode") == "live":
                try:
                    await self._redeem_live_position(pos, market)
                except Exception as e:
                    _log.error("Failed to redeem live position %s: %s", pos["id"], e)

            # Credit paper wallet on settlement
            if pos.get("mode") == "paper":
                from services.wallet import wallet_service
                cost = float(pos.get("cost_usdc") or 0)
                if status == "won":
                    credit_amount = cost + realized_pnl
                    wallet_service.credit("paper", credit_amount, "settlement_win",
                                          position_id=pos["id"],
                                          description=f"Won: {pos.get('market_title', '')[:80]}",
                                          idempotency_key=f"settle_{pos['id']}")
                else:
                    # Lost — money was already deducted on entry. Record $0 credit for audit trail.
                    wallet_service.credit("paper", 0.0, "settlement_loss",
                                          position_id=pos["id"],
                                          description=f"Lost: {pos.get('market_title', '')[:80]}",
                                          idempotency_key=f"settle_{pos['id']}")

            # Update DB entry
            with _sqlite_lock:
                conn.execute("""
                    UPDATE poly_yield_positions
                    SET status = ?, settled_at = datetime('now'), realized_pnl = ?,
                        settlement_outcome = ?, apy_delta = ?, exit_price = ?
                    WHERE id = ?
                """, [status, realized_pnl, settlement_outcome, apy_delta,
                      1.0 if status == "won" else 0.0, pos["id"]])

                # Update cumulative statistics
                self._update_stats(conn, pos.get("strategy"), realized_pnl, status, pos.get("mode", "paper"), float(pos.get("cost_usdc") or 0))
                conn.commit()

            settled_count += 1

            # Trigger broadcast update through the engine websocket
            try:
                from strategies.engine import poly_yield_engine
                await poly_yield_engine._broadcast({
                    "type": "position_settled", 
                    "pos_id": pos["id"], 
                    "status": status, 
                    "pnl": realized_pnl
                })
            except Exception:
                pass

        return settled_count

    def _winning_outcome(self, market: dict) -> Optional[str]:
        """Determine the resolved winner. Requires a definitive (>= 0.99) settlement price —
        a mere majority price on a closed-but-unresolved market must NEVER settle a position,
        otherwise PnL is realized on a guess."""
        outcomes = parse_list(market.get("outcomes"))
        prices = parse_list(market.get("outcomePrices"))
        if not outcomes or len(prices) != len(outcomes):
            return None

        for outcome, price in zip(outcomes, prices):
            try:
                if float(price) >= 0.99:
                    return str(outcome)
            except (TypeError, ValueError):
                continue
        # Not definitively resolved yet — keep the position open and re-check next poll
        return None

    def _position_won(self, pos: dict, winning_outcome: str) -> bool:
        """Determine if a position is on the winning side."""
        outcome = (pos.get("outcome") or "").strip().lower()
        winner = winning_outcome.strip().lower()
        strategy = pos.get("strategy") or ""

        # S6 Longshot MM sells YES (buys NO). Wins when YES does NOT win.
        if strategy == "s6_longshot" or "sell yes" in outcome:
            return winner != "yes"

        # Explicit YES/NO matching for binary markets
        if outcome in ("yes", "buy yes", "buy_yes"):
            return winner == "yes"
        if outcome in ("no", "buy no", "buy_no"):
            return winner == "no"

        # Multi-outcome: exact match only (no substring to prevent false positives)
        return winner == outcome

    def _compute_pnl(self, pos: dict, market: dict) -> Tuple[float, str, str]:
        winning = self._winning_outcome(market)
        if not winning:
            return 0.0, "unknown", "open"

        shares = float(pos.get("shares") or 0)
        cost = float(pos.get("cost_usdc") or 0)
        entry = float(pos.get("entry_price") or 0)
        gas = float(pos.get("actual_gas_usdc") or 0)
        strategy = pos.get("strategy") or ""

        won = self._position_won(pos, winning)

        # All strategies including s6_longshot buy a token (S6 buys NO)
        # So they all use traditional buy leg PnL calculation
        if won:
            payout = shares * 1.0
            realized = payout - cost - gas
        else:
            realized = -cost - gas

        status = "won" if won else "lost"
        settlement_outcome = f"resolved_{winning.lower().replace(' ', '_')}"
        return round(realized, 4), settlement_outcome, status

    def _compute_apy_delta(self, pos: dict, realized_pnl: float) -> Optional[float]:
        predicted_apy = pos.get("predicted_apy")
        cost = float(pos.get("cost_usdc") or 0)
        days = pos.get("predicted_days_to_expiry")
        if predicted_apy is None or cost <= 0 or not days or float(days) <= 0:
            return None
        
        # Use the same compounding formula as predicted APY so the delta compares like with like
        from strategies.base import calculate_compounding_apy
        actual_apy = calculate_compounding_apy(realized_pnl / cost, float(days))
        return round(actual_apy - float(predicted_apy), 4)

    def _update_stats(self, conn, strategy: str, realized_pnl: float, status: str, mode: str, cost_usdc: float = 0.0):
        if not strategy:
            return
        
        conn.execute("INSERT OR IGNORE INTO poly_yield_stats (strategy, mode) VALUES (?, ?)", [strategy, mode])
        
        win_inc = 1 if status == "won" else 0
        loss_inc = 1 if status == "lost" else 0
        # total_returned = capital returned to wallet (cost + pnl, floored at 0)
        return_amount = max(0.0, cost_usdc + realized_pnl)
        
        conn.execute("""
            UPDATE poly_yield_stats
            SET total_pnl = total_pnl + ?,
                total_returned = total_returned + ?,
                win_count = win_count + ?,
                loss_count = loss_count + ?,
                open_positions = MAX(0, open_positions - 1),
                updated_at = datetime('now')
            WHERE strategy = ? AND mode = ?
        """, [realized_pnl, return_amount, win_inc, loss_inc, strategy, mode])

    async def _redeem_live_position(self, pos: dict, market: dict):
        """Redeem won conditional tokens via Polymarket CTF."""
        from services.keystore import keystore
        pk = keystore.get_decrypted("polymarket_wallet")
        if not pk:
            _log.warning("[Settlement] No private key for redemption of %s", pos["id"])
            return

        condition_id = market.get("conditionId")
        if not condition_id:
            _log.error("[Settlement] No conditionId found for market %s", market.get("id"))
            return

        import web3
        from eth_account import Account
        from strategies.engine import poly_yield_engine

        acct = Account.from_key(pk)
        # AsyncWeb3 is required with AsyncHTTPProvider — mixing sync Web3 with an async
        # provider makes every awaited eth call fail at runtime
        w3 = web3.AsyncWeb3(web3.AsyncHTTPProvider(settings.polygon_rpc_url))
        
        # Get CTF Address from ClobClient if available
        ctf_address = "0x4D97DCd97eC945f40CF65F87097CAe16E4bb2830" # Polygon CTF Address
        if poly_yield_engine._clob_client:
            try:
                ctf_address = poly_yield_engine._clob_client.get_conditional_address()
            except:
                pass

        collateral = settings.polygon_usdc_contract
        parent_collection_id = "0x" + "0" * 64
        
        # Determine index sets. Usually [1, 2] for YES/NO
        outcomes = parse_list(market.get("outcomes"))
        index_sets = []
        for i in range(len(outcomes)):
            index_sets.append(1 << i)

        # Simple ABI for redeemPositions
        abi = [{
            "type": "function",
            "name": "redeemPositions",
            "inputs": [
                {"name": "collateralToken", "type": "address"},
                {"name": "parentCollectionId", "type": "bytes32"},
                {"name": "conditionId", "type": "bytes32"},
                {"name": "indexSets", "type": "uint256[]"}
            ],
            "outputs": []
        }]
        
        contract = w3.eth.contract(address=w3.to_checksum_address(ctf_address), abi=abi)
        
        # We wrap in try block in case of RPC error or insufficient gas
        _log.info("[Settlement] Redeeming position %s via CTF contract", pos["id"])
        
        try:
            nonce = await w3.eth.get_transaction_count(acct.address)
            tx = await contract.functions.redeemPositions(
                w3.to_checksum_address(collateral),
                parent_collection_id,
                condition_id,
                index_sets
            ).build_transaction({
                'from': acct.address,
                'nonce': nonce,
                'gasPrice': await w3.eth.gas_price
            })
            
            signed_tx = w3.eth.account.sign_transaction(tx, private_key=pk)
            tx_hash = await w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            _log.info("[Settlement] Redeemed position %s, Tx Hash: %s", pos["id"], tx_hash.hex())
        except Exception as e:
            _log.error("[Settlement] Smart contract redemption skipped or failed: %s", e)

# Global singleton
poly_yield_settlement = PolyYieldSettlement()
