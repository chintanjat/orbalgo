from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import httpx

from .config import Settings


@dataclass(frozen=True)
class InstrumentContract:
    underlying: str
    security_id: str
    expiry: date
    strike: float
    option_type: str
    lot_size: int


class InstrumentMaster:
    """Small in-memory view of Dhan's daily master, limited to NIFTY/BANKNIFTY index options."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._lot_sizes: dict[str, int] = {}
        self._contracts: dict[str, list[InstrumentContract]] = {"NIFTY": [], "BANKNIFTY": []}
        self._loaded_for_date: date | None = None

    async def ensure_fresh(self) -> None:
        today = datetime.now().date()
        path = self.settings.instrument_master_cache
        if self._loaded_for_date == today and any(self._contracts.values()):
            return
        if path.exists() and datetime.fromtimestamp(path.stat().st_mtime).date() == today:
            self._load(path)
            return
        await self.refresh()

    async def refresh(self) -> None:
        path = self.settings.instrument_master_cache
        path.parent.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            r = await client.get(self.settings.instrument_master_url)
            r.raise_for_status()
            path.write_bytes(r.content)
        self._load(path)

    def load_cached(self) -> None:
        if self.settings.instrument_master_cache.exists():
            self._load(self.settings.instrument_master_cache)

    def _load(self, path: Path) -> None:
        lot_sizes: dict[str, int] = {}
        contracts: dict[str, list[InstrumentContract]] = {"NIFTY": [], "BANKNIFTY": []}
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                sid = self._first(row, "SECURITY_ID", "SEM_SMST_SECURITY_ID")
                lot = self._first(row, "LOT_SIZE", "SEM_LOT_UNITS")
                if sid and lot:
                    try:
                        lot_sizes[str(sid).strip()] = int(float(lot))
                    except ValueError:
                        pass

                underlying = (self._first(row, "UNDERLYING_SYMBOL") or "").upper().strip()
                instrument = (self._first(row, "INSTRUMENT", "SEM_INSTRUMENT_NAME") or "").upper().strip()
                exch = (self._first(row, "EXCH_ID", "SEM_EXM_EXCH_ID") or "").upper().strip()
                if underlying not in contracts or instrument != "OPTIDX" or exch != "NSE" or not sid or not lot:
                    continue
                expiry_raw = self._first(row, "SM_EXPIRY_DATE", "SEM_EXPIRY_DATE")
                strike_raw = self._first(row, "STRIKE_PRICE", "SEM_STRIKE_PRICE")
                option_raw = (self._first(row, "OPTION_TYPE", "SEM_OPTION_TYPE") or "").upper().strip()
                if not expiry_raw or not strike_raw or option_raw not in {"CE", "PE"}:
                    continue
                try:
                    expiry = self._parse_date(expiry_raw)
                    strike = float(strike_raw)
                    lot_size = int(float(lot))
                except (ValueError, TypeError):
                    continue
                contracts[underlying].append(
                    InstrumentContract(
                        underlying=underlying,
                        security_id=str(sid).strip(),
                        expiry=expiry,
                        strike=strike,
                        option_type=option_raw,
                        lot_size=lot_size,
                    )
                )
        if not lot_sizes:
            raise RuntimeError("Could not parse security IDs / lot sizes from Dhan instrument master")
        self._lot_sizes = lot_sizes
        self._contracts = contracts
        self._loaded_for_date = datetime.now().date()

    @staticmethod
    def _parse_date(value: str) -> date:
        text = str(value).strip()
        for candidate in (text, text[:10]):
            try:
                return datetime.fromisoformat(candidate).date()
            except ValueError:
                pass
        for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d %b %Y"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                pass
        raise ValueError(f"unsupported expiry date {value!r}")

    @staticmethod
    def _first(row: dict, *keys: str) -> str | None:
        for key in keys:
            if row.get(key) not in (None, ""):
                return row[key]
        return None

    async def option_contracts(self, underlying: str) -> list[InstrumentContract]:
        await self.ensure_fresh()
        return list(self._contracts.get(underlying, []))

    async def lot_size(self, security_id: str) -> int:
        await self.ensure_fresh()
        lot = self._lot_sizes.get(str(security_id))
        if lot is None or lot <= 0:
            raise RuntimeError(f"Lot size not found for Dhan security_id={security_id}")
        return lot
