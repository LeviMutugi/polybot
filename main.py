"""
PolyYield Main Entrypoint
FastAPI web server serving REST endpoints, WebSocket telemetry, and the Glassmorphic HTML5 UI dashboard.
"""
import asyncio
import json
import logging
import uuid
from typing import Dict
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
import httpx

from config import settings
from db.database import init_db, close_db, get_sqlite, _sqlite_lock
from db.config import cfg
from strategies.engine import poly_yield_engine, active_websockets
from strategies.settlement import poly_yield_settlement
from services.keystore import keystore
from services.portfolio_allocator import portfolio_allocator

# Logging Configuration
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
_log = logging.getLogger(__name__)

app = FastAPI(title="PolyYield Prediction Market Engine", version="1.0.0")

# --- API Authentication ---
_bearer = HTTPBearer(auto_error=False)

async def verify_token(creds: HTTPAuthorizationCredentials = Depends(_bearer)):
    """Validate Bearer token against the configured api_secret."""
    if not settings.api_secret or settings.api_secret == "poly_yield_default_secret_key_change_me":
        return  # Skip auth if default/unset (development mode)
    if not creds or creds.credentials != settings.api_secret:
        raise HTTPException(status_code=401, detail="Unauthorized")

class ConfigUpdate(BaseModel):
    key: str
    value: str

class KeySubmit(BaseModel):
    service: str
    value: str
    label: str = ""

class ManualTradeArgs(BaseModel):
    market_id: str
    outcome: str
    stake_usdc: float
    price: float
    stop_loss_price: float = None
    take_profit_price: float = None
    trailing_stop_pct: float = None
    token_id: str = None
    market_title: str = "Manual Position"

class ExitArgs(BaseModel):
    current_price: float

class PaperFundArgs(BaseModel):
    amount: float

@app.on_event("startup")
async def startup_event():
    # Scaffold Database
    init_db()
    # Start engine and settlement loops
    await poly_yield_engine.start()
    poly_yield_settlement.start()

@app.on_event("shutdown")
async def shutdown_event():
    await poly_yield_engine.stop()
    await poly_yield_settlement.stop()
    close_db()

# --- REST API Endpoints ---

@app.get("/api/poly-yield/opportunities")
async def get_opportunities():
    """Retrieve FRESH, still-open opportunities. Stale or already-executed rows are
    never returned — displaying them invites executing against dead prices."""
    interval = await cfg.get_typed("poly_yield.scan_interval_s", int, 120)
    max_age_s = max(300, interval * 3)

    conn = get_sqlite()
    with _sqlite_lock:
        rows = conn.execute(
            "SELECT * FROM poly_yield_opportunities "
            "WHERE status = 'open' AND updated_at >= datetime('now', ?) "
            "ORDER BY updated_at DESC",
            [f"-{max_age_s} seconds"]
        ).fetchall()

    # Compute sizing context ONCE (wallet balance can involve an RPC call in live mode —
    # never do that per-row)
    mode = await cfg.get_typed("poly_yield.active_mode", str, "paper")
    balance = await portfolio_allocator._get_wallet_balance()
    drawdown_limit_pct = await cfg.get_typed("poly_yield.auto_exec_drawdown_limit", float, 50.0)
    with _sqlite_lock:
        exp_row = conn.execute(
            "SELECT SUM(cost_usdc) as exposure FROM poly_yield_positions WHERE status = 'open' AND mode = ?", [mode]
        ).fetchone()
    current_exposure = exp_row["exposure"] if exp_row and exp_row["exposure"] is not None else 0.0
    remaining_room = max(0.0, balance * (drawdown_limit_pct / 100.0) - current_exposure)

    opportunities = []
    for row in rows:
        d = dict(row)
        # Parse legs and instructions if present
        if d.get("legs"):
            d["legs"] = json.loads(d["legs"])
        if d.get("instructions"):
            d["instructions"] = json.loads(d["instructions"])

        # Display sizing: clip the strategy's suggested size to remaining drawdown room.
        # (The authoritative sizing check re-runs at execution time.)
        suggested = float(d.get("suggested_usdc") or 0.0)
        clipped = round(min(suggested, remaining_room), 2)
        d["suggested_usdc"] = clipped
        # Max profit/loss scale linearly with capital deployed — rescale so displayed
        # payoff always matches the displayed (possibly clipped) size, never the
        # pre-clip scan-time size.
        if suggested > 0 and clipped != suggested:
            ratio = clipped / suggested
            if d.get("max_profit_usdc") is not None:
                d["max_profit_usdc"] = round(d["max_profit_usdc"] * ratio, 4)
            if d.get("max_loss_usdc") is not None:
                d["max_loss_usdc"] = round(d["max_loss_usdc"] * ratio, 4)
        opportunities.append(d)

    return opportunities

@app.get("/api/poly-yield/positions")
async def get_positions():
    """Retrieve open and settled positions for the currently active mode."""
    active_mode = await cfg.get_typed("poly_yield.active_mode", str, "paper")
    conn = get_sqlite()
    with _sqlite_lock:
        rows = conn.execute(
            "SELECT * FROM poly_yield_positions WHERE mode = ? ORDER BY entry_at DESC", [active_mode]
        ).fetchall()
    return [dict(r) for r in rows]

@app.get("/api/poly-yield/stats")
async def get_stats():
    """Retrieve per-strategy statistics for both modes."""
    conn = get_sqlite()
    with _sqlite_lock:
        rows = conn.execute("SELECT * FROM poly_yield_stats").fetchall()
    return [dict(r) for r in rows]

@app.get("/api/poly-yield/wallet-balance")
async def get_wallet_balance():
    """Retrieve current wallet balance for the active mode."""
    balance = await portfolio_allocator._get_wallet_balance()
    return {"balance": balance}

@app.post("/api/poly-yield/stats/reset")
async def reset_stats(_=Depends(verify_token)):
    """Reset aggregate statistics to zero."""
    conn = get_sqlite()
    with _sqlite_lock:
        conn.execute("""
            UPDATE poly_yield_stats
            SET total_pnl = 0, total_returned = 0, win_count = 0, loss_count = 0
        """)
        conn.commit()
    return {"status": "success"}

@app.get("/api/poly-yield/history")
async def get_history(mode: str = None, strategy: str = None, status: str = None,
                       executed_by: str = None, limit: int = 500):
    """Retrieve historical positions (open + settled), optionally filtered, for the
    Trade History & Audit view. Defaults to all modes/strategies/statuses, newest first."""
    limit = max(1, min(limit, 5000))
    clauses = []
    params = []
    if mode:
        clauses.append("mode = ?")
        params.append(mode)
    if strategy:
        clauses.append("strategy = ?")
        params.append(strategy)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if executed_by:
        clauses.append("COALESCE(executed_by, 'bot') = ?")
        params.append(executed_by)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    conn = get_sqlite()
    with _sqlite_lock:
        rows = conn.execute(
            f"SELECT * FROM poly_yield_positions {where} ORDER BY entry_at DESC LIMIT ?",
            [*params, limit]
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/poly-yield/accounting")
async def get_accounting(mode: str = "paper"):
    """
    Full accounting & audit summary for the Trade History tab: totals, per-strategy
    breakdown, manual-vs-bot breakdown, and (for paper) the wallet ledger conservation
    check — every number here is recomputed directly from poly_yield_positions rather
    than the incrementally-maintained poly_yield_stats table, so it can't drift.
    """
    conn = get_sqlite()
    with _sqlite_lock:
        totals_row = conn.execute("""
            SELECT
                COUNT(*) as trade_count,
                SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) as open_count,
                SUM(CASE WHEN status = 'won' THEN 1 ELSE 0 END) as won_count,
                SUM(CASE WHEN status = 'lost' THEN 1 ELSE 0 END) as lost_count,
                SUM(COALESCE(cost_usdc, 0)) as total_volume_usdc,
                SUM(CASE WHEN status IN ('won','lost') THEN COALESCE(realized_pnl, 0) ELSE 0 END) as total_realized_pnl,
                SUM(COALESCE(actual_gas_usdc, 0)) as total_gas_usdc,
                SUM(CASE WHEN status IN ('won','lost') THEN MAX(0, COALESCE(cost_usdc,0) + COALESCE(realized_pnl,0)) ELSE 0 END) as total_returned,
                AVG(CASE WHEN status IN ('won','lost') AND settled_at IS NOT NULL
                         THEN (julianday(settled_at) - julianday(entry_at)) * 24.0 ELSE NULL END) as avg_hold_hours,
                AVG(CASE WHEN status IN ('won','lost') THEN apy_delta ELSE NULL END) as avg_apy_delta
            FROM poly_yield_positions WHERE mode = ?
        """, [mode]).fetchone()

        by_strategy_rows = conn.execute("""
            SELECT strategy,
                COUNT(*) as trade_count,
                SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) as open_count,
                SUM(CASE WHEN status = 'won' THEN 1 ELSE 0 END) as won_count,
                SUM(CASE WHEN status = 'lost' THEN 1 ELSE 0 END) as lost_count,
                SUM(COALESCE(cost_usdc, 0)) as volume_usdc,
                SUM(CASE WHEN status IN ('won','lost') THEN COALESCE(realized_pnl, 0) ELSE 0 END) as realized_pnl
            FROM poly_yield_positions WHERE mode = ?
            GROUP BY strategy ORDER BY volume_usdc DESC
        """, [mode]).fetchall()

        by_source_rows = conn.execute("""
            SELECT COALESCE(executed_by, 'bot') as executed_by,
                COUNT(*) as trade_count,
                SUM(COALESCE(cost_usdc, 0)) as volume_usdc,
                SUM(CASE WHEN status IN ('won','lost') THEN COALESCE(realized_pnl, 0) ELSE 0 END) as realized_pnl
            FROM poly_yield_positions WHERE mode = ?
            GROUP BY COALESCE(executed_by, 'bot')
        """, [mode]).fetchall()

    totals = dict(totals_row) if totals_row else {}
    won = totals.get("won_count") or 0
    lost = totals.get("lost_count") or 0
    totals["win_rate_pct"] = round((won / (won + lost)) * 100.0, 2) if (won + lost) > 0 else None

    by_strategy = []
    for r in by_strategy_rows:
        d = dict(r)
        w, l = d.get("won_count") or 0, d.get("lost_count") or 0
        d["win_rate_pct"] = round((w / (w + l)) * 100.0, 2) if (w + l) > 0 else None
        by_strategy.append(d)

    ledger_health = None
    if mode == "paper":
        from services.wallet import wallet_service
        ledger_health = wallet_service.verify_conservation("paper")

    return {
        "mode": mode,
        "totals": totals,
        "by_strategy": by_strategy,
        "by_executed_by": [dict(r) for r in by_source_rows],
        "ledger_health": ledger_health,
    }

@app.get("/api/poly-yield/config")
async def get_config():
    """Retrieve all configuration parameters."""
    conn = get_sqlite()
    with _sqlite_lock:
        rows = conn.execute("SELECT key, value FROM system_config").fetchall()
    return {r["key"]: r["value"] for r in rows}

# Keys whose value is money-integrity-critical and must only change through the
# audited wallet endpoints (deposit/reset write a ledger entry; a raw config write
# would silently break conservation-of-money checks).
PROTECTED_CONFIG_KEYS = {"portfolio.paper_balance"}

@app.post("/api/poly-yield/config")
async def update_config(update: ConfigUpdate, _=Depends(verify_token)):
    """Update a configuration value dynamically."""
    if not update.key or not update.key.strip():
        raise HTTPException(status_code=400, detail="Config key cannot be empty")
    if update.key in PROTECTED_CONFIG_KEYS:
        raise HTTPException(
            status_code=400,
            detail=f"'{update.key}' cannot be set directly. Use the paper-deposit/paper-reset endpoints so the wallet ledger stays consistent."
        )
    await cfg.set_async(update.key, update.value)
    # Restart loops or propagate configurations if required
    if update.key == "poly_yield.enabled" and update.value.lower() == "true":
        poly_yield_engine._killswitch = False
    if update.key == "polygon_chain_id":
        await poly_yield_engine._init_clob()
        
    # Broadcast updated configuration and stats to all active WebSocket connections
    config_data = await get_config()
    stats_data = await get_stats()
    dead = []
    for ws in active_websockets:
        try:
            await ws.send_json({
                "type": "welcome",
                "config": config_data,
                "stats": stats_data
            })
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in active_websockets:
            active_websockets.remove(ws)
            
    return {"status": "success", "key": update.key, "value": update.value}

@app.post("/api/poly-yield/execute/{opp_id}")
async def trigger_execution(opp_id: str, _=Depends(verify_token)):
    """Manually trigger trade execution for an opportunity."""
    conn = get_sqlite()
    with _sqlite_lock:
        row = conn.execute(
            "SELECT * FROM poly_yield_opportunities WHERE id = ?", [opp_id]
        ).fetchone()
        
    if not row:
        raise HTTPException(status_code=404, detail="Opportunity not found")
        
    opp = dict(row)
    if opp.get("legs"):
        opp["legs"] = json.loads(opp["legs"])
    if opp.get("instructions"):
        opp["instructions"] = json.loads(opp["instructions"])

    result = await poly_yield_engine.execute_opportunity(opp, triggered_by="manual")
    if result.get("success"):
        return result
    else:
        raise HTTPException(status_code=400, detail=result.get("error", "Execution failed"))

@app.get("/api/poly-yield/orderbook/{token_id}")
async def get_orderbook(token_id: str):
    """Fetch live order book depth from Polymarket CLOB."""
    chain_id = await cfg.get_typed("polygon_chain_id", int, 137)
    clob_url = "https://clob-testnet.polymarket.com" if chain_id == 80002 else settings.polymarket_clob_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{clob_url}/book", params={"token_id": token_id})
            if resp.status_code == 200:
                return resp.json()
            else:
                return JSONResponse(status_code=resp.status_code, content={"error": "CLOB unreachable"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/api/poly-yield/keys")
async def get_keys():
    """Retrieve metadata of stored keys (masked)."""
    return keystore.list_keys()

@app.post("/api/poly-yield/keys")
async def save_key(submit: KeySubmit, _=Depends(verify_token)):
    """Save/Encrypt credentials locally."""
    res = keystore.add_key(submit.service, submit.value, submit.label)
    # If the user saved a new wallet private key, re-init CLOB client
    if submit.service == "polymarket_wallet":
        await poly_yield_engine._init_clob()
    return res

@app.post("/api/poly-yield/manual-trade")
async def place_manual_trade(trade: ManualTradeArgs, _=Depends(verify_token)):
    """Place a manual trade bypassing strategy evaluations."""
    # Input validation — a price outside (0,1) or non-positive stake corrupts
    # share math (division by zero / negative shares)
    if not trade.market_id or not trade.market_id.strip():
        raise HTTPException(status_code=400, detail="market_id is required")
    if not (0.0 < trade.price < 1.0):
        raise HTTPException(status_code=400, detail="Price must be between 0 and 1 (exclusive)")
    if trade.stake_usdc <= 0:
        raise HTTPException(status_code=400, detail="Stake must be positive")
    for label, v in (("Stop loss", trade.stop_loss_price), ("Take profit", trade.take_profit_price)):
        if v is not None and not (0.0 < v < 1.0):
            raise HTTPException(status_code=400, detail=f"{label} price must be between 0 and 1")
    if trade.trailing_stop_pct is not None and not (0.0 < trade.trailing_stop_pct < 100.0):
        raise HTTPException(status_code=400, detail="Trailing stop must be between 0 and 100 percent")
    if trade.stop_loss_price is not None and trade.stop_loss_price >= trade.price:
        raise HTTPException(status_code=400, detail="Stop loss must be below the entry price")
    if trade.take_profit_price is not None and trade.take_profit_price <= trade.price:
        raise HTTPException(status_code=400, detail="Take profit must be above the entry price")

    opp = {
        "id": f"opp_manual_{uuid.uuid4().hex[:8]}",
        "strategy": "manual",
        "market_id": trade.market_id,
        "market_title": trade.market_title,
        "outcome": trade.outcome,
        "entry_price": trade.price,
        "suggested_usdc": trade.stake_usdc,
        "token_id": trade.token_id,
        "stop_loss_price": trade.stop_loss_price,
        "take_profit_price": trade.take_profit_price,
        "trailing_stop_pct": trade.trailing_stop_pct,
        "exec_mode": "manual",
        "risk_level": "Medium",
        "annualized_apy": 0.0,
        "profit_pct": 0.0,
        "days_to_expiry": 30.0,
        "action": "BUY",
        "instructions": [],
        "legs": []
    }
    
    # Save opportunity record so foreign keys validate
    await poly_yield_engine._upsert_opportunity(opp)
    
    res = await poly_yield_engine.execute_opportunity(opp, triggered_by="manual")
    if res.get("success"):
        return res
    else:
        raise HTTPException(status_code=400, detail=res.get("error", "Execution failed"))

@app.post("/api/poly-yield/exit/{pos_id}")
async def exit_open_position(pos_id: str, args: ExitArgs, _=Depends(verify_token)):
    """Manually exit an open position."""
    if not (0.0 < args.current_price <= 1.0):
        raise HTTPException(status_code=400, detail="Exit price must be between 0 and 1")
    res = await poly_yield_engine.exit_position(pos_id, args.current_price, reason="Manual Exit")
    if res.get("success"):
        return res
    else:
        raise HTTPException(status_code=400, detail=res.get("error", "Exit failed"))

@app.post("/api/poly-yield/paper-deposit")
async def paper_deposit(args: PaperFundArgs, _=Depends(verify_token)):
    """Add funds to paper wallet."""
    if args.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")
    from services.wallet import wallet_service
    new_balance = wallet_service.credit("paper", args.amount, "deposit",
                                         description=f"Manual deposit of ${args.amount:.2f}")
    return {"balance": new_balance}

@app.post("/api/poly-yield/paper-reset")
async def paper_reset(args: PaperFundArgs, _=Depends(verify_token)):
    """Reset paper wallet to a specific amount."""
    if args.amount < 0:
        raise HTTPException(status_code=400, detail="Amount must be non-negative")
    from services.wallet import wallet_service
    new_balance = wallet_service.set_balance("paper", args.amount,
                                              description=f"Paper wallet reset to ${args.amount:.2f}")
    return {"balance": new_balance}

@app.get("/api/poly-yield/ledger")
async def get_ledger(mode: str = None, limit: int = 100):
    """Get wallet transaction history for audit."""
    from services.wallet import wallet_service
    return wallet_service.get_ledger(mode, limit)

@app.get("/api/poly-yield/wallet-health")
async def wallet_health():
    """Conservation-of-money health check."""
    from services.wallet import wallet_service
    return wallet_service.verify_conservation("paper")

# --- Dutching Bot & Multi-LLM Arena Endpoints ---

class DutchingAllocateArgs(BaseModel):
    provider: str
    model_name: str = ""
    allocated_budget_usdc: float

class DutchingEvaluateArgs(BaseModel):
    market_question: str
    market_description: str = ""
    candidates: list
    top_set_names: list
    providers: list = ["openai", "anthropic", "kimi", "deepseek"]

class DutchingExecuteArgs(BaseModel):
    opportunity_id: str
    instance_id: str = "inst_manual_1"
    p_model_top_set: float = 0.90
    p_tail_risk: float = 0.10
    confidence: float = 0.80

@app.get("/api/dutching/arena")
async def get_dutching_arena():
    """Get active multi-LLM model allocations & leaderboard statistics."""
    conn = get_sqlite()
    with _sqlite_lock:
        rows = conn.execute(
            "SELECT * FROM dutching_arena_instances ORDER BY total_pnl DESC, created_at ASC"
        ).fetchall()
        if not rows:
            defaults = [
                ("inst_openai", "openai", "gpt-4o", 10.0),
                ("inst_anthropic", "anthropic", "claude-3-5-sonnet-20241022", 10.0),
                ("inst_kimi", "kimi", "moonshot-v1-8k", 10.0),
                ("inst_deepseek", "deepseek", "deepseek-chat", 10.0)
            ]
            for i_id, prov, model, budget in defaults:
                conn.execute(
                    "INSERT OR IGNORE INTO dutching_arena_instances (id, provider, model_name, allocated_budget_usdc) VALUES (?, ?, ?, ?)",
                    [i_id, prov, model, budget]
                )
            conn.commit()
            rows = conn.execute(
                "SELECT * FROM dutching_arena_instances ORDER BY total_pnl DESC, created_at ASC"
            ).fetchall()
    results = []
    for r in rows:
        d = dict(r)
        prov = d["provider"].lower()
        # keystore might store keys under the exact provider name (e.g. 'openai') 
        # or provider-specific suffixes. The JS uses keys like 'openai', 'anthropic', etc.
        d["has_key"] = bool(keystore.get_decrypted(prov))
        results.append(d)
        
    return results

@app.post("/api/dutching/arena/allocate")
async def allocate_dutching_instance(args: DutchingAllocateArgs, _=Depends(verify_token)):
    """Allocate a dedicated USDC wallet balance to an LLM provider instance."""
    if args.allocated_budget_usdc < 0:
        raise HTTPException(status_code=400, detail="Allocation budget must be non-negative")
    
    conn = get_sqlite()
    instance_id = f"inst_{args.provider.lower()}_{uuid.uuid4().hex[:6]}"
    default_models = {
        "openai": "gpt-4o",
        "anthropic": "claude-3-5-sonnet-20241022",
        "kimi": "moonshot-v1-8k",
        "deepseek": "deepseek-chat"
    }
    model_name = args.model_name or default_models.get(args.provider.lower(), "custom-model")
    
    with _sqlite_lock:
        conn.execute(
            """INSERT OR REPLACE INTO dutching_arena_instances
               (id, provider, model_name, allocated_budget_usdc, updated_at)
               VALUES (?, ?, ?, ?, datetime('now'))""",
            [instance_id, args.provider.lower(), model_name, args.allocated_budget_usdc]
        )
        conn.commit()
    return {"id": instance_id, "provider": args.provider, "allocated_budget_usdc": args.allocated_budget_usdc}

@app.post("/api/dutching/evaluate")
async def evaluate_dutching_market(args: DutchingEvaluateArgs, _=Depends(verify_token)):
    """Run side-by-side market evaluation across specified LLM providers."""
    from services.llm_provider import llm_provider
    results = {}
    
    async def _eval_one(prov: str):
        return prov, await llm_provider.evaluate_market(
            provider_key=prov,
            market_question=args.market_question,
            market_description=args.market_description,
            candidates=args.candidates,
            top_set_names=args.top_set_names
        )

    tasks = [_eval_one(p) for p in args.providers]
    eval_outs = await asyncio.gather(*tasks, return_exceptions=True)
    
    for item in eval_outs:
        if isinstance(item, tuple):
            prov, res = item
            results[prov] = res
        elif isinstance(item, Exception):
            _log.warning("Evaluation exception in arena: %s", item)

    return {"market_question": args.market_question, "evaluations": results}

@app.post("/api/dutching/keys")
async def save_dutching_key(args: KeySubmit, _=Depends(verify_token)):
    """Save encrypted API key for an LLM provider into Key Vault."""
    if not args.service or not args.value:
        raise HTTPException(status_code=400, detail="Service and key value are required")
    res = keystore.add_key(args.service.lower(), args.value, args.label or f"{args.service} LLM Key")
    return {"status": "saved", "service": args.service, "key_id": res["id"]}

@app.post("/api/dutching/execute")
async def execute_dutching_trade(args: DutchingExecuteArgs, _=Depends(verify_token)):
    """Execute a scanned Dutching opportunity through the standard hardened engine
    pipeline — the same execute_opportunity() path every other strategy uses. This
    gets Dutching, for free: Kelly/fixed-size allocation, the drawdown limit, the
    daily-loss and consecutive-loss circuit breakers, the per-market lock, the
    opportunity freshness/idempotency guards, live pre-flight order-book
    verification with slippage/drift guards, and partial-fill rollback + killswitch.
    (It also means Dutching respects its own exec_mode gate like every other
    strategy — blocked while s20_dutching.exec_mode is 'manual', the default;
    switch it to semi/auto in the Strategy Control Panel to unlock this.)

    dutching_trades only carries LLM-arena metadata now (p_model_top_set,
    p_tail_risk, confidence, per-model leaderboard attribution) — the resulting
    poly_yield_positions row, linked via position_id, is the source of truth for
    the trade's cost, PnL, and settlement.
    """
    conn = get_sqlite()
    with _sqlite_lock:
        row = conn.execute(
            "SELECT * FROM poly_yield_opportunities WHERE id = ?", [args.opportunity_id]
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Opportunity not found")

    opp = dict(row)
    if opp.get("strategy") != "s20_dutching":
        raise HTTPException(status_code=400, detail="Opportunity is not a Dutching opportunity")
    if opp.get("legs"):
        opp["legs"] = json.loads(opp["legs"])
    if opp.get("instructions"):
        opp["instructions"] = json.loads(opp["instructions"])

    result = await poly_yield_engine.execute_opportunity(opp, triggered_by="manual")
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Execution failed"))

    position_id = result.get("position_id")
    stake_usdc = float(result.get("cost_usdc") or opp.get("suggested_usdc") or 0.0)
    legs = opp.get("legs") or []
    mode = await cfg.get_typed("poly_yield.active_mode", str, "paper")
    trade_id = f"dutch_tx_{uuid.uuid4().hex[:8]}"
    sum_market_price = sum(float(l.get("market_price", l.get("price", 0))) for l in legs)
    sum_fill_price = sum(float(l.get("fill_price", l.get("price", 0))) for l in legs)

    with _sqlite_lock:
        conn.execute(
            """INSERT INTO dutching_trades
               (id, instance_id, market_id, market_title, top_candidates_json, sum_market_price,
                sum_fill_price, p_model_top_set, p_tail_risk, confidence, stake_usdc, legs_json, mode, position_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                trade_id, args.instance_id, opp.get("market_id"), opp.get("market_title"),
                json.dumps([l.get("outcome") for l in legs]),
                sum_market_price, sum_fill_price, args.p_model_top_set,
                args.p_tail_risk, args.confidence, stake_usdc,
                json.dumps(legs), mode, position_id
            ]
        )
        # Ensure the arena instance row exists (e.g. the "Quick Trade" button targets
        # inst_manual_1, which is never created by the arena leaderboard seeding path)
        # so the budget/position update below isn't silently dropped.
        conn.execute(
            "INSERT OR IGNORE INTO dutching_arena_instances (id, provider, model_name) VALUES (?, ?, ?)",
            [args.instance_id, args.instance_id, "manual"]
        )
        conn.execute(
            "UPDATE dutching_arena_instances SET used_budget_usdc = used_budget_usdc + ?, active_positions = active_positions + 1 WHERE id = ?",
            [stake_usdc, args.instance_id]
        )
        conn.commit()

    new_balance = await portfolio_allocator._get_wallet_balance()
    return {
        "status": "executed",
        "trade_id": trade_id,
        "position_id": position_id,
        "mode": mode,
        "stake_usdc": stake_usdc,
        "new_balance": new_balance
    }

# --- WebSocket Telemetry Endpoint ---

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_websockets.append(websocket)
    try:
        # Send initial configuration
        config_data = await get_config()
        stats_data = await get_stats()
        await websocket.send_json({
            "type": "welcome",
            "config": config_data,
            "stats": stats_data
        })
        
        while True:
            # Maintain active connection
            await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in active_websockets:
            active_websockets.remove(websocket)
    except Exception:
        if websocket in active_websockets:
            active_websockets.remove(websocket)

# --- Serving Front-End Dashboard ---

from fastapi import Response

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard(request: Request):
    """Serve the single-page Glassmorphic dashboard."""
    try:
        with open("templates/index.html", "r", encoding="utf-8") as f:
            html_content = f.read()
        return html_content
    except FileNotFoundError:
        return """
        <html>
            <body style="background:#0b0d19; color:#fff; font-family:sans-serif; text-align:center; padding-top:20%;">
                <h1>PolyYield Dashboard template not found!</h1>
                <p>Ensure templates/index.html is created and populated.</p>
            </body>
        </html>
        """


if __name__ == "__main__":
    # Allows `python main.py` / `pm2 start main.py --interpreter python3` to actually
    # boot the server (previously this file defined the app but never ran it).
    import uvicorn
    uvicorn.run(app, host=settings.host, port=settings.port)
