"""
Regression tests for the S9-S19 strategy implementations (previously 27-line
stubs that always returned no opportunities) and the S5/S3 grouping fixes —
proving each strategy's core differentiating logic actually fires against
synthetic market data, entirely offline (no live network access).
"""
import sqlite3
import pytest
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


@pytest.fixture(autouse=True)
def _reset_price_history():
    """S11/S12/S13/S16 share an in-process rolling price history keyed by
    strategy:market_id (strategies.base._price_history) — clear it between
    tests so they don't leak state into each other."""
    import strategies.base as base_mod
    base_mod._price_history.clear()
    yield
    base_mod._price_history.clear()


async def _mock_gas():
    return 0.001


def _mock_calc_price(price_map, default=0.5):
    async def _mock(token_id, amount_usdc, side="buy", http_client=None):
        return {"price": price_map.get(token_id, default), "slippage": 0.0}
    return _mock


# ---------- S9 Stablecoin Peg Arb ----------

@pytest.mark.asyncio
async def test_s9_only_fires_on_stablecoin_keyword_markets(monkeypatch):
    import strategies.s9_stablecoin_peg as mod
    from datetime import datetime, timezone, timedelta
    monkeypatch.setattr(mod.gas_tracker, "get_gas_cost_usdc", _mock_gas)
    monkeypatch.setattr(mod, "calculate_execution_price", _mock_calc_price({"tok_yes": 0.97}))

    near_future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()  # within default max_days_left=60
    strat = mod.StablecoinPegStrategy()
    unrelated = {
        "id": "m1", "question": "Will it rain tomorrow?", "endDate": near_future,
        "outcomes": '["Yes", "No"]', "outcomePrices": '["0.97", "0.03"]',
        "clobTokenIds": '["tok_yes", "tok_no"]',
    }
    stablecoin = {
        "id": "m2", "question": "Will USDC de-peg below $0.99 in March?", "endDate": near_future,
        "outcomes": '["Yes", "No"]', "outcomePrices": '["0.03", "0.97"]',
        "clobTokenIds": '["tok_yes2", "tok_yes"]',
    }
    opps = await strat.scan([unrelated, stablecoin], balance=1000.0, http_client=None)
    assert len(opps) == 1
    assert opps[0]["market_id"] == "m2"
    assert opps[0]["strategy"] == "s9_stablecoin_peg"


# ---------- S10 Oracle Discrepancy ----------

def test_s10_days_past_expiry_helper():
    from strategies.s10_oracle import _days_past_expiry
    from datetime import datetime, timezone, timedelta
    past = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    assert _days_past_expiry(past) == pytest.approx(3, abs=0.1)
    assert _days_past_expiry(future) is None
    assert _days_past_expiry(None) is None


@pytest.mark.asyncio
async def test_s10_fires_on_past_due_unsettled_market(monkeypatch):
    import strategies.s10_oracle as mod
    from datetime import datetime, timezone, timedelta
    monkeypatch.setattr(mod.gas_tracker, "get_gas_cost_usdc", _mock_gas)
    monkeypatch.setattr(mod, "calculate_execution_price", _mock_calc_price({"tok_yes": 0.985}))

    strat = mod.OracleStrategy()
    past_end = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    future_end = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    market_open_not_settled = {
        "id": "m1", "question": "Did the election happen?", "endDate": past_end, "closed": False,
        "outcomes": '["Yes", "No"]', "outcomePrices": '["0.985", "0.015"]',
        "clobTokenIds": '["tok_yes", "tok_no"]',
    }
    market_already_closed = dict(market_open_not_settled, id="m2", closed=True)
    market_not_yet_due = dict(market_open_not_settled, id="m3", closed=False, endDate=future_end)

    opps = await strat.scan([market_open_not_settled, market_already_closed, market_not_yet_due],
                             balance=1000.0, http_client=None)
    assert len(opps) == 1
    assert opps[0]["market_id"] == "m1"


# ---------- S11 Overreaction Scalp ----------

@pytest.mark.asyncio
async def test_s11_requires_warmup_then_fires_on_spike(monkeypatch):
    import strategies.s11_overreaction as mod
    monkeypatch.setattr(mod.gas_tracker, "get_gas_cost_usdc", _mock_gas)
    monkeypatch.setattr(mod, "calculate_execution_price", _mock_calc_price({"tok_yes": 0.35}))

    strat = mod.OverreactionStrategy()
    market_before = {
        "id": "m1", "question": "Will X happen?", "endDate": "2027-01-01T00:00:00Z",
        "outcomes": '["Yes", "No"]', "outcomePrices": '["0.50", "0.50"]',
        "clobTokenIds": '["tok_yes", "tok_no"]',
    }
    # First scan: only one sample recorded — no baseline to compare against yet (warm-up).
    opps1 = await strat.scan([market_before], balance=1000.0, http_client=None)
    assert opps1 == []

    market_after = dict(market_before, outcomePrices='["0.35", "0.65"]')
    opps2 = await strat.scan([market_after], balance=1000.0, http_client=None)
    assert len(opps2) == 1
    # YES dropped from 0.50 to 0.35 (-30%, past the 8% default threshold) -> reversion bet buys YES
    assert opps2[0]["outcome"] == "Yes"


# ---------- S12 Trend Momentum ----------

@pytest.mark.asyncio
async def test_s12_requires_sustained_trend(monkeypatch):
    import strategies.s12_momentum as mod
    monkeypatch.setattr(mod.gas_tracker, "get_gas_cost_usdc", _mock_gas)
    monkeypatch.setattr(mod, "calculate_execution_price", _mock_calc_price({"tok_yes": 0.65}))

    strat = mod.MomentumStrategy()
    base_market = {
        "id": "m1", "question": "Will X happen?", "endDate": "2027-01-01T00:00:00Z",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["tok_yes", "tok_no"]',
    }
    prices = ["0.50", "0.55", "0.60", "0.65"]  # min_samples defaults to 4
    opps = []
    for p in prices:
        m = dict(base_market, outcomePrices=f'["{p}", "{1 - float(p):.2f}"]')
        opps = await strat.scan([m], balance=1000.0, http_client=None)
    # Only once 4 monotonically increasing samples exist should this fire.
    assert len(opps) == 1
    assert opps[0]["outcome"] == "Yes"


# ---------- S13 Sentiment Tracker (market-derived proxy) ----------

@pytest.mark.asyncio
async def test_s13_composite_score_triggers_on_bullish_imbalance(monkeypatch):
    import strategies.s13_sentiment as mod
    monkeypatch.setattr(mod.gas_tracker, "get_gas_cost_usdc", _mock_gas)
    monkeypatch.setattr(mod, "calculate_execution_price", _mock_calc_price({"tok_yes": 0.55}))

    async def mock_imbalance(http_client, token_id, depth_levels=5):
        return 0.9  # strongly bullish resting book skew

    monkeypatch.setattr(mod, "_book_imbalance", mock_imbalance)

    strat = mod.SentimentStrategy()
    market = {
        "id": "m1", "question": "Will X happen?", "endDate": "2027-01-01T00:00:00Z",
        "outcomes": '["Yes", "No"]', "outcomePrices": '["0.55", "0.45"]',
        "clobTokenIds": '["tok_yes", "tok_no"]',
    }
    opps = await strat.scan([market], balance=1000.0, http_client=None)
    assert len(opps) == 1
    assert opps[0]["outcome"] == "Yes"
    assert opps[0]["exec_mode"] == "manual"  # advisory-only proxy by default


# ---------- S14 Macro Correlation (proxy, user-curated pairs) ----------

@pytest.mark.asyncio
async def test_s14_empty_without_curated_pairs(monkeypatch):
    import strategies.s14_macro_corr as mod
    strat = mod.MacroCorrStrategy()
    market_a = {
        "id": "a", "question": "Will the Fed cut rates by June?",
        "outcomes": '["Yes", "No"]', "outcomePrices": '["0.30", "0.70"]',
        "clobTokenIds": '["tok_a", "tok_a_no"]', "endDate": "2027-01-01T00:00:00Z",
    }
    market_b = {
        "id": "b", "question": "Will the ECB cut rates by June?",
        "outcomes": '["Yes", "No"]', "outcomePrices": '["0.45", "0.55"]',
        "clobTokenIds": '["tok_b", "tok_b_no"]', "endDate": "2027-01-01T00:00:00Z",
    }
    opps = await strat.scan([market_a, market_b], balance=1000.0, http_client=None)
    assert opps == []


@pytest.mark.asyncio
async def test_s14_fires_on_underpriced_side_of_curated_pair(monkeypatch):
    import strategies.s14_macro_corr as mod
    monkeypatch.setattr(mod.gas_tracker, "get_gas_cost_usdc", _mock_gas)
    monkeypatch.setattr(mod, "calculate_execution_price", _mock_calc_price({"tok_a": 0.30}))
    cfg.set("s14_macro_corr.correlation_pairs", '[["fed", "ecb"]]')

    strat = mod.MacroCorrStrategy()
    market_a = {
        "id": "a", "question": "Will the Fed cut rates by June?",
        "outcomes": '["Yes", "No"]', "outcomePrices": '["0.30", "0.70"]',
        "clobTokenIds": '["tok_a", "tok_a_no"]', "endDate": "2027-01-01T00:00:00Z",
    }
    market_b = {
        "id": "b", "question": "Will the ECB cut rates by June?",
        "outcomes": '["Yes", "No"]', "outcomePrices": '["0.45", "0.55"]',
        "clobTokenIds": '["tok_b", "tok_b_no"]', "endDate": "2027-01-01T00:00:00Z",
    }
    opps = await strat.scan([market_a, market_b], balance=1000.0, http_client=None)
    assert len(opps) == 1
    assert opps[0]["market_id"] == "a"  # the underpriced side of the curated pair


# ---------- S15 Theta Harvester ----------

@pytest.mark.asyncio
async def test_s15_requires_band_and_min_days(monkeypatch):
    import strategies.s15_theta as mod
    from datetime import datetime, timezone, timedelta
    monkeypatch.setattr(mod.gas_tracker, "get_gas_cost_usdc", _mock_gas)
    monkeypatch.setattr(mod, "calculate_execution_price", _mock_calc_price({"tok_no": 0.80}))

    strat = mod.ThetaHarvesterStrategy()
    far_future = (datetime.now(timezone.utc) + timedelta(days=60)).isoformat()
    near_future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()

    in_band_far = {
        "id": "m1", "question": "Will X happen?", "endDate": far_future,
        "outcomes": '["Yes", "No"]', "outcomePrices": '["0.20", "0.80"]',
        "clobTokenIds": '["tok_yes", "tok_no"]',
    }
    in_band_soon = dict(in_band_far, id="m2", endDate=near_future)  # fails min_days_left
    out_of_band = dict(in_band_far, id="m3", outcomePrices='["0.05", "0.95"]')  # too extreme (S1/S6/S19 territory)

    opps = await strat.scan([in_band_far, in_band_soon, out_of_band], balance=1000.0, http_client=None)
    assert len(opps) == 1
    assert opps[0]["market_id"] == "m1"
    assert opps[0]["outcome"] == "No"


# ---------- S16 Poll Drift (proxy) ----------

@pytest.mark.asyncio
async def test_s16_requires_election_keyword_and_trend(monkeypatch):
    import strategies.s16_poll_drift as mod
    monkeypatch.setattr(mod.gas_tracker, "get_gas_cost_usdc", _mock_gas)
    monkeypatch.setattr(mod, "calculate_execution_price", _mock_calc_price({"tok_yes": 0.65}))

    strat = mod.PollDriftStrategy()
    base_market = {
        "id": "m1", "question": "Will the incumbent win the presidential election?",
        "endDate": "2027-01-01T00:00:00Z",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["tok_yes", "tok_no"]',
    }
    non_election = dict(base_market, id="m2", question="Will it rain tomorrow?")

    prices = ["0.50", "0.55", "0.60", "0.65"]
    opps = []
    for p in prices:
        m1 = dict(base_market, outcomePrices=f'["{p}", "{1 - float(p):.2f}"]')
        m2 = dict(non_election, outcomePrices=f'["{p}", "{1 - float(p):.2f}"]')
        opps = await strat.scan([m1, m2], balance=1000.0, http_client=None)

    assert len(opps) == 1
    assert opps[0]["market_id"] == "m1"


# ---------- S17 Liquidity Sniper ----------

@pytest.mark.asyncio
async def test_s17_binary_arb_when_sum_below_one(monkeypatch):
    import strategies.s17_sniper as mod
    monkeypatch.setattr(mod.gas_tracker, "get_gas_cost_usdc", _mock_gas)
    monkeypatch.setattr(mod, "calculate_execution_price", _mock_calc_price({"tok_yes": 0.45, "tok_no": 0.45}))

    strat = mod.SniperStrategy()
    market = {
        "id": "m1", "question": "Will X happen?", "endDate": "2027-01-01T00:00:00Z",
        "outcomes": '["Yes", "No"]', "outcomePrices": '["0.45", "0.45"]',
        "clobTokenIds": '["tok_yes", "tok_no"]',
    }
    opps = await strat.scan([market], balance=1000.0, http_client=None)
    assert len(opps) == 1
    assert opps[0]["payoff_type"] == "guaranteed_arb"
    assert len(opps[0]["legs"]) == 2


# ---------- S18 Catalyst Straddle ----------

@pytest.mark.asyncio
async def test_s18_enters_near_5050_with_bounded_cost(monkeypatch):
    import strategies.s18_straddle as mod
    monkeypatch.setattr(mod.gas_tracker, "get_gas_cost_usdc", _mock_gas)
    monkeypatch.setattr(mod, "calculate_execution_price", _mock_calc_price({"tok_yes": 0.51, "tok_no": 0.51}))

    strat = mod.CatalystStraddleStrategy()
    market = {
        "id": "m1", "question": "Will X happen?", "endDate": "2027-01-01T00:00:00Z",
        "outcomes": '["Yes", "No"]', "outcomePrices": '["0.50", "0.51"]',
        "clobTokenIds": '["tok_yes", "tok_no"]',
    }
    opps = await strat.scan([market], balance=1000.0, http_client=None)
    assert len(opps) == 1
    assert opps[0]["payoff_type"] == "guaranteed_arb"
    assert opps[0]["profit_pct"] < 0  # guaranteed small carry cost if held to resolution, not a yield play


# ---------- S19 Longshot YES ----------

@pytest.mark.asyncio
async def test_s19_fires_on_ultra_longshot_within_cap(monkeypatch):
    import strategies.s19_longshot_yes as mod
    monkeypatch.setattr(mod, "calculate_execution_price", _mock_calc_price({"tok_yes": 0.01}, default=0.01))
    cfg.set("poly_yield.active_mode", "paper")

    strat = mod.LongshotYesStrategy()
    market = {
        "id": "m1", "question": "Will X happen?", "endDate": "2027-01-01T00:00:00Z",
        "outcomes": '["Yes", "No"]', "outcomePrices": '["0.01", "0.99"]',
        "clobTokenIds": '["tok_yes", "tok_no"]',
    }
    opps = await strat.scan([market], balance=1000.0, http_client=None)
    assert len(opps) == 1
    assert opps[0]["outcome"] == "Yes"
    assert opps[0]["annualized_apy"] is None  # deliberately not a yield play


@pytest.mark.asyncio
async def test_s19_respects_total_exposure_cap(monkeypatch):
    import strategies.s19_longshot_yes as mod
    monkeypatch.setattr(mod, "calculate_execution_price", _mock_calc_price({"tok_yes": 0.01}, default=0.01))
    cfg.set("poly_yield.active_mode", "paper")

    from db.database import get_sqlite, _sqlite_lock
    conn = get_sqlite()
    # Default cap: max_total_allocation_pct=0.03 of balance(1000) = $30. Leave only
    # $0.30 of room — below the $0.50 minimum stake, so no new signal should fire.
    with _sqlite_lock:
        conn.execute("""
            INSERT INTO poly_yield_positions (id, strategy, market_id, outcome, shares, entry_price,
                cost_usdc, status, mode)
            VALUES ('pos_existing', 's19_longshot_yes', 'existing_mkt', 'Yes', 100, 0.01, 29.7, 'open', 'paper')
        """)
        conn.commit()

    strat = mod.LongshotYesStrategy()
    market = {
        "id": "new_mkt", "question": "Will X happen?", "endDate": "2027-01-01T00:00:00Z",
        "outcomes": '["Yes", "No"]', "outcomePrices": '["0.01", "0.99"]',
        "clobTokenIds": '["tok_yes", "tok_no"]',
    }
    opps = await strat.scan([market], balance=1000.0, http_client=None)
    assert opps == []


# ---------- S5 Sub-Event Arb: question-text grouping fix ----------

def test_s5_prefix_temporal_grouping():
    from strategies.s5_sub_event import _find_parent_sub_groups
    markets = [
        {"id": "parent1", "question": "Will the Fed cut interest rates in 2026?"},
        {"id": "sub1", "question": "Will the Fed cut interest rates in Q1 2026?"},
        {"id": "sub2", "question": "Will the Fed cut interest rates in Q2 2026?"},
        {"id": "sub3", "question": "Will the Fed cut interest rates in Q3 2026?"},
        {"id": "unrelated", "question": "Will it rain tomorrow?"},
    ]
    groups = _find_parent_sub_groups(markets)
    assert len(groups) == 1
    parent, subs = groups[0]
    assert parent["id"] == "parent1"
    assert {s["id"] for s in subs} == {"sub1", "sub2", "sub3"}


@pytest.mark.asyncio
async def test_s5_scan_finds_parent_sub_mispricing(monkeypatch):
    import strategies.s5_sub_event as mod
    monkeypatch.setattr(mod.gas_tracker, "get_gas_cost_usdc", _mock_gas)
    monkeypatch.setattr(mod, "calculate_execution_price", _mock_calc_price({"tok_parent": 0.30}))

    strat = mod.SubEventArbitrageStrategy()
    parent = {
        "id": "parent1", "question": "Will the Fed cut interest rates in 2026?",
        "endDate": "2027-01-01T00:00:00Z",
        "outcomes": '["Yes", "No"]', "outcomePrices": '["0.30", "0.70"]',
        "clobTokenIds": '["tok_parent", "tok_parent_no"]',
    }
    subs = [
        {"id": f"sub{i}", "question": f"Will the Fed cut interest rates in Q{i} 2026?",
         "outcomes": '["Yes", "No"]', "outcomePrices": '["0.20", "0.80"]',
         "clobTokenIds": [f"tok_sub{i}", f"tok_sub{i}_no"]}
        for i in range(1, 4)
    ]
    # sub_sum = 0.60, parent_yes = 0.30 -> parent underpriced -> buy_parent_yes
    opps = await strat.scan([parent] + subs, balance=1000.0, http_client=None)
    assert len(opps) == 1
    assert opps[0]["action"] == "buy_parent_yes"
    assert opps[0]["market_id"] == "parent1"


# ---------- S3 Buy-All: neg-risk basket extension ----------

def test_s3_neg_risk_group_key_defensive_lookup():
    from strategies.s3_buy_all import _neg_risk_group_key
    assert _neg_risk_group_key({"negRiskMarketID": "evt1"}) == "evt1"
    assert _neg_risk_group_key({"negRiskMarketId": "evt2"}) == "evt2"
    assert _neg_risk_group_key({}) is None


@pytest.mark.asyncio
async def test_s3_neg_risk_basket_arb(monkeypatch):
    import strategies.s3_buy_all as mod
    monkeypatch.setattr(mod.gas_tracker, "get_gas_cost_usdc", _mock_gas)
    monkeypatch.setattr(mod, "calculate_execution_price",
                         _mock_calc_price({"tok_a": 0.30, "tok_b": 0.25, "tok_c": 0.20}))

    strat = mod.BuyAllArbitrageStrategy()
    markets = [
        {"id": "mkt_a", "question": "Will A win?", "negRiskMarketID": "evt1", "endDate": "2027-01-01T00:00:00Z",
         "outcomes": '["Yes", "No"]', "outcomePrices": '["0.30", "0.70"]', "clobTokenIds": '["tok_a", "tok_a_no"]'},
        {"id": "mkt_b", "question": "Will B win?", "negRiskMarketID": "evt1", "endDate": "2027-01-01T00:00:00Z",
         "outcomes": '["Yes", "No"]', "outcomePrices": '["0.25", "0.75"]', "clobTokenIds": '["tok_b", "tok_b_no"]'},
        {"id": "mkt_c", "question": "Will C win?", "negRiskMarketID": "evt1", "endDate": "2027-01-01T00:00:00Z",
         "outcomes": '["Yes", "No"]', "outcomePrices": '["0.20", "0.80"]', "clobTokenIds": '["tok_c", "tok_c_no"]'},
    ]
    opps = await strat.scan(markets, balance=1000.0, http_client=None)
    assert len(opps) == 1
    assert opps[0]["payoff_type"] == "guaranteed_arb"
    assert len(opps[0]["legs"]) == 3
    assert {leg["market_id"] for leg in opps[0]["legs"]} == {"mkt_a", "mkt_b", "mkt_c"}


@pytest.mark.asyncio
async def test_s3_native_multi_outcome_still_works(monkeypatch):
    """Regression guard for the max_slippage/is_fillable function-scoping bug
    found while adding the neg-risk pass: the native (single-market, >= 3
    outcomes) path must keep working exactly as before."""
    import strategies.s3_buy_all as mod
    monkeypatch.setattr(mod.gas_tracker, "get_gas_cost_usdc", _mock_gas)
    monkeypatch.setattr(mod, "calculate_execution_price",
                         _mock_calc_price({"tok_a": 0.30, "tok_b": 0.25, "tok_c": 0.20}))

    strat = mod.BuyAllArbitrageStrategy()
    market = {
        "id": "m1", "question": "Who will win?", "endDate": "2027-01-01T00:00:00Z",
        "outcomes": '["A", "B", "C"]', "outcomePrices": '["0.30", "0.25", "0.20"]',
        "clobTokenIds": '["tok_a", "tok_b", "tok_c"]',
    }
    opps = await strat.scan([market], balance=1000.0, http_client=None)
    assert len(opps) == 1
    assert opps[0]["market_id"] == "m1"
