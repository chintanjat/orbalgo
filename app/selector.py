from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from .config import Settings
from .dhan import DhanClient
from .instruments import InstrumentContract, InstrumentMaster
from .models import OptionCandidate, TradingViewEvent

IST = ZoneInfo("Asia/Kolkata")


class SelectionError(RuntimeError):
    pass


class OptionSelector:
    def __init__(self, settings: Settings, dhan: DhanClient, instruments: InstrumentMaster):
        self.settings = settings
        self.dhan = dhan
        self.instruments = instruments

    async def select(self, event: TradingViewEvent, *, today: date | None = None) -> OptionCandidate:
        if event.side.value not in {"CE", "PE"}:
            raise SelectionError("ENTRY event requires CE or PE side")
        today = today or datetime.now(IST).date()

        # Preferred path: Dhan daily instrument master. This keeps option-chain's 3-second
        # throttle out of the latency-critical entry path.
        contracts = await self.instruments.option_contracts(event.underlying)
        eligible = [
            c for c in contracts
            if c.option_type == event.side.value
            and c.expiry >= today
            and (self.settings.allow_expiry_day or c.expiry != today)
        ]
        if not eligible:
            raise SelectionError(f"No eligible {event.underlying} {event.side.value} contracts in Dhan instrument master")

        expiry = min(c.expiry for c in eligible)
        expiry_contracts = [c for c in eligible if c.expiry == expiry]
        strike = self._choose_strike(sorted({c.strike for c in expiry_contracts}), event.spot, event.side.value, event.strike_mode)
        matches = [c for c in expiry_contracts if abs(c.strike - strike) < 1e-9]
        if len(matches) != 1:
            raise SelectionError(f"Expected one contract for {expiry} {strike} {event.side.value}, got {len(matches)}")
        contract = matches[0]

        quote = await self.dhan.quote(contract.security_id)
        depth = quote.get("depth") or {}
        buy = (depth.get("buy") or [{}])[0] if depth.get("buy") else {}
        sell = (depth.get("sell") or [{}])[0] if depth.get("sell") else {}
        return OptionCandidate(
            underlying=event.underlying,
            side=event.side.value,
            expiry=expiry,
            strike=strike,
            security_id=contract.security_id,
            last_price=float(quote.get("last_price") or 0),
            bid=float(buy.get("price") or 0),
            ask=float(sell.get("price") or 0),
            bid_qty=int(buy.get("quantity") or 0),
            ask_qty=int(sell.get("quantity") or 0),
            volume=int(quote.get("volume") or 0),
            lot_size=contract.lot_size,
        )

    @staticmethod
    def _choose_strike(strikes: list[float], spot: float, side: str, mode: str) -> float:
        if not strikes:
            raise SelectionError("No strikes available")
        if mode == "ATM":
            return min(strikes, key=lambda x: (abs(x - spot), x))
        if side == "CE":
            itm = [x for x in strikes if x < spot]
            if not itm:
                raise SelectionError("No ITM CE strike below spot")
            return max(itm)
        itm = [x for x in strikes if x > spot]
        if not itm:
            raise SelectionError("No ITM PE strike above spot")
        return min(itm)
