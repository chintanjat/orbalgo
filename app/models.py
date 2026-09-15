from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class EventType(str, Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    FLATTEN = "FLATTEN"
    STATE = "STATE"


class Side(str, Enum):
    CE = "CE"
    PE = "PE"
    NONE = "NONE"


class TradingViewEvent(BaseModel):
    schema_: str = Field(alias="schema")
    event: EventType
    event_id: str = Field(min_length=1, max_length=200)
    trade_id: str = ""
    underlying: Literal["NIFTY", "BANKNIFTY"]
    bar_close_ms: int
    side: Side
    reason: str = ""
    spot: float
    or_high: float | None = None
    or_low: float | None = None
    spot_stop: float | None = None
    spot_target: float | None = None
    strike_mode: Literal["NEAREST_ITM", "ATM"] = "NEAREST_ITM"

    model_config = {"populate_by_name": True}

    @field_validator("schema_")
    @classmethod
    def supported_schema(cls, value: str) -> str:
        if value != "or15.v1":
            raise ValueError("unsupported schema")
        return value

    @field_validator("side")
    @classmethod
    def entry_needs_side(cls, value: Side, info):
        return value

    @property
    def bar_close_dt(self) -> datetime:
        return datetime.fromtimestamp(self.bar_close_ms / 1000)


class OptionCandidate(BaseModel):
    underlying: str
    side: Literal["CE", "PE"]
    expiry: date
    strike: float
    security_id: str
    last_price: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    bid_qty: int = 0
    ask_qty: int = 0
    volume: int = 0
    lot_size: int = 0

    @property
    def spread_pct(self) -> float | None:
        if self.bid <= 0 or self.ask <= 0:
            return None
        mid = (self.bid + self.ask) / 2
        return (self.ask - self.bid) / mid if mid > 0 else None


class Position(BaseModel):
    id: int | None = None
    underlying: Literal["NIFTY", "BANKNIFTY"]
    trade_id: str
    entry_event_id: str
    side: Literal["CE", "PE"]
    expiry: date
    strike: float
    security_id: str
    lots: int
    lot_size: int
    quantity: int
    entry_price: float
    option_stop_price: float
    status: Literal["OPEN", "CLOSED"] = "OPEN"
    entry_order_id: str = ""
    exit_order_id: str = ""
    exit_price: float | None = None
    exit_reason: str = ""
    opened_at: datetime
    closed_at: datetime | None = None
    last_option_price: float | None = None
    spot_entry: float | None = None
    spot_stop: float | None = None
    spot_target: float | None = None
