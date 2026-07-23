"""
Regression tests for the manual multi-leg/Dutching-style basket trade endpoint —
the generic tool letting a human build an ad-hoc N-leg basket (proportional
sizing, uniform payout if any covered leg wins) from candidates they pick
themselves, not just opportunities the bot already scanned.
"""
import sqlite3
import pytest
from fastapi import HTTPException
from db.config import cfg


@pytest.fixture(autouse=True)
def setup_test_db():
    import db.database
    from db.database import init_db
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.database._sqlite_conn = conn
    init_db()
    yield conn
    conn.close()
    db.database._sqlite_conn = None


def _mock_calc_price(price_map, default=0.5):
    async def _mock(token_id, amount_usdc, side="buy", http_client=None):
        return {"price": price_map.get(token_id, default), "slippage": 0.0}
    return _mock


async def _mock_gas():
    return 0.001


@pytest.mark.asyncio
async def test_manual_basket_trade_executes_dutching_style(monkeypatch):
    import strategies.base as base_mod
    import strategies.engine as engine_mod
    import main
    from db.database import get_sqlite, _sqlite_lock

    monkeypatch.setattr(base_mod, "calculate_execution_price",
                         _mock_calc_price({"tok_a": 0.40, "tok_b": 0.30, "tok_c": 0.10}))
    monkeypatch.setattr(engine_mod.gas_tracker, "get_gas_cost_usdc", _mock_gas)

    cfg.set("poly_yield.active_mode", "paper")
    cfg.set("portfolio.paper_balance", "1000.0")

    args = main.ManualBasketTradeArgs(
        legs=[
            main.ManualBasketLegArgs(market_id="m_a", token_id="tok_a", outcome="A", price=0.40),
            main.ManualBasketLegArgs(market_id="m_b", token_id="tok_b", outcome="B", price=0.30),
            main.ManualBasketLegArgs(market_id="m_c", token_id="tok_c", outcome="C", price=0.10),
        ],
        total_stake_usdc=20.0,
        market_title="My Custom Dutching Basket",
    )
    result = await main.place_manual_basket_trade(args)
    assert result["success"] is True
    assert result["mode"] == "paper"
    assert result["position_id"]

    conn = get_sqlite()
    with _sqlite_lock:
        pos = conn.execute(
            "SELECT * FROM poly_yield_positions WHERE id = ?", [result["position_id"]]
        ).fetchone()
    assert pos is not None
    assert pos["strategy"] == "manual"
    assert pos["payoff_type"] == "conditional_multi_leg"  # default (safe) assumption
    assert pos["mode"] == "paper"
    assert pos["executed_by"] == "manual"

    import json
    legs = json.loads(pos["legs"])
    assert len(legs) == 3
    assert {l["market_id"] for l in legs} == {"m_a", "m_b", "m_c"}
    # Proportional sizing: stake_usdc / price (i.e. shares bought) must be
    # (approximately) equal across legs — that's the entire point of the
    # Dutching math (equal payout regardless of which covered leg wins).
    implied_shares = [l["stake_usdc"] / l["price"] for l in legs]
    assert max(implied_shares) - min(implied_shares) < 0.01


@pytest.mark.asyncio
async def test_manual_basket_trade_guaranteed_arb_flag(monkeypatch):
    import strategies.base as base_mod
    import strategies.engine as engine_mod
    import main

    monkeypatch.setattr(base_mod, "calculate_execution_price",
                         _mock_calc_price({"tok_a": 0.45, "tok_b": 0.45}))
    monkeypatch.setattr(engine_mod.gas_tracker, "get_gas_cost_usdc", _mock_gas)
    cfg.set("poly_yield.active_mode", "paper")
    cfg.set("portfolio.paper_balance", "1000.0")

    args = main.ManualBasketTradeArgs(
        legs=[
            main.ManualBasketLegArgs(market_id="m1", token_id="tok_a", outcome="Yes", price=0.45),
            main.ManualBasketLegArgs(market_id="m1", token_id="tok_b", outcome="No", price=0.45),
        ],
        total_stake_usdc=10.0,
        guaranteed_arb=True,
    )
    result = await main.place_manual_basket_trade(args)
    assert result["success"] is True

    from db.database import get_sqlite, _sqlite_lock
    conn = get_sqlite()
    with _sqlite_lock:
        pos = conn.execute(
            "SELECT payoff_type FROM poly_yield_positions WHERE id = ?", [result["position_id"]]
        ).fetchone()
    assert pos["payoff_type"] == "guaranteed_arb"


@pytest.mark.asyncio
async def test_manual_basket_trade_rejects_single_leg():
    import main
    args = main.ManualBasketTradeArgs(
        legs=[main.ManualBasketLegArgs(market_id="m1", token_id="tok_a", outcome="Yes", price=0.5)],
        total_stake_usdc=10.0,
    )
    with pytest.raises(HTTPException) as exc_info:
        await main.place_manual_basket_trade(args)
    assert exc_info.value.status_code == 400
    assert "at least 2 legs" in exc_info.value.detail


@pytest.mark.asyncio
async def test_manual_basket_trade_rejects_bad_price():
    import main
    args = main.ManualBasketTradeArgs(
        legs=[
            main.ManualBasketLegArgs(market_id="m1", token_id="tok_a", outcome="Yes", price=1.5),
            main.ManualBasketLegArgs(market_id="m2", token_id="tok_b", outcome="No", price=0.3),
        ],
        total_stake_usdc=10.0,
    )
    with pytest.raises(HTTPException) as exc_info:
        await main.place_manual_basket_trade(args)
    assert exc_info.value.status_code == 400
    assert "between 0 and 1" in exc_info.value.detail


@pytest.mark.asyncio
async def test_manual_basket_trade_rejects_missing_token_id():
    import main
    args = main.ManualBasketTradeArgs(
        legs=[
            main.ManualBasketLegArgs(market_id="m1", token_id="", outcome="Yes", price=0.4),
            main.ManualBasketLegArgs(market_id="m2", token_id="tok_b", outcome="No", price=0.3),
        ],
        total_stake_usdc=10.0,
    )
    with pytest.raises(HTTPException) as exc_info:
        await main.place_manual_basket_trade(args)
    assert exc_info.value.status_code == 400
    assert "token_id" in exc_info.value.detail


@pytest.mark.asyncio
async def test_manual_basket_trade_rejects_non_positive_stake():
    import main
    args = main.ManualBasketTradeArgs(
        legs=[
            main.ManualBasketLegArgs(market_id="m1", token_id="tok_a", outcome="Yes", price=0.4),
            main.ManualBasketLegArgs(market_id="m2", token_id="tok_b", outcome="No", price=0.3),
        ],
        total_stake_usdc=0,
    )
    with pytest.raises(HTTPException) as exc_info:
        await main.place_manual_basket_trade(args)
    assert exc_info.value.status_code == 400
    assert "positive" in exc_info.value.detail
