from datetime import date

import pytest

from app.config import Settings
from app.instruments import InstrumentContract
from app.models import TradingViewEvent
from app.selector import OptionSelector


class FakeDhan:
    async def quote(self, security_id):
        return {
            "last_price": 120,
            "volume": 10000,
            "depth": {
                "buy": [{"price": 119, "quantity": 1000}],
                "sell": [{"price": 121, "quantity": 1200}],
            },
        }


class FakeMaster:
    async def option_contracts(self, underlying):
        assert underlying == "NIFTY"
        return [
            InstrumentContract("NIFTY", "today-ce", date(2026, 9, 15), 25300, "CE", 65),
            InstrumentContract("NIFTY", "101", date(2026, 9, 22), 25300, "CE", 65),
            InstrumentContract("NIFTY", "102", date(2026, 9, 22), 25350, "CE", 65),
            InstrumentContract("NIFTY", "202", date(2026, 9, 22), 25350, "PE", 65),
            InstrumentContract("NIFTY", "203", date(2026, 9, 22), 25400, "PE", 65),
        ]


def event(side="CE", mode="NEAREST_ITM"):
    return TradingViewEvent.model_validate({
        "schema": "or15.v1", "event": "ENTRY", "event_id": "e1", "trade_id": "t1",
        "underlying": "NIFTY", "bar_close_ms": 1, "side": side, "reason": "OR_EMA",
        "spot": 25347, "or_high": 25300, "or_low": 25100, "spot_stop": 25100,
        "spot_target": 25700, "strike_mode": mode,
    })


@pytest.mark.asyncio
async def test_nearest_itm_ce_skips_expiry_day():
    s = Settings(allow_expiry_day=False)
    sel = OptionSelector(s, FakeDhan(), FakeMaster())
    c = await sel.select(event("CE"), today=date(2026, 9, 15))
    assert c.expiry == date(2026, 9, 22)
    assert c.strike == 25300
    assert c.security_id == "101"
    assert c.ask == 121


@pytest.mark.asyncio
async def test_nearest_itm_pe_is_above_spot():
    s = Settings(allow_expiry_day=False)
    sel = OptionSelector(s, FakeDhan(), FakeMaster())
    c = await sel.select(event("PE"), today=date(2026, 9, 15))
    assert c.strike == 25350
    assert c.security_id == "202"
