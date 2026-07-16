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
    print(f"DEBUG AUTH: creds={creds}, expected={settings.api_secret}")
    if creds:
        print(f"DEBUG AUTH: creds.credentials={creds.credentials}")
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
    """Retrieve scanned opportunities from database."""
    conn = get_sqlite()
    with _sqlite_lock:
        rows = conn.execute(
            "SELECT * FROM poly_yield_opportunities ORDER BY updated_at DESC"
        ).fetchall()
    
    opportunities = []
    for row in rows:
        d = dict(row)
        # Parse legs and instructions if present
        if d.get("legs"):
            d["legs"] = json.loads(d["legs"])
        if d.get("instructions"):
            d["instructions"] = json.loads(d["instructions"])
            
        # Dynamically recalculate suggested size based on current wallet balance
        from services.portfolio_allocator import portfolio_allocator, AllocationDeniedError
        try:
            true_prob = (d.get("implied_prob") / 100.0) if d.get("implied_prob") is not None else None
            alloc = await portfolio_allocator.request_allocation(
                strategy_key=d["strategy"],
                market_id=d["market_id"],
                suggested_usdc=d.get("suggested_usdc", 0.0),
                implied_price=d.get("entry_price"),
                true_prob=true_prob
            )
            d["suggested_usdc"] = alloc
        except AllocationDeniedError:
            d["suggested_usdc"] = 0.0
        except Exception as e:
            # Fallback
            pass
            
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

@app.get("/api/debug-secret")
async def get_debug_secret():
    return {"secret": settings.api_secret}

@app.get("/api/debug-auth")
async def get_debug_auth(creds: HTTPAuthorizationCredentials = Depends(_bearer)):
    return {"creds": creds.credentials if creds else None, "expected": settings.api_secret, "match": (creds.credentials == settings.api_secret) if creds else False}

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
async def get_history():
    """Retrieve all historical positions across all modes."""
    conn = get_sqlite()
    with _sqlite_lock:
        rows = conn.execute(
            "SELECT * FROM poly_yield_positions ORDER BY entry_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]

@app.get("/api/poly-yield/config")
async def get_config():
    """Retrieve all configuration parameters."""
    conn = get_sqlite()
    with _sqlite_lock:
        rows = conn.execute("SELECT key, value FROM system_config").fetchall()
    return {r["key"]: r["value"] for r in rows}

@app.post("/api/poly-yield/config")
async def update_config(update: ConfigUpdate, _=Depends(verify_token)):
    """Update a configuration value dynamically."""
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

    result = await poly_yield_engine.execute_opportunity(opp)
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
    
    res = await poly_yield_engine.execute_opportunity(opp)
    if res.get("success"):
        return res
    else:
        raise HTTPException(status_code=400, detail=res.get("error", "Execution failed"))

@app.post("/api/poly-yield/exit/{pos_id}")
async def exit_open_position(pos_id: str, args: ExitArgs, _=Depends(verify_token)):
    """Manually exit an open position."""
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
        active_websockets.remove(websocket)
    except Exception:
        if websocket in active_websockets:
            active_websockets.remove(websocket)

# --- Serving Front-End Dashboard ---

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
