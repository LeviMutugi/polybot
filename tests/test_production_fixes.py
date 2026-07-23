"""
Regression tests for production-readiness fixes:
- Order book normalization (Polymarket returns best price LAST)
- VWAP walk correctness and fillability guard
- Kelly sizing unit sanitization (percent-vs-fraction bug)
- Opportunity freshness / staleness guards
- Manual-mode strategies cannot be executed through the engine
- Settlement winner detection requires a definitive price
"""
import os
import sys
import sqlite3
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.database import init_db
from db.config import cfg


@pytest.fixture(autouse=True)
def setup_test_db():
    import db.database
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.database._sqlite_conn = conn
    init_db()
    yield conn
    conn.close()
    db.database._sqlite_conn = None


# ---------- Order book normalization ----------

def test_sort_book_levels_normalizes_polymarket_ordering():
    from strategies.base import sort_book_levels
    # Polymarket-style: bids ascending (best LAST), asks descending (best LAST)
    bids = [{"price": "0.01", "size": "10"}, {"price": "0.48", "size": "10"}, {"price": "0.50", "size": "10"}]
    asks = [{"price": "0.99", "size": "10"}, {"price": "0.55", "size": "10"}, {"price": "0.52", "size": "10"}]

    sorted_bids = sort_book_levels(bids, "bids")
    sorted_asks = sort_book_levels(asks, "asks")
    assert float(sorted_bids[0]["price"]) == 0.50, "Best bid must be the HIGHEST bid"
    assert float(sorted_asks[0]["price"]) == 0.52, "Best ask must be the LOWEST ask"


class _StubResp:
    status_code = 200
    def __init__(self, data):
        self._data = data
    def json(self):
        return self._data


class _StubClient:
    def __init__(self, data):
        self._data = data
    async def get(self, url, params=None):
        return _StubResp(self._data)


@pytest.mark.asyncio
async def test_vwap_walks_best_prices_first():
    from strategies.base import calculate_execution_price
    # Raw Polymarket ordering: worst prices first in the arrays
    book = {
        "bids": [{"price": "0.01", "size": "1000"}, {"price": "0.50", "size": "1000"}],
        "asks": [{"price": "0.99", "size": "1000"}, {"price": "0.52", "size": "1000"}],
    }
    res = await calculate_execution_price("tok", 52.0, side="buy", http_client=_StubClient(book))
    # $52 buys exactly 100 shares at the BEST ask 0.52 — a naive walk would price at 0.99
    assert abs(res["price"] - 0.52) < 1e-9
    assert res["slippage"] == 0

    res_sell = await calculate_execution_price("tok", 50.0, side="sell", http_client=_StubClient(book))
    assert abs(res_sell["price"] - 0.50) < 1e-9


def test_is_fillable_guards():
    from strategies.base import is_fillable
    assert is_fillable({"price": 0.5, "slippage": 0.5}, 1.5)
    assert not is_fillable({"price": 0.5, "slippage": 2.5}, 1.5)
    assert not is_fillable({"price": 0.5, "slippage": 0, "error": "No liquidity"}, 1.5)
    assert not is_fillable({"price": 0.4, "slippage": 99.0, "warning": "Insufficient liquidity"}, 1.5)
    assert not is_fillable({"price": 0.0, "slippage": 0}, 1.5)


def test_apy_functions_are_bounded():
    from strategies.base import calculate_compounding_apy, calculate_simple_apy, APY_CAP_PCT
    # 2% yield on a 6-hour market must not annualize with days=0.25
    assert calculate_simple_apy(2.0, 0.25) == pytest.approx(2.0 * 365.0)
    assert calculate_compounding_apy(0.5, 0.01) <= APY_CAP_PCT
    assert calculate_compounding_apy(-1.5, 10) == -100.0
    assert calculate_simple_apy(None, 10) == 0.0


# ---------- Kelly unit sanitization ----------

@pytest.mark.asyncio
async def test_kelly_percent_input_falls_back_to_fixed_sizing():
    """A percent (93.5) passed where a fraction belongs must NOT produce a max-Kelly bet."""
    from services.portfolio_allocator import portfolio_allocator
    cfg.set("poly_yield.active_mode", "paper")
    cfg.set("portfolio.paper_balance", "1000.0")
    cfg.set("s1_novelty.exec_mode", "semi")
    cfg.set("s1_novelty.max_position_pct", "0.02")

    alloc = await portfolio_allocator.request_allocation(
        "s1_novelty", "m1", suggested_usdc=20.0,
        implied_price=0.93, true_prob=93.5  # buggy percent input
    )
    # Fallback sizing: 2% of $1000 = $20, NOT the 20%-of-capital Kelly cap ($200)
    assert alloc <= 20.0


@pytest.mark.asyncio
async def test_kelly_fraction_input_sizes_correctly():
    from services.portfolio_allocator import portfolio_allocator
    cfg.set("poly_yield.active_mode", "paper")
    cfg.set("portfolio.paper_balance", "1000.0")
    cfg.set("portfolio.kelly_fraction", "0.10")
    cfg.set("s1_novelty.exec_mode", "semi")

    alloc = await portfolio_allocator.request_allocation(
        "s1_novelty", "m1", suggested_usdc=50.0,
        implied_price=0.93, true_prob=0.955
    )
    # f* = (0.955-0.93)/0.07 = 0.3571; × 0.10 kelly_fraction = 3.57% of $1000 ≈ $35.7
    assert 30.0 < alloc < 40.0


@pytest.mark.asyncio
async def test_allocation_never_exceeds_suggested():
    from services.portfolio_allocator import portfolio_allocator
    cfg.set("poly_yield.active_mode", "paper")
    cfg.set("portfolio.paper_balance", "1000.0")
    cfg.set("s6_longshot.exec_mode", "auto")

    alloc = await portfolio_allocator.request_allocation(
        "s6_longshot", "m2", suggested_usdc=5.0,
        implied_price=0.50, true_prob=0.99  # huge Kelly edge
    )
    assert alloc <= 5.0


# ---------- Freshness / staleness ----------

@pytest.mark.asyncio
async def test_stale_opportunity_is_rejected_and_marked(setup_test_db):
    from db.database import get_sqlite, _sqlite_lock
    from strategies.engine import poly_yield_engine
    conn = get_sqlite()
    with _sqlite_lock:
        conn.execute(
            "INSERT INTO poly_yield_opportunities (id, strategy, market_id, status, updated_at) "
            "VALUES ('opp_old', 'manual', 'm1', 'open', datetime('now', '-2 hours'))"
        )
        conn.commit()

    err = await poly_yield_engine._opportunity_freshness_error({"id": "opp_old"})
    assert err is not None and "stale" in err.lower()

    with _sqlite_lock:
        row = conn.execute("SELECT status FROM poly_yield_opportunities WHERE id = 'opp_old'").fetchone()
    assert row["status"] == "stale"


@pytest.mark.asyncio
async def test_executed_opportunity_cannot_be_re_executed(setup_test_db):
    from db.database import get_sqlite, _sqlite_lock
    from strategies.engine import poly_yield_engine
    conn = get_sqlite()
    with _sqlite_lock:
        conn.execute(
            "INSERT INTO poly_yield_opportunities (id, strategy, market_id, status, updated_at) "
            "VALUES ('opp_done', 'manual', 'm1', 'executed', datetime('now'))"
        )
        conn.commit()
    err = await poly_yield_engine._opportunity_freshness_error({"id": "opp_done"})
    assert err is not None and "no longer available" in err.lower()


@pytest.mark.asyncio
async def test_execute_rejects_stale_opportunity(setup_test_db):
    from db.database import get_sqlite, _sqlite_lock
    from strategies.engine import poly_yield_engine
    conn = get_sqlite()
    cfg.set("poly_yield.enabled", "true")
    cfg.set("poly_yield.active_mode", "paper")
    with _sqlite_lock:
        conn.execute(
            "INSERT INTO poly_yield_opportunities (id, strategy, market_id, status, updated_at) "
            "VALUES ('opp_stale_x', 'manual', 'm9', 'open', datetime('now', '-2 hours'))"
        )
        conn.commit()
    res = await poly_yield_engine.execute_opportunity({
        "id": "opp_stale_x", "strategy": "manual", "market_id": "m9",
        "market_title": "T", "outcome": "YES", "entry_price": 0.5, "suggested_usdc": 10.0,
    })
    assert res["success"] is False
    assert "stale" in res["error"].lower()


def test_mark_missing_stale(setup_test_db):
    from db.database import get_sqlite, _sqlite_lock
    from strategies.engine import poly_yield_engine
    conn = get_sqlite()
    with _sqlite_lock:
        conn.execute("INSERT INTO poly_yield_opportunities (id, strategy, market_id, status) VALUES ('s1_a', 's1_novelty', 'm1', 'open')")
        conn.execute("INSERT INTO poly_yield_opportunities (id, strategy, market_id, status) VALUES ('s1_b', 's1_novelty', 'm2', 'open')")
        conn.commit()

    poly_yield_engine._mark_missing_stale({"s1_novelty": {"s1_a"}})

    with _sqlite_lock:
        a = conn.execute("SELECT status FROM poly_yield_opportunities WHERE id = 's1_a'").fetchone()
        b = conn.execute("SELECT status FROM poly_yield_opportunities WHERE id = 's1_b'").fetchone()
    assert a["status"] == "open"
    assert b["status"] == "stale"


# ---------- Manual-mode strategies can't be engine-executed ----------

@pytest.mark.asyncio
async def test_manual_mode_strategy_execution_rejected(setup_test_db):
    from strategies.engine import poly_yield_engine
    cfg.set("poly_yield.enabled", "true")
    cfg.set("poly_yield.active_mode", "paper")
    cfg.set("s2_split.exec_mode", "manual")

    res = await poly_yield_engine.execute_opportunity({
        "id": "opp_s2_test", "strategy": "s2_split", "market_id": "m5",
        "market_title": "LP", "outcome": "YES + NO", "entry_price": 0.5, "suggested_usdc": 10.0,
    })
    assert res["success"] is False
    assert "manual mode" in res["error"].lower()


# ---------- Settlement winner detection ----------

def test_winner_requires_definitive_price():
    from strategies.settlement import poly_yield_settlement
    # Closed but not definitively resolved — must NOT settle
    market = {"outcomes": '["Yes", "No"]', "outcomePrices": '["0.60", "0.40"]'}
    assert poly_yield_settlement._winning_outcome(market) is None

    resolved = {"outcomes": '["Yes", "No"]', "outcomePrices": '["0.995", "0.005"]'}
    assert poly_yield_settlement._winning_outcome(resolved) == "Yes"


# ---------- Paper trade lifecycle still conserves money after engine rework ----------

@pytest.mark.asyncio
async def test_paper_trade_wallet_conservation(setup_test_db, monkeypatch):
    from db.database import get_sqlite, _sqlite_lock
    from strategies.engine import poly_yield_engine
    from services.wallet import wallet_service

    # Deterministic simulated gas fee — the engine correctly debits this on open
    # and exit (so paper PnL isn't systematically optimistic vs. live); pin it so
    # the conservation math below isn't at the mercy of live gas/MATIC price lookups.
    import strategies.engine as engine_mod
    async def mock_gas(): return 0.01
    monkeypatch.setattr(engine_mod.gas_tracker, "get_gas_cost_usdc", mock_gas)

    cfg.set("poly_yield.enabled", "true")
    cfg.set("poly_yield.active_mode", "paper")
    cfg.set("portfolio.paper_balance", "1000.0")

    opp = {
        "id": "opp_conserve_1", "strategy": "manual", "market_id": "mc1",
        "market_title": "Conservation Test", "outcome": "YES",
        "entry_price": 0.50, "suggested_usdc": 100.0, "exec_mode": "manual",
        "instructions": [], "legs": [],
    }
    await poly_yield_engine._upsert_opportunity(opp)
    res = await poly_yield_engine.execute_opportunity(opp)
    assert res["success"] is True
    # Stake debited plus the simulated gas fee on open
    assert wallet_service.get_balance("paper") == pytest.approx(900.0 - 0.01)

    conn = get_sqlite()
    with _sqlite_lock:
        pos = conn.execute("SELECT * FROM poly_yield_positions WHERE opportunity_id = 'opp_conserve_1'").fetchone()
    assert pos is not None

    exit_res = await poly_yield_engine.exit_position(pos["id"], current_price=0.60, reason="Manual Exit")
    assert exit_res["success"] is True
    # 200 shares sold at $0.60 = $120 back, minus the simulated exit gas fee
    assert wallet_service.get_balance("paper") == pytest.approx(1020.0 - 0.02)

    health = wallet_service.verify_conservation("paper")
    assert health["valid"], health


@pytest.mark.asyncio
async def test_exit_rejects_invalid_price(setup_test_db):
    from strategies.engine import poly_yield_engine
    from db.database import get_sqlite, _sqlite_lock
    conn = get_sqlite()
    with _sqlite_lock:
        conn.execute("""
            INSERT INTO poly_yield_positions (id, strategy, market_id, outcome, shares, entry_price, cost_usdc, status, mode)
            VALUES ('pos_badexit', 'manual', 'm1', 'YES', 10, 0.5, 5.0, 'open', 'paper')
        """)
        conn.commit()
    res = await poly_yield_engine.exit_position("pos_badexit", current_price=5.0)
    assert res["success"] is False
    assert "invalid exit price" in res["error"].lower()
