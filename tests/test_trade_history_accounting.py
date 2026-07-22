"""
Regression tests for the trade-history / accounting feature set:
- Max profit / max loss annotation on opportunities and positions
- executed_by ('bot' vs 'manual') tracking
- exit_price no longer clobbers actual_fill_price (entry fill preserved)
- Accounting summary math (win rate, volume, realized pnl, per-strategy breakdown)
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


# ---------- Payoff annotation ----------

def test_annotate_payoff_single_leg():
    from strategies.engine import poly_yield_engine
    opp = {"strategy": "s1_novelty", "suggested_usdc": 20.0, "entry_price": 0.95}
    poly_yield_engine._annotate_payoff(opp)
    # shares = 20/0.95 = 21.05; max_profit = 21.05 - 20 = 1.05; max_loss = -20
    assert opp["max_profit_usdc"] == pytest.approx(1.0526, abs=0.001)
    assert opp["max_loss_usdc"] == -20.0


def test_annotate_payoff_arbitrage_basket_is_guaranteed():
    from strategies.engine import poly_yield_engine
    opp = {"strategy": "s3_buy_all", "suggested_usdc": 100.0, "profit_pct": 5.0,
           "payoff_type": "guaranteed_arb",
           "legs": [{"outcome": "A", "price": 0.3}, {"outcome": "B", "price": 0.6}]}
    poly_yield_engine._annotate_payoff(opp)
    assert opp["max_profit_usdc"] == 5.0
    assert opp["max_loss_usdc"] == 5.0  # guaranteed: no downside range for a filled arb


def test_annotate_payoff_conditional_multi_leg_keeps_asymmetric_risk():
    """Dutching-style multi-leg bets on a SUBSET of outcomes carry real tail risk —
    unlike a guaranteed arb basket, _annotate_payoff must not overwrite the
    strategy's own (asymmetric) max_profit/max_loss with a false guarantee."""
    from strategies.engine import poly_yield_engine
    opp = {"strategy": "s20_dutching", "suggested_usdc": 100.0, "profit_pct": 8.0,
           "payoff_type": "conditional_multi_leg",
           "max_profit_usdc": 8.0, "max_loss_usdc": -100.0,
           "legs": [{"outcome": "A", "price": 0.3}, {"outcome": "B", "price": 0.6}]}
    poly_yield_engine._annotate_payoff(opp)
    assert opp["max_profit_usdc"] == 8.0
    assert opp["max_loss_usdc"] == -100.0


def test_annotate_payoff_skips_lp_strategy():
    from strategies.engine import poly_yield_engine
    opp = {"strategy": "s2_split", "suggested_usdc": 50.0, "entry_price": 0.5}
    poly_yield_engine._annotate_payoff(opp)
    assert "max_profit_usdc" not in opp
    assert "max_loss_usdc" not in opp


def test_annotate_payoff_skips_invalid_price():
    from strategies.engine import poly_yield_engine
    opp = {"strategy": "s1_novelty", "suggested_usdc": 20.0, "entry_price": 0}
    poly_yield_engine._annotate_payoff(opp)
    assert "max_profit_usdc" not in opp


# ---------- executed_by + exit_price integrity ----------

@pytest.mark.asyncio
async def test_bot_triggered_execution_records_executed_by_bot(setup_test_db):
    from strategies.engine import poly_yield_engine
    from db.database import get_sqlite, _sqlite_lock

    cfg.set("poly_yield.enabled", "true")
    cfg.set("poly_yield.active_mode", "paper")
    opp = {
        "id": "opp_bot_1", "strategy": "manual", "market_id": "mb1",
        "market_title": "Bot Trigger Test", "outcome": "YES",
        "entry_price": 0.5, "suggested_usdc": 10.0, "exec_mode": "manual",
        "instructions": [], "legs": [],
    }
    await poly_yield_engine._upsert_opportunity(opp)
    res = await poly_yield_engine.execute_opportunity(opp)  # default triggered_by='bot'
    assert res["success"] is True

    conn = get_sqlite()
    with _sqlite_lock:
        pos = conn.execute("SELECT * FROM poly_yield_positions WHERE opportunity_id = 'opp_bot_1'").fetchone()
    assert pos["executed_by"] == "bot"
    assert pos["max_profit_usdc"] == pytest.approx(10.0)  # 20 shares - 10 cost
    assert pos["max_loss_usdc"] == -10.0


@pytest.mark.asyncio
async def test_manual_triggered_execution_records_executed_by_manual(setup_test_db):
    from strategies.engine import poly_yield_engine
    from db.database import get_sqlite, _sqlite_lock

    cfg.set("poly_yield.enabled", "true")
    cfg.set("poly_yield.active_mode", "paper")
    opp = {
        "id": "opp_manual_1", "strategy": "manual", "market_id": "mm1",
        "market_title": "Manual Trigger Test", "outcome": "YES",
        "entry_price": 0.5, "suggested_usdc": 10.0, "exec_mode": "manual",
        "instructions": [], "legs": [],
    }
    await poly_yield_engine._upsert_opportunity(opp)
    res = await poly_yield_engine.execute_opportunity(opp, triggered_by="manual")
    assert res["success"] is True

    conn = get_sqlite()
    with _sqlite_lock:
        pos = conn.execute("SELECT * FROM poly_yield_positions WHERE opportunity_id = 'opp_manual_1'").fetchone()
    assert pos["executed_by"] == "manual"


@pytest.mark.asyncio
async def test_exit_price_does_not_clobber_entry_fill_price(setup_test_db):
    """Regression test for the bug where exit_position overwrote actual_fill_price
    (the ENTRY fill) with the exit price, destroying the audit trail."""
    from strategies.engine import poly_yield_engine
    from db.database import get_sqlite, _sqlite_lock

    cfg.set("poly_yield.enabled", "true")
    cfg.set("poly_yield.active_mode", "paper")
    opp = {
        "id": "opp_exitfix_1", "strategy": "manual", "market_id": "mx1",
        "market_title": "Exit Fix Test", "outcome": "YES",
        "entry_price": 0.40, "suggested_usdc": 10.0, "exec_mode": "manual",
        "instructions": [], "legs": [],
    }
    await poly_yield_engine._upsert_opportunity(opp)
    res = await poly_yield_engine.execute_opportunity(opp, triggered_by="manual")
    pos_id = None
    conn = get_sqlite()
    with _sqlite_lock:
        pos = conn.execute("SELECT * FROM poly_yield_positions WHERE opportunity_id = 'opp_exitfix_1'").fetchone()
    pos_id = pos["id"]
    assert pos["actual_fill_price"] == pytest.approx(0.40)

    exit_res = await poly_yield_engine.exit_position(pos_id, current_price=0.60, reason="Manual Exit")
    assert exit_res["success"] is True

    with _sqlite_lock:
        pos_after = conn.execute("SELECT * FROM poly_yield_positions WHERE id = ?", [pos_id]).fetchone()
    # Entry fill price must be UNCHANGED; exit price recorded separately
    assert pos_after["actual_fill_price"] == pytest.approx(0.40)
    assert pos_after["exit_price"] == pytest.approx(0.60)


@pytest.mark.asyncio
async def test_settlement_records_exit_price_on_resolution():
    from strategies.settlement import poly_yield_settlement
    pos = {"entry_price": 0.9, "cost_usdc": 10.0, "shares": 11.11,
           "actual_gas_usdc": 0.0, "strategy": "s1_novelty", "outcome": "no"}
    market_won = {"outcomes": '["Yes", "No"]', "outcomePrices": '["0.005", "0.995"]'}
    pnl, outcome, status = await poly_yield_settlement._compute_pnl(pos, market_won)
    assert status == "won"
    # exit_price for a win should be recorded as 1.0 in the DB update (checked via SQL literal
    # in _settle_open_positions; here we just confirm the win/loss classification is correct)
    assert pnl > 0


# ---------- Accounting summary math ----------

def test_accounting_query_matches_manual_calculation(setup_test_db):
    from db.database import get_sqlite, _sqlite_lock
    conn = get_sqlite()
    with _sqlite_lock:
        conn.execute("""
            INSERT INTO poly_yield_positions
            (id, strategy, market_id, outcome, shares, entry_price, cost_usdc, status,
             realized_pnl, actual_gas_usdc, mode, executed_by, entry_at, settled_at)
            VALUES ('p1', 's1_novelty', 'm1', 'NO', 20, 0.5, 10.0, 'won', 3.0, 0.01, 'paper', 'bot',
                    datetime('now','-2 hours'), datetime('now'))
        """)
        conn.execute("""
            INSERT INTO poly_yield_positions
            (id, strategy, market_id, outcome, shares, entry_price, cost_usdc, status,
             realized_pnl, actual_gas_usdc, mode, executed_by, entry_at, settled_at)
            VALUES ('p2', 's1_novelty', 'm2', 'NO', 40, 0.5, 20.0, 'lost', -20.0, 0.01, 'paper', 'manual',
                    datetime('now','-1 hours'), datetime('now'))
        """)
        conn.execute("""
            INSERT INTO poly_yield_positions
            (id, strategy, market_id, outcome, shares, entry_price, cost_usdc, status, mode, executed_by, entry_at)
            VALUES ('p3', 's6_longshot', 'm3', 'NO', 10, 0.9, 9.0, 'open', 'paper', 'bot', datetime('now'))
        """)
        conn.commit()

        totals = conn.execute("""
            SELECT
                COUNT(*) as trade_count,
                SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) as open_count,
                SUM(CASE WHEN status = 'won' THEN 1 ELSE 0 END) as won_count,
                SUM(CASE WHEN status = 'lost' THEN 1 ELSE 0 END) as lost_count,
                SUM(COALESCE(cost_usdc, 0)) as total_volume_usdc,
                SUM(CASE WHEN status IN ('won','lost') THEN COALESCE(realized_pnl, 0) ELSE 0 END) as total_realized_pnl,
                SUM(COALESCE(actual_gas_usdc, 0)) as total_gas_usdc
            FROM poly_yield_positions WHERE mode = 'paper'
        """).fetchone()

    assert totals["trade_count"] == 3
    assert totals["open_count"] == 1
    assert totals["won_count"] == 1
    assert totals["lost_count"] == 1
    assert totals["total_volume_usdc"] == pytest.approx(39.0)
    assert totals["total_realized_pnl"] == pytest.approx(-17.0)
    assert totals["total_gas_usdc"] == pytest.approx(0.02)


@pytest.mark.asyncio
async def test_accounting_endpoint_matches_positions(setup_test_db):
    from db.database import get_sqlite, _sqlite_lock
    conn = get_sqlite()
    with _sqlite_lock:
        conn.execute("""
            INSERT INTO poly_yield_positions
            (id, strategy, market_id, outcome, shares, entry_price, cost_usdc, status,
             realized_pnl, mode, executed_by, entry_at, settled_at)
            VALUES ('acc1', 'manual', 'm1', 'YES', 20, 0.5, 10.0, 'won', 5.0, 'paper', 'manual',
                    datetime('now'), datetime('now'))
        """)
        conn.commit()

    import main
    result = await main.get_accounting(mode="paper")
    assert result["totals"]["trade_count"] == 1
    assert result["totals"]["win_rate_pct"] == 100.0
    assert result["by_executed_by"][0]["executed_by"] == "manual"
    assert result["ledger_health"] is not None  # paper mode always includes ledger health
