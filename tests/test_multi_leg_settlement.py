"""
Unit Tests for Multi-Leg Position Settlement (S3 Buy-All, S5 Sub-Event, S20 Dutching)

Regression coverage for a critical pre-existing bug: guaranteed-arbitrage baskets
(S3/S5) always settled as a total loss because settlement only understood
single-outcome positions. Also covers Dutching-style conditional multi-leg bets,
where only a subset of outcomes is covered and each leg can live on its own
independent market/condition.
"""
import json
import pytest
from strategies.settlement import poly_yield_settlement


@pytest.mark.asyncio
async def test_guaranteed_arb_settles_won_when_shared_market_resolves(monkeypatch):
    """S3-style basket: all legs share ONE market. Regardless of which specific
    outcome wins, the basket is guaranteed to pay out — this must settle 'won'."""
    shared_market = {
        "id": "mkt_shared",
        "closed": True,
        "outcomes": '["Candidate A", "Candidate B", "Candidate C"]',
        "outcomePrices": '["1.0", "0.0", "0.0"]',
    }

    async def mock_fetch_market(market_id):
        return shared_market if market_id == "mkt_shared" else None

    monkeypatch.setattr(poly_yield_settlement, "_fetch_market", mock_fetch_market)

    pos = {
        "id": "pos_arb_1",
        "strategy": "s3_buy_all",
        "payoff_type": "guaranteed_arb",
        "shares": 50.0,
        "cost_usdc": 45.0,
        "actual_gas_usdc": 0.5,
        "legs": json.dumps([
            {"outcome": "Candidate A", "market_id": "mkt_shared"},
            {"outcome": "Candidate B", "market_id": "mkt_shared"},
            {"outcome": "Candidate C", "market_id": "mkt_shared"},
        ]),
    }

    pnl, outcome, status = await poly_yield_settlement._compute_pnl(pos, None)
    assert status == "won"
    assert pnl == pytest.approx(50.0 - 45.0 - 0.5)


@pytest.mark.asyncio
async def test_guaranteed_arb_stays_open_until_resolved(monkeypatch):
    async def mock_fetch_market(market_id):
        return {"id": market_id, "closed": False}

    monkeypatch.setattr(poly_yield_settlement, "_fetch_market", mock_fetch_market)

    pos = {
        "id": "pos_arb_2",
        "strategy": "s3_buy_all",
        "payoff_type": "guaranteed_arb",
        "shares": 50.0,
        "cost_usdc": 45.0,
        "actual_gas_usdc": 0.5,
        "legs": json.dumps([
            {"outcome": "Candidate A", "market_id": "mkt_shared"},
            {"outcome": "Candidate B", "market_id": "mkt_shared"},
        ]),
    }

    pnl, outcome, status = await poly_yield_settlement._compute_pnl(pos, None)
    assert status == "open"


@pytest.mark.asyncio
async def test_conditional_multi_leg_wins_when_one_leg_market_resolves_yes(monkeypatch):
    """Dutching-style: each leg is its OWN binary Yes/No sub-market. Wins as soon
    as one covered leg's own market resolves Yes, even if the others are still
    pending."""
    markets = {
        "mkt_trump": {"id": "mkt_trump", "closed": True, "outcomes": '["Yes", "No"]', "outcomePrices": '["1.0", "0.0"]'},
        "mkt_harris": {"id": "mkt_harris", "closed": False},  # still pending
    }

    async def mock_fetch_market(market_id):
        return markets.get(market_id)

    monkeypatch.setattr(poly_yield_settlement, "_fetch_market", mock_fetch_market)

    pos = {
        "id": "pos_dutch_1",
        "strategy": "s20_dutching",
        "payoff_type": "conditional_multi_leg",
        "shares": 20.0,
        "cost_usdc": 18.0,
        "actual_gas_usdc": 0.1,
        "legs": json.dumps([
            {"outcome": "Trump", "market_id": "mkt_trump"},
            {"outcome": "Harris", "market_id": "mkt_harris"},
        ]),
    }

    pnl, outcome, status = await poly_yield_settlement._compute_pnl(pos, None)
    assert status == "won"
    assert pnl == pytest.approx(20.0 - 18.0 - 0.1)


@pytest.mark.asyncio
async def test_conditional_multi_leg_loses_when_all_legs_resolve_no(monkeypatch):
    markets = {
        "mkt_trump": {"id": "mkt_trump", "closed": True, "outcomes": '["Yes", "No"]', "outcomePrices": '["0.0", "1.0"]'},
        "mkt_harris": {"id": "mkt_harris", "closed": True, "outcomes": '["Yes", "No"]', "outcomePrices": '["0.0", "1.0"]'},
    }

    async def mock_fetch_market(market_id):
        return markets.get(market_id)

    monkeypatch.setattr(poly_yield_settlement, "_fetch_market", mock_fetch_market)

    pos = {
        "id": "pos_dutch_2",
        "strategy": "s20_dutching",
        "payoff_type": "conditional_multi_leg",
        "shares": 20.0,
        "cost_usdc": 18.0,
        "actual_gas_usdc": 0.1,
        "legs": json.dumps([
            {"outcome": "Trump", "market_id": "mkt_trump"},
            {"outcome": "Harris", "market_id": "mkt_harris"},
        ]),
    }

    pnl, outcome, status = await poly_yield_settlement._compute_pnl(pos, None)
    assert status == "lost"
    assert pnl == pytest.approx(-18.0 - 0.1)


@pytest.mark.asyncio
async def test_conditional_multi_leg_stays_open_when_none_resolved_yet(monkeypatch):
    async def mock_fetch_market(market_id):
        return {"id": market_id, "closed": False}

    monkeypatch.setattr(poly_yield_settlement, "_fetch_market", mock_fetch_market)

    pos = {
        "id": "pos_dutch_3",
        "strategy": "s20_dutching",
        "payoff_type": "conditional_multi_leg",
        "shares": 20.0,
        "cost_usdc": 18.0,
        "actual_gas_usdc": 0.1,
        "legs": json.dumps([
            {"outcome": "Trump", "market_id": "mkt_trump"},
            {"outcome": "Harris", "market_id": "mkt_harris"},
        ]),
    }

    pnl, outcome, status = await poly_yield_settlement._compute_pnl(pos, None)
    assert status == "open"


@pytest.mark.asyncio
async def test_directional_position_unaffected_by_multi_leg_logic(monkeypatch):
    """Plain single-outcome positions (payoff_type defaults to 'directional') must
    keep using the existing market-level winner-matching logic untouched."""
    market = {"outcomes": '["Yes", "No"]', "outcomePrices": '["0.01", "0.99"]'}
    pos = {
        "id": "pos_dir_1",
        "strategy": "s1_novelty",
        "outcome": "no",
        "shares": 10.0,
        "cost_usdc": 9.0,
        "actual_gas_usdc": 0.0,
    }
    pnl, outcome, status = await poly_yield_settlement._compute_pnl(pos, market)
    assert status == "won"
    assert pnl == pytest.approx(10.0 - 9.0)
