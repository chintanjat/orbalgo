from pathlib import Path

import pytest

from app.config import Settings
from app.engine import ExecutionEngine
from app.models import OptionCandidate, TradingViewEvent
from app.store import Store


class FakeDhan:
    async def ltp(self, ids):
        return {str(x): 60.0 for x in ids}


class FakeSelector:
    async def select(self, event):
        from datetime import date
        return OptionCandidate(
            underlying=event.underlying, side=event.side.value, expiry=date(2026, 9, 22), strike=25300,
            security_id="101", last_price=100, bid=99, ask=100, bid_qty=1000, ask_qty=1000,
            volume=10000, lot_size=65,
        )


def tv_event(event_id="e1", event_type="ENTRY", side="CE", reason="OR_EMA"):
    import time
    return TradingViewEvent.model_validate({
        "schema": "or15.v1", "event": event_type, "event_id": event_id, "trade_id": "t1",
        "underlying": "NIFTY", "bar_close_ms": int(time.time()*1000), "side": side, "reason": reason,
        "spot": 25347, "or_high": 25300, "or_low": 25100, "spot_stop": 25100,
        "spot_target": 25700, "strike_mode": "NEAREST_ITM",
    })


@pytest.mark.asyncio
async def test_paper_entry_one_lot_and_30pct_stop(tmp_path: Path):
    s = Settings(execution_mode="paper", nifty_lots=1, db_path=tmp_path / "x.sqlite3")
    store = Store(s.db_path)
    engine = ExecutionEngine(s, store, FakeDhan(), FakeSelector())
    out = await engine.handle(tv_event())
    assert out["quantity"] == 65
    assert out["entry_price"] == 100
    assert out["option_stop_price"] == 70


@pytest.mark.asyncio
async def test_duplicate_event_is_ignored(tmp_path: Path):
    s = Settings(execution_mode="paper", db_path=tmp_path / "x.sqlite3")
    store = Store(s.db_path)
    engine = ExecutionEngine(s, store, FakeDhan(), FakeSelector())
    event = tv_event()
    await engine.handle(event)
    out = await engine.handle(event)
    assert out["duplicate"] is True


@pytest.mark.asyncio
async def test_option_stop_closes_position(tmp_path: Path):
    s = Settings(execution_mode="paper", db_path=tmp_path / "x.sqlite3")
    store = Store(s.db_path)
    engine = ExecutionEngine(s, store, FakeDhan(), FakeSelector())
    await engine.handle(tv_event())
    results = await engine.monitor_option_stops_once()
    assert results[0]["reason"] == "OPTION_STOP_30PCT"
    assert store.get_open_position("NIFTY") is None

@pytest.mark.asyncio
async def test_intelligence_shadow_features_are_returned(tmp_path: Path):
    s = Settings(execution_mode="paper", db_path=tmp_path / "x.sqlite3")
    store = Store(s.db_path)
    engine = ExecutionEngine(s, store, FakeDhan(), FakeSelector())
    out = await engine.handle(tv_event())
    intel = out["intelligence"]
    assert intel["allowed"] is True
    assert intel["features"]["opening_range_width"] == 200
    assert intel["features"]["option_spread_pct"] is not None
