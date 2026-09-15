from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from .config import Settings
from .models import OptionCandidate, TradingViewEvent

IST = ZoneInfo("Asia/Kolkata")


class AdmissionDecision(BaseModel):
    allowed: bool
    rejection_reasons: list[str]
    features: dict[str, Any]


class IntelligenceLayer:
    """Deterministic pre-trade admission + shadow feature collection.

    Important: features are observed, not scored into an invented trading filter.
    A feature can block a trade only when its corresponding config gate is enabled.
    """

    def __init__(self, settings: Settings):
        self.settings = settings

    def evaluate(self, event: TradingViewEvent, option: OptionCandidate, quantity: int) -> AdmissionDecision:
        reasons: list[str] = []
        if option.ask <= 0 and option.last_price <= 0:
            reasons.append("NO_USABLE_OPTION_PRICE")

        spread = option.spread_pct
        if self.settings.max_option_spread_pct > 0 and spread is not None and spread > self.settings.max_option_spread_pct:
            reasons.append("SPREAD_TOO_WIDE")
        if self.settings.min_option_volume > 0 and option.volume < self.settings.min_option_volume:
            reasons.append("VOLUME_TOO_LOW")
        if (
            self.settings.min_top_ask_multiple > 0
            and option.ask_qty < quantity * self.settings.min_top_ask_multiple
        ):
            reasons.append("ASK_LIQUIDITY_TOO_LOW")

        or_width = None
        breakout_distance = None
        breakout_or_ratio = None
        if event.or_high is not None and event.or_low is not None and event.or_high > event.or_low:
            or_width = event.or_high - event.or_low
            if event.side.value == "CE":
                breakout_distance = event.spot - event.or_high
            elif event.side.value == "PE":
                breakout_distance = event.or_low - event.spot
            if breakout_distance is not None:
                breakout_or_ratio = breakout_distance / or_width

        spot_risk = abs(event.spot - event.spot_stop) if event.spot_stop is not None else None
        inferred_rr = None
        if event.spot_target is not None and spot_risk and spot_risk > 0:
            inferred_rr = abs(event.spot_target - event.spot) / spot_risk

        dt = datetime.fromtimestamp(event.bar_close_ms / 1000, tz=IST)
        features: dict[str, Any] = {
            "signal_time_ist": dt.isoformat(),
            "minute_of_session": max(0, (dt.hour * 60 + dt.minute) - (9 * 60 + 15)),
            "spot": event.spot,
            "opening_range_width": or_width,
            "breakout_distance": breakout_distance,
            "breakout_or_ratio": breakout_or_ratio,
            "spot_risk_points": spot_risk,
            "inferred_spot_rr": inferred_rr,
            "option_security_id": option.security_id,
            "option_expiry": option.expiry.isoformat(),
            "option_strike": option.strike,
            "option_ltp": option.last_price,
            "option_bid": option.bid,
            "option_ask": option.ask,
            "option_spread_pct": spread,
            "option_volume": option.volume,
            "top_ask_qty": option.ask_qty,
            "order_quantity": quantity,
            "top_ask_qty_multiple": (option.ask_qty / quantity) if quantity > 0 else None,
        }
        return AdmissionDecision(allowed=not reasons, rejection_reasons=reasons, features=features)
