"""
PolyYield Core Unit Tests
Validates config parsing, keystore encryption, gas tracker conversions, and Kelly portfolio sizing.
"""
import pytest
from db.database import init_db
from db.config import cfg
from services.keystore import encrypt_value, decrypt_value
from services.portfolio_allocator import portfolio_allocator

@pytest.fixture(autouse=True)
def setup_test_db():
    import db.database
    import sqlite3
    
    # Create an isolated in-memory connection for testing
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    
    # Override global connection in database module
    db.database._sqlite_conn = conn
    
    # Scaffold configurations and tables in memory
    init_db()
    
    yield conn
    
    # Close connection and reset database module pointer
    conn.close()
    db.database._sqlite_conn = None

def test_dynamic_config():
    # Set and retrieve a config parameter
    cfg.set("poly_yield.test_param", "42")
    val = cfg.get("poly_yield.test_param")
    assert val == "42"

def test_keystore_encryption():
    secret = "0xmysecretpolygonwalletprivatekey"
    token = encrypt_value(secret)
    decrypted = decrypt_value(token)
    assert decrypted == secret

@pytest.mark.asyncio
async def test_kelly_sizing():
    # Seed kelly_fraction to a known value for deterministic test
    cfg.set("portfolio.kelly_fraction", "0.10")

    # 1. Edge exists: True prob (10%) > Implied Price (5%)
    # f* = (p - P) / (1 - P) = (0.10 - 0.05) / 0.95 = 0.05263
    # Allocated USDC = $1000 * 0.05263 * 0.10 = $5.26
    size = await portfolio_allocator.calculate_kelly_size(
        implied_price=0.05,
        true_prob=0.10,
        total_capital=1000.0
    )
    assert 5.0 < size < 6.0, f"Expected ~$5.26, got ${size:.2f}"

    # 2. No edge: True prob (3%) < Implied Price (5%)
    size_zero = await portfolio_allocator.calculate_kelly_size(
        implied_price=0.05,
        true_prob=0.03,
        total_capital=1000.0
    )
    assert size_zero == 0.0

    # 3. Edge case: true_prob == implied_price (no edge)
    size_equal = await portfolio_allocator.calculate_kelly_size(
        implied_price=0.50,
        true_prob=0.50,
        total_capital=1000.0
    )
    assert size_equal == 0.0

    # 4. Edge case: implied_price == 1.0 (division by zero guard)
    size_one = await portfolio_allocator.calculate_kelly_size(
        implied_price=1.0,
        true_prob=0.99,
        total_capital=1000.0
    )
    assert size_one == 0.0

@pytest.mark.asyncio
async def test_manual_trade_and_exit():
    from db.database import get_sqlite, _sqlite_lock
    from strategies.engine import poly_yield_engine
    
    # 1. Place a manual paper trade opportunity
    opp = {
        "id": "opp_test_manual_123",
        "strategy": "manual",
        "market_id": "test_market_1",
        "market_title": "Test Manual Market",
        "outcome": "YES",
        "entry_price": 0.60,
        "suggested_usdc": 100.0,
        "token_id": "test_token_1",
        "stop_loss_price": 0.50,
        "take_profit_price": 0.80,
        "trailing_stop_pct": 10.0,
        "exec_mode": "manual",
        "risk_level": "Low",
        "annualized_apy": 0.0,
        "profit_pct": 0.0,
        "days_to_expiry": 30.0,
        "action": "BUY",
        "instructions": [],
        "legs": []
    }
    
    # Run database scaffolding and reset configs for testing
    init_db()
    cfg.set("poly_yield.enabled", "true")
    cfg.set("poly_yield.active_mode", "paper")
    
    # Execute through engine
    await poly_yield_engine._upsert_opportunity(opp)
    res = await poly_yield_engine.execute_opportunity(opp)
    assert res.get("success") is True
    
    # 2. Check position record
    conn = get_sqlite()
    with _sqlite_lock:
        pos = conn.execute("SELECT * FROM poly_yield_positions WHERE opportunity_id = ?", [opp["id"]]).fetchone()
    
    assert pos is not None
    assert pos["stop_loss_price"] == 0.50
    assert pos["take_profit_price"] == 0.80
    assert pos["trailing_stop_pct"] == 10.0
    assert pos["status"] == "open"
    assert pos["shares"] == pytest.approx(100.0 / 0.60, 0.01)

    # 3. Manually exit position
    exit_res = await poly_yield_engine.exit_position(pos["id"], current_price=0.70, reason="Manual Exit")
    assert exit_res["success"] is True
    assert exit_res["exit_price"] == 0.70
    
    with _sqlite_lock:
        pos_after = conn.execute("SELECT * FROM poly_yield_positions WHERE id = ?", [pos["id"]]).fetchone()
    assert pos_after["status"] == "won"
    assert pos_after["realized_pnl"] > 0
    assert pos_after["settlement_outcome"] == "exit_manual_exit"

@pytest.mark.asyncio
async def test_consecutive_losses_circuit_breaker():
    from db.database import get_sqlite, _sqlite_lock
    
    init_db()
    cfg.set("poly_yield.enabled", "true")
    cfg.set("portfolio.consecutive_loss_limit", "3")
    
    conn = get_sqlite()
    # Insert 3 consecutive losses
    with _sqlite_lock:
        conn.execute("DELETE FROM poly_yield_positions")
        for i in range(3):
            conn.execute("""
                INSERT INTO poly_yield_positions (id, strategy, market_id, outcome, shares, entry_price, cost_usdc, status, realized_pnl, settled_at, mode)
                VALUES (?, 'manual', 'm1', 'YES', 10, 0.5, 5.0, 'lost', -5.0, datetime('now'), 'paper')
            """, [f"p_loss_{i}"])
        conn.commit()

    # Request allocation should be denied & engine disabled
    from services.portfolio_allocator import AllocationDeniedError
    with pytest.raises(AllocationDeniedError):
        await portfolio_allocator.request_allocation("manual", "m1", 10.0)
    assert cfg.get("poly_yield.enabled") == "false"

@pytest.mark.asyncio
async def test_daily_loss_limit():
    from db.database import get_sqlite, _sqlite_lock
    
    init_db()
    cfg.set("poly_yield.enabled", "true")
    cfg.set("portfolio.daily_loss_limit", "50.0")
    
    conn = get_sqlite()
    # Insert a big loss today ($60)
    with _sqlite_lock:
        conn.execute("DELETE FROM poly_yield_positions")
        conn.execute("""
            INSERT INTO poly_yield_positions (id, strategy, market_id, outcome, shares, entry_price, cost_usdc, status, realized_pnl, settled_at, mode)
            VALUES ('p_big_loss', 'manual', 'm1', 'YES', 120, 0.5, 60.0, 'lost', -60.0, datetime('now'), 'paper')
        """)
        conn.commit()

    # Request allocation should be denied due to daily loss limit
    from services.portfolio_allocator import AllocationDeniedError
    with pytest.raises(AllocationDeniedError):
        await portfolio_allocator.request_allocation("manual", "m1", 10.0)

@pytest.mark.asyncio
async def test_stats_and_positions_mode_isolation():
    from db.database import get_sqlite, _sqlite_lock
    from strategies.engine import poly_yield_engine
    
    init_db()
    conn = get_sqlite()
    with _sqlite_lock:
        conn.execute("DELETE FROM poly_yield_positions")
        conn.execute("DELETE FROM poly_yield_stats")
        
        # Insert a paper position
        conn.execute("""
            INSERT INTO poly_yield_positions (id, opportunity_id, strategy, market_id, market_title, outcome, shares, entry_price, cost_usdc, status, realized_pnl, mode)
            VALUES ('pos_paper_1', 'opp1', 'manual', 'm1', 'M1', 'YES', 20, 0.5, 10.0, 'won', 10.0, 'paper')
        """)
        conn.execute("""
            INSERT INTO poly_yield_stats (strategy, mode, total_pnl, total_returned, win_count)
            VALUES ('manual', 'paper', 10.0, 10.0, 1)
        """)
        
        # Insert a live position
        conn.execute("""
            INSERT INTO poly_yield_positions (id, opportunity_id, strategy, market_id, market_title, outcome, shares, entry_price, cost_usdc, status, realized_pnl, mode)
            VALUES ('pos_live_1', 'opp2', 'manual', 'm1', 'M1', 'YES', 40, 0.5, 20.0, 'won', 20.0, 'live')
        """)
        conn.execute("""
            INSERT INTO poly_yield_stats (strategy, mode, total_pnl, total_returned, win_count)
            VALUES ('manual', 'live', 20.0, 20.0, 1)
        """)
        conn.commit()
        
    # Check that portfolio allocator checks only count the current mode's exposure
    cfg.set("poly_yield.active_mode", "paper")
    cfg.set("portfolio.daily_loss_limit", "5.0")
    alloc_paper = await portfolio_allocator.request_allocation("manual", "m1", 10.0)
    assert alloc_paper > 0.0
    
    # Trigger a big loss in paper to test circuit breaker
    with _sqlite_lock:
        conn.execute("""
            INSERT INTO poly_yield_positions (id, opportunity_id, strategy, market_id, market_title, outcome, shares, entry_price, cost_usdc, status, realized_pnl, settled_at, mode)
            VALUES ('pos_paper_loss', 'opp3', 'manual', 'm1', 'M1', 'YES', 20, 0.5, 10.0, 'lost', -10.0, datetime('now'), 'paper')
        """)
        conn.commit()
        
    # Alloc for paper should now be blocked
    from services.portfolio_allocator import AllocationDeniedError
    with pytest.raises(AllocationDeniedError):
        await portfolio_allocator.request_allocation("manual", "m1", 10.0)
    
    # Switch to live mode
    cfg.set("poly_yield.active_mode", "live")
    # Live mode has no losses (realized_pnl is +20), so alloc should be allowed
    from unittest.mock import patch
    with patch.object(portfolio_allocator, "_get_wallet_balance", return_value=100.0):
        alloc_live = await portfolio_allocator.request_allocation("manual", "m1", 10.0)
        assert alloc_live > 0.0



