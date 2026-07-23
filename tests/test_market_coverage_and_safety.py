"""
Regression tests for:
- strategies.base.fetch_all_paginated (the shared full-catalog pagination
  utility replacing the old 500-market / 100-event hard caps)
- S20 Dutching's unfillable-leg flagging (still surfaces the opportunity for
  visibility, but tags it so the UI can show it and the auto-exec loop won't
  keep retrying a known-bad execution)
- PolyYieldEngine._select_auto_opportunities (the auto-exec eligibility filter)
"""
import sqlite3
import pytest


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


class _FakeResp:
    def __init__(self, status_code, data):
        self.status_code = status_code
        self._data = data

    def json(self):
        return self._data


class _FakeClient:
    """Maps a requested `offset` param to a canned response, and records the
    offsets it was called with (in order)."""
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def get(self, url, params=None):
        offset = (params or {}).get("offset", 0)
        self.calls.append(offset)
        return self.pages.get(offset, _FakeResp(200, []))


# ---------- fetch_all_paginated ----------

@pytest.mark.asyncio
async def test_fetch_all_paginated_walks_multiple_pages():
    from strategies.base import fetch_all_paginated
    client = _FakeClient({
        0: _FakeResp(200, [{"id": 1}, {"id": 2}]),
        2: _FakeResp(200, [{"id": 3}, {"id": 4}]),
        4: _FakeResp(200, [{"id": 5}]),  # short page -> signals the last page
    })
    items = await fetch_all_paginated(client, "https://example.test/items", {}, page_size=2, delay_s=0)
    assert [i["id"] for i in items] == [1, 2, 3, 4, 5]
    assert client.calls == [0, 2, 4]


@pytest.mark.asyncio
async def test_fetch_all_paginated_stops_on_error_but_keeps_partial_results():
    from strategies.base import fetch_all_paginated
    client = _FakeClient({
        0: _FakeResp(200, [{"id": 1}, {"id": 2}]),
        2: _FakeResp(500, None),
    })
    items = await fetch_all_paginated(client, "https://example.test/items", {}, page_size=2, delay_s=0)
    # A transient failure mid-pagination loses only the remainder, not what was
    # already gathered from earlier successful pages.
    assert [i["id"] for i in items] == [1, 2]


@pytest.mark.asyncio
async def test_fetch_all_paginated_respects_max_pages_safety_valve():
    from strategies.base import fetch_all_paginated

    class _InfiniteClient:
        def __init__(self):
            self.calls = 0

        async def get(self, url, params=None):
            self.calls += 1
            offset = (params or {}).get("offset", 0)
            return _FakeResp(200, [{"id": offset}, {"id": offset + 1}])  # always a full page

    client = _InfiniteClient()
    items = await fetch_all_paginated(client, "https://example.test/items", {}, page_size=2, max_pages=3, delay_s=0)
    assert client.calls == 3
    assert len(items) == 6


@pytest.mark.asyncio
async def test_fetch_all_paginated_never_raises_on_request_exception():
    from strategies.base import fetch_all_paginated

    class _BrokenClient:
        async def get(self, url, params=None):
            raise ConnectionError("simulated network failure")

    items = await fetch_all_paginated(_BrokenClient(), "https://example.test/items", {}, delay_s=0)
    assert items == []


# ---------- S20 Dutching: unfillable-leg flagging ----------

@pytest.mark.asyncio
async def test_s20_flags_unfillable_leg_but_still_shows_opportunity(monkeypatch):
    import strategies.s20_dutching as mod

    event = {
        "id": "evt1", "title": "Test event", "slug": "test-event", "endDate": "2027-01-01T00:00:00Z",
        "markets": [
            {"id": "m_a", "groupItemTitle": "A", "outcomes": '["Yes", "No"]',
             "outcomePrices": '["0.40", "0.60"]', "clobTokenIds": '["tok_a", "tok_a_no"]'},
            {"id": "m_b", "groupItemTitle": "B", "outcomes": '["Yes", "No"]',
             "outcomePrices": '["0.30", "0.70"]', "clobTokenIds": '["tok_b", "tok_b_no"]'},
            {"id": "m_c", "groupItemTitle": "C", "outcomes": '["Yes", "No"]',
             "outcomePrices": '["0.10", "0.90"]', "clobTokenIds": '["tok_c", "tok_c_no"]'},
        ],
    }

    class _Resp:
        status_code = 200
        def json(self):
            return [event]

    class _Client:
        async def get(self, url, params=None):
            return _Resp()

    async def mock_calc_price(token_id, amount_usdc, side="buy", http_client=None):
        if token_id == "tok_c":
            # Simulate a thin book: huge slippage fails the fillability check.
            return {"price": 0.10, "slippage": 99.0, "warning": "Insufficient liquidity"}
        return {"price": {"tok_a": 0.40, "tok_b": 0.30}.get(token_id, 0.5), "slippage": 0.0}

    monkeypatch.setattr(mod, "calculate_execution_price", mock_calc_price)

    async def mock_gas():
        return 0.001
    monkeypatch.setattr(mod.gas_tracker, "get_gas_cost_usdc", mock_gas)

    strat = mod.DutchingStrategy()
    opps = await strat.scan([], balance=1000.0, http_client=_Client())

    # Still surfaced (not silently dropped) ...
    assert len(opps) == 1
    # ... but clearly flagged, with a human-readable reason naming the bad leg.
    assert opps[0]["fillable"] is False
    assert "C" in opps[0]["unfillable_reason"]
    assert "Insufficient liquidity" in opps[0]["unfillable_reason"]


@pytest.mark.asyncio
async def test_s20_all_legs_fillable_leaves_opportunity_unflagged(monkeypatch):
    import strategies.s20_dutching as mod

    event = {
        "id": "evt1", "title": "Test event", "slug": "test-event", "endDate": "2027-01-01T00:00:00Z",
        "markets": [
            {"id": "m_a", "groupItemTitle": "A", "outcomes": '["Yes", "No"]',
             "outcomePrices": '["0.40", "0.60"]', "clobTokenIds": '["tok_a", "tok_a_no"]'},
            {"id": "m_b", "groupItemTitle": "B", "outcomes": '["Yes", "No"]',
             "outcomePrices": '["0.30", "0.70"]', "clobTokenIds": '["tok_b", "tok_b_no"]'},
            {"id": "m_c", "groupItemTitle": "C", "outcomes": '["Yes", "No"]',
             "outcomePrices": '["0.10", "0.90"]', "clobTokenIds": '["tok_c", "tok_c_no"]'},
        ],
    }

    class _Resp:
        status_code = 200
        def json(self):
            return [event]

    class _Client:
        async def get(self, url, params=None):
            return _Resp()

    async def mock_calc_price(token_id, amount_usdc, side="buy", http_client=None):
        return {"price": {"tok_a": 0.40, "tok_b": 0.30, "tok_c": 0.10}.get(token_id, 0.5), "slippage": 0.0}

    monkeypatch.setattr(mod, "calculate_execution_price", mock_calc_price)

    async def mock_gas():
        return 0.001
    monkeypatch.setattr(mod.gas_tracker, "get_gas_cost_usdc", mock_gas)

    strat = mod.DutchingStrategy()
    opps = await strat.scan([], balance=1000.0, http_client=_Client())

    assert len(opps) == 1
    assert opps[0]["fillable"] is True
    assert opps[0]["unfillable_reason"] is None


# ---------- Engine: auto-exec eligibility filter ----------

def test_select_auto_opportunities_skips_unfillable_and_non_auto():
    from strategies.engine import PolyYieldEngine
    all_opps = [
        {"id": "opp1", "exec_mode": "auto", "fillable": True, "annualized_apy": 10},
        {"id": "opp2", "exec_mode": "auto", "fillable": False, "annualized_apy": 50},  # known unfillable — must skip
        {"id": "opp3", "exec_mode": "semi", "fillable": True, "annualized_apy": 100},  # not auto mode
        {"id": "opp4", "exec_mode": "auto", "annualized_apy": 5},  # fillable unset -> defaults True
    ]
    selected = PolyYieldEngine._select_auto_opportunities(all_opps, killswitch=False)
    ids = {o["id"] for o in selected}
    assert ids == {"opp1", "opp4"}


def test_select_auto_opportunities_respects_killswitch():
    from strategies.engine import PolyYieldEngine
    all_opps = [{"id": "opp1", "exec_mode": "auto", "fillable": True, "annualized_apy": 10}]
    assert PolyYieldEngine._select_auto_opportunities(all_opps, killswitch=True) == []


@pytest.mark.asyncio
async def test_upsert_opportunity_persists_fillable_and_reason(setup_test_db):
    from db.database import get_sqlite, _sqlite_lock
    from strategies.engine import poly_yield_engine

    opp = {
        "id": "opp_test1", "strategy": "s20_dutching", "market_id": "evt1", "market_title": "Test",
        "outcome": "Top-3 Dutch: A, B, C", "entry_price": 0.8, "suggested_usdc": 50.0,
        "exec_mode": "auto", "fillable": False, "unfillable_reason": "C: Insufficient liquidity",
        "legs": [{"outcome": "A"}], "instructions": [],
    }
    await poly_yield_engine._upsert_opportunity(opp)

    conn = get_sqlite()
    with _sqlite_lock:
        row = conn.execute(
            "SELECT fillable, unfillable_reason FROM poly_yield_opportunities WHERE id = ?", ["opp_test1"]
        ).fetchone()
    assert row["fillable"] == 0
    assert row["unfillable_reason"] == "C: Insufficient liquidity"
