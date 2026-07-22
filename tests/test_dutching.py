"""
Unit Tests for Dutching Bot Strategy & Multi-LLM Provider Engine
"""
import pytest
import asyncio
import httpx
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
            {"groupItemTitle": "Candidate A", "outcomes": '["Yes", "No"]',
             "outcomePrices": '["0.45", "0.55"]', "clobTokenIds": '["tok_a", "tok_a_no"]'},
            {"groupItemTitle": "Candidate B", "outcomes": '["Yes", "No"]',
             "outcomePrices": '["0.30", "0.70"]', "clobTokenIds": '["tok_b", "tok_b_no"]'},
            {"groupItemTitle": "Candidate C", "outcomes": '["Yes", "No"]',
             "outcomePrices": '["0.10", "0.90"]', "clobTokenIds": '["tok_c", "tok_c_no"]'},
            {"groupItemTitle": "Candidate D", "outcomes": '["Yes", "No"]',
             "outcomePrices": '["0.05", "0.95"]', "clobTokenIds": '["tok_d", "tok_d_no"]'},
        ]
    }

    class MockResponse:
        def json(self):
            return [mock_event]

    async def mock_get(url, params=None):
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
