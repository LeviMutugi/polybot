"""
Unit Tests for Dutching Bot Strategy & Multi-LLM Provider Engine
"""
import pytest
import asyncio
import httpx
from db.config import cfg
from strategies.s20_dutching import DutchingStrategy
from services.llm_provider import LLMProviderService

@pytest.fixture(autouse=True)
def setup_test_db():
    import db.database
    import sqlite3
    from db.database import init_db

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

@pytest.mark.asyncio
async def test_dutching_strategy_scan(monkeypatch):
    strategy = DutchingStrategy()

    # S20 scans Gamma "events" directly (multi-outcome markets are represented as an
    # event with several sub-markets), not the flat `markets` list the engine passes in.
    mock_event = {
        "id": "evt_pres_2028",
        "title": "Who will win the 2028 US Presidential Election?",
        "slug": "presidential-election-2028",
        "endDate": "2028-11-05T00:00:00Z",
        "markets": [
            {"id": "mkt_a", "groupItemTitle": "Candidate A", "outcomes": '["Yes", "No"]',
             "outcomePrices": '["0.45", "0.55"]', "clobTokenIds": '["tok_a", "tok_a_no"]'},
            {"id": "mkt_b", "groupItemTitle": "Candidate B", "outcomes": '["Yes", "No"]',
             "outcomePrices": '["0.30", "0.70"]', "clobTokenIds": '["tok_b", "tok_b_no"]'},
            {"id": "mkt_c", "groupItemTitle": "Candidate C", "outcomes": '["Yes", "No"]',
             "outcomePrices": '["0.10", "0.90"]', "clobTokenIds": '["tok_c", "tok_c_no"]'},
            {"id": "mkt_d", "groupItemTitle": "Candidate D", "outcomes": '["Yes", "No"]',
             "outcomePrices": '["0.05", "0.95"]', "clobTokenIds": '["tok_d", "tok_d_no"]'},
        ]
    }

    class MockResponse:
        status_code = 200
        def json(self):
            return [mock_event]

    async def mock_get(url, params=None):
        # Second page (offset=100) is empty — signals pagination is done.
        if (params or {}).get("offset", 0) > 0:
            class EmptyResponse:
                status_code = 200
                def json(self):
                    return []
            return EmptyResponse()
        return MockResponse()

    async def mock_calc_price(token_id, amount_usdc, side="buy", http_client=None):
        prices = {"tok_a": 0.45, "tok_b": 0.30, "tok_c": 0.10, "tok_d": 0.05}
        p = prices.get(token_id, 0.5)
        return {"price": p, "slippage": 0.0, "status": "ok"}

    import strategies.s20_dutching as dutching_mod
    monkeypatch.setattr(dutching_mod, "calculate_execution_price", mock_calc_price)

    async def mock_gas(): return 0.01
    monkeypatch.setattr(dutching_mod.gas_tracker, "get_gas_cost_usdc", mock_gas)

    async with httpx.AsyncClient() as client:
        monkeypatch.setattr(client, "get", mock_get)
        opps = await strategy.scan([], balance=1000.0, http_client=client)

        # Check opportunity detection
        assert len(opps) == 1
        opp = opps[0]
        assert opp["strategy"] == "s20_dutching"
        assert opp["p_sum"] == 0.85
        assert len(opp["legs"]) == 3
        assert opp["profit_pct"] > 0
        assert opp["payoff_type"] == "conditional_multi_leg"
        # Each leg must carry its OWN sub-market id — settlement/redemption need this
        # since each candidate's market is an independent binary Yes/No condition.
        for leg in opp["legs"]:
            assert leg["market_id"] in ("mkt_a", "mkt_b", "mkt_c", "mkt_d")

@pytest.mark.asyncio
async def test_llm_provider_fallback_response():
    llm_service = LLMProviderService()
    res = await llm_service.evaluate_market(
        provider_key="unsupported_provider",
        market_question="Test question",
        market_description="Test desc",
        candidates=[{"name": "A", "price": 0.5}, {"name": "B", "price": 0.4}],
        top_set_names=["A", "B"]
    )
    assert res["status"] == "fallback"
    assert res["p_tail_risk"] > 0
    assert "Unsupported provider" in res["error"]

def test_llm_json_response_parsing():
    llm_service = LLMProviderService()
    sample_json = '''
    ```json
    {
        "p_model_top_set": 0.94,
        "p_tail_risk": 0.06,
        "confidence": 0.88,
        "tail_risk_assessment": "low",
        "rationale": "Top candidates have high polling margins."
    }
    ```
    '''
    parsed = llm_service._parse_json_response(sample_json)
    assert parsed["p_model_top_set"] == 0.94
    assert parsed["p_tail_risk"] == 0.06
    assert parsed["confidence"] == 0.88
    assert parsed["status"] == "success"


@pytest.mark.asyncio
async def test_dutching_execute_respects_exec_mode_and_debits_paper_wallet(monkeypatch):
    """Quick Trade must route through the same hardened engine.execute_opportunity()
    path as every other strategy: blocked while s20_dutching.exec_mode is 'manual'
    (the default), and once switched to semi/auto it debits the paper wallet,
    records a real position with legs/payoff_type, and links a dutching_trades
    metadata row to that position for the arena leaderboard."""
    import strategies.base as base_mod
    import strategies.engine as engine_mod
    from fastapi import HTTPException
    from strategies.engine import poly_yield_engine
    from db.database import get_sqlite, _sqlite_lock
    import main

    async def mock_calc_price(token_id, amount_usdc, side="buy", http_client=None):
        prices = {"tok_trump": 0.45, "tok_harris": 0.40}
        return {"price": prices.get(token_id, 0.5), "slippage": 0.0}

    monkeypatch.setattr(base_mod, "calculate_execution_price", mock_calc_price)

    # Deterministic simulated gas fee — pin it so the balance-conservation assertion
    # below isn't at the mercy of live gas/MATIC price lookups.
    async def mock_gas(): return 0.01
    monkeypatch.setattr(engine_mod.gas_tracker, "get_gas_cost_usdc", mock_gas)

    opp = {
        "id": "opp_dutch_test1",
        "strategy": "s20_dutching",
        "market_id": "evt_test",
        "market_title": "Test Dutching Market",
        "market_type": "Multi-outcome",
        "outcome": "Top-2 Dutch: Trump, Harris",
        "entry_price": 0.85,
        "suggested_usdc": 20.0,
        "profit_pct": 8.0,
        "exec_mode": "manual",
        "payoff_type": "conditional_multi_leg",
        "legs": [
            {"outcome": "Trump", "market_id": "mkt_trump", "token_id": "tok_trump",
             "price": 0.45, "fill_price": 0.45, "shares": 22.0, "stake_usdc": 10.0},
            {"outcome": "Harris", "market_id": "mkt_harris", "token_id": "tok_harris",
             "price": 0.40, "fill_price": 0.40, "shares": 22.0, "stake_usdc": 10.0},
        ],
        "status": "open",
    }
    await poly_yield_engine._upsert_opportunity(opp)

    args = main.DutchingExecuteArgs(opportunity_id="opp_dutch_test1")

    # Default exec_mode is 'manual' — Quick Trade must be blocked exactly like clicking
    # "Execute" on any other strategy's opportunity card.
    with pytest.raises(HTTPException) as exc_info:
        await main.execute_dutching_trade(args)
    assert "MANUAL mode" in str(exc_info.value.detail)

    # Switch to semi — Quick Trade should now succeed through the full engine pipeline
    cfg.set("s20_dutching.exec_mode", "semi")
    balance_before = float(cfg.get("portfolio.paper_balance"))

    result = await main.execute_dutching_trade(args)
    assert result["status"] == "executed"
    assert result["position_id"]
    assert result["stake_usdc"] > 0
    # Basket execution debits the stake plus a simulated gas fee per leg (2 legs here)
    assert result["new_balance"] == pytest.approx(balance_before - result["stake_usdc"] - 0.02)

    conn = get_sqlite()
    with _sqlite_lock:
        pos = conn.execute(
            "SELECT * FROM poly_yield_positions WHERE id = ?", [result["position_id"]]
        ).fetchone()
        trade = conn.execute(
            "SELECT * FROM dutching_trades WHERE position_id = ?", [result["position_id"]]
        ).fetchone()

    assert pos is not None
    assert pos["strategy"] == "s20_dutching"
    assert pos["payoff_type"] == "conditional_multi_leg"
    assert pos["legs"] is not None
    assert pos["mode"] == "paper"
    assert trade is not None
    # Manual/consensus bucket instance id is mode-scoped: inst_manual_{mode}
    assert trade["instance_id"] == "inst_manual_paper"
