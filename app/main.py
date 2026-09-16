from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from .config import get_settings
from .dhan import DhanClient
from .engine import ExecutionEngine
from .instruments import InstrumentMaster
from .intelligence import IntelligenceLayer
from .models import TradingViewEvent
from .selector import OptionSelector
from .store import Store

settings = get_settings()
logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("or15")

store = Store(settings.db_path)
dhan = DhanClient(settings)
instruments = InstrumentMaster(settings)
selector = OptionSelector(settings, dhan, instruments)
intelligence = IntelligenceLayer(settings)
engine = ExecutionEngine(settings, store, dhan, selector, intelligence)


async def stop_supervisor(stop_event: asyncio.Event):
    while not stop_event.is_set():
        try:
            await engine.monitor_option_stops_once()
        except Exception:
            log.exception("option-stop supervisor cycle failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.stop_poll_seconds)
        except asyncio.TimeoutError:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    instruments.load_cached()
    await dhan.ensure_authenticated()
    stop_event = asyncio.Event()
    task = asyncio.create_task(stop_supervisor(stop_event))
    yield
    stop_event.set()
    await task
    await dhan.close()


app = FastAPI(title="OR15 Dhan Executor", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health():
    return {
        "ok": True,
        "execution_mode": settings.execution_mode,
        "nifty_lots": settings.nifty_lots,
        "banknifty_lots": settings.banknifty_lots,
        "allow_expiry_day": settings.allow_expiry_day,
        "option_stop_pct": settings.option_stop_pct,
        "open_positions": [p.model_dump(mode="json") for p in store.list_open_positions()],
    }


@app.post("/webhook/tradingview/{secret}")
async def tradingview_webhook(secret: str, event: TradingViewEvent):
    if secret != settings.webhook_secret:
        raise HTTPException(status_code=404, detail="not found")
    try:
        return await engine.handle(event)
    except Exception as exc:
        raise HTTPException(status_code=409, detail={"error": type(exc).__name__, "message": str(exc)}) from exc
