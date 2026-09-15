from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timezone

from .config import Settings
from .dhan import DhanClient, DhanError
from .models import EventType, Position, TradingViewEvent
from .intelligence import IntelligenceLayer
from .selector import OptionSelector, SelectionError
from .store import Store

log = logging.getLogger("or15.engine")


class EngineError(RuntimeError):
    pass


class ExecutionEngine:
    def __init__(self, settings: Settings, store: Store, dhan: DhanClient, selector: OptionSelector, intelligence: IntelligenceLayer | None = None):
        self.settings = settings
        self.store = store
        self.dhan = dhan
        self.selector = selector
        self.intelligence = intelligence or IntelligenceLayer(settings)
        self._locks = {"NIFTY": asyncio.Lock(), "BANKNIFTY": asyncio.Lock()}

    async def handle(self, event: TradingViewEvent) -> dict:
        if not self.store.claim_event(event):
            return {"ok": True, "duplicate": True, "event_id": event.event_id}

        try:
            self._validate_freshness(event)
            async with self._locks[event.underlying]:
                if event.event == EventType.ENTRY:
                    result = await self._enter(event)
                elif event.event == EventType.EXIT:
                    result = await self._exit(event.underlying, event.reason or "SPOT_EXIT")
                elif event.event == EventType.FLATTEN:
                    result = await self._exit(event.underlying, event.reason or "SESSION_END")
                else:
                    result = {"ok": True, "action": "STATE_ACK"}
            self.store.set_event_outcome(event.event_id, result)
            return result
        except Exception as exc:
            result = {"ok": False, "error": type(exc).__name__, "message": str(exc)}
            self.store.set_event_outcome(event.event_id, result)
            log.exception("event %s failed", event.event_id)
            raise

    def _validate_freshness(self, event: TradingViewEvent):
        if self.settings.max_alert_age_seconds == 0:
            return
        age = datetime.now(timezone.utc).timestamp() - event.bar_close_ms / 1000
        if age > self.settings.max_alert_age_seconds:
            raise EngineError(f"stale alert: {age:.1f}s old")
        if age < -30:
            raise EngineError("alert timestamp is too far in the future")

    async def _enter(self, event: TradingViewEvent) -> dict:
        if event.side.value not in {"CE", "PE"}:
            raise EngineError("ENTRY requires side CE or PE")
        existing = self.store.get_open_position(event.underlying)
        if existing:
            return {"ok": True, "action": "ENTRY_IGNORED_ALREADY_OPEN", "position_id": existing.id}

        option = await self.selector.select(event)
        lots = self.settings.lots_for(event.underlying)
        quantity = lots * option.lot_size
        decision = self.intelligence.evaluate(event, option, quantity)
        if not decision.allowed:
            raise EngineError("trade rejected: " + ",".join(decision.rejection_reasons))

        correlation = self._correlation(event.event_id, "E")
        if self.settings.execution_mode == "paper":
            entry_price = option.ask or option.last_price or option.bid
            if entry_price <= 0:
                raise EngineError("paper fill impossible: option has no positive ask/LTP/bid")
            order_id = f"PAPER-{correlation}"
            filled_qty = quantity
        else:
            placed = await self.dhan.place_market_order(
                transaction_type="BUY", security_id=option.security_id, quantity=quantity, correlation_id=correlation
            )
            order_id = str(placed.get("orderId") or "")
            if not order_id:
                raise EngineError(f"Dhan entry returned no orderId: {placed}")
            status = await self.dhan.await_fill(order_id, self.settings.order_fill_timeout_seconds)
            filled_qty = int(status.get("filledQty") or 0)
            entry_price = float(status.get("averageTradedPrice") or status.get("tradedPrice") or 0)
            if status.get("orderStatus") == "REJECTED":
                raise EngineError(f"Dhan entry rejected: {status.get('omsErrorDescription') or status}")
            if filled_qty <= 0 or entry_price <= 0:
                if status.get("orderStatus") in {"PENDING", "PART_TRADED", "TRANSIT"}:
                    await self.dhan.cancel_order(order_id)
                raise EngineError(f"entry did not fill: {status}")
            if filled_qty < quantity and status.get("orderStatus") in {"PENDING", "PART_TRADED", "TRANSIT"}:
                await self.dhan.cancel_order(order_id)

        stop_price = entry_price * (1 - self.settings.option_stop_pct)
        p = Position(
            underlying=event.underlying,
            trade_id=event.trade_id,
            entry_event_id=event.event_id,
            side=event.side.value,
            expiry=option.expiry,
            strike=option.strike,
            security_id=option.security_id,
            lots=lots,
            lot_size=option.lot_size,
            quantity=filled_qty,
            entry_price=entry_price,
            option_stop_price=stop_price,
            entry_order_id=order_id,
            opened_at=datetime.now(timezone.utc),
            spot_entry=event.spot,
            spot_stop=event.spot_stop,
            spot_target=event.spot_target,
        )
        p = self.store.insert_position(p)
        return {
            "ok": True,
            "action": "ENTRY_FILLED" if self.settings.execution_mode == "live" else "PAPER_ENTRY",
            "position_id": p.id,
            "underlying": p.underlying,
            "side": p.side,
            "expiry": str(p.expiry),
            "strike": p.strike,
            "security_id": p.security_id,
            "lots": p.lots,
            "lot_size": p.lot_size,
            "quantity": p.quantity,
            "entry_price": p.entry_price,
            "option_stop_price": round(p.option_stop_price, 4),
            "intelligence": decision.model_dump(mode="json"),
        }

    async def _exit(self, underlying: str, reason: str) -> dict:
        p = self.store.get_open_position(underlying)
        if not p:
            return {"ok": True, "action": "EXIT_NO_POSITION", "underlying": underlying, "reason": reason}

        correlation = self._correlation(p.entry_event_id + reason, "X")
        if self.settings.execution_mode == "paper":
            prices = await self.dhan.ltp([p.security_id])
            exit_price = prices.get(p.security_id) or p.last_option_price or p.entry_price
            order_id = f"PAPER-{correlation}"
        else:
            placed = await self.dhan.place_market_order(
                transaction_type="SELL", security_id=p.security_id, quantity=p.quantity, correlation_id=correlation
            )
            order_id = str(placed.get("orderId") or "")
            if not order_id:
                raise EngineError(f"Dhan exit returned no orderId: {placed}")
            status = await self.dhan.await_fill(order_id, self.settings.order_fill_timeout_seconds)
            filled = int(status.get("filledQty") or 0)
            if status.get("orderStatus") == "REJECTED":
                raise EngineError(f"Dhan exit rejected: {status.get('omsErrorDescription') or status}")
            if filled < p.quantity:
                # Never mark closed if we cannot prove the full managed quantity was sold.
                raise EngineError(f"exit only filled {filled}/{p.quantity}; manual/reconciliation action required: {status}")
            exit_price = float(status.get("averageTradedPrice") or status.get("tradedPrice") or 0)
            if exit_price <= 0:
                raise EngineError(f"Dhan exit fill has no valid price: {status}")

        self.store.close_position(p.id, exit_price=exit_price, reason=reason, exit_order_id=order_id)
        return {
            "ok": True,
            "action": "POSITION_CLOSED",
            "position_id": p.id,
            "underlying": p.underlying,
            "reason": reason,
            "entry_price": p.entry_price,
            "exit_price": exit_price,
            "quantity": p.quantity,
            "pnl_rupees_before_costs": round((exit_price - p.entry_price) * p.quantity, 2),
        }

    async def monitor_option_stops_once(self) -> list[dict]:
        positions = self.store.list_open_positions()
        if not positions:
            return []
        prices = await self.dhan.ltp([p.security_id for p in positions])
        results: list[dict] = []
        for p in positions:
            ltp = prices.get(p.security_id)
            if ltp is None or ltp <= 0:
                continue
            self.store.update_last_price(p.id, ltp)
            if ltp <= p.option_stop_price:
                async with self._locks[p.underlying]:
                    current = self.store.get_open_position(p.underlying)
                    if current and current.id == p.id:
                        results.append(await self._exit(p.underlying, "OPTION_STOP_30PCT"))
        return results

    @staticmethod
    def _correlation(value: str, suffix: str) -> str:
        digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:24]
        return f"or15-{suffix}-{digest}"[:30]
