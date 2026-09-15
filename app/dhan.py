from __future__ import annotations

import asyncio
from typing import Any

import httpx

from .config import Settings


class DhanError(RuntimeError):
    pass


class DhanClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.client = httpx.AsyncClient(base_url=settings.dhan_base_url, timeout=10.0, transport=transport)
        self._quote_lock = asyncio.Lock()
        self._last_quote_at = 0.0

    async def close(self):
        await self.client.aclose()

    def _headers(self, *, quote: bool = False) -> dict[str, str]:
        if not self.settings.dhan_access_token:
            raise DhanError("DHAN_ACCESS_TOKEN is not configured")
        h = {"access-token": self.settings.dhan_access_token, "Content-Type": "application/json", "Accept": "application/json"}
        if quote:
            if not self.settings.dhan_client_id:
                raise DhanError("DHAN_CLIENT_ID is not configured")
            h["client-id"] = self.settings.dhan_client_id
        return h

    async def _request(self, method: str, path: str, *, json: Any = None, quote: bool = False) -> Any:
        r = await self.client.request(method, path, headers=self._headers(quote=quote), json=json)
        if r.status_code >= 400:
            raise DhanError(f"Dhan {method} {path} -> {r.status_code}: {r.text[:500]}")
        if not r.content:
            return {}
        data = r.json()
        if isinstance(data, dict) and data.get("status") == "failure":
            raise DhanError(f"Dhan failure for {path}: {data}")
        return data

    async def expiry_list(self, underlying_security_id: int) -> list[str]:
        data = await self._request(
            "POST", "/optionchain/expirylist",
            json={"UnderlyingScrip": underlying_security_id, "UnderlyingSeg": "IDX_I"},
            quote=True,
        )
        return data.get("data") or []

    async def option_chain(self, underlying_security_id: int, expiry: str) -> dict:
        data = await self._request(
            "POST", "/optionchain",
            json={"UnderlyingScrip": underlying_security_id, "UnderlyingSeg": "IDX_I", "Expiry": expiry},
            quote=True,
        )
        return data.get("data") or {}



    async def _throttle_quote_api(self) -> None:
        # Dhan documents Quote APIs at 1 request/second. Keep a small cushion.
        async with self._quote_lock:
            now = asyncio.get_running_loop().time()
            wait = 1.05 - (now - self._last_quote_at)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_quote_at = asyncio.get_running_loop().time()

    async def quote(self, security_id: str) -> dict:
        await self._throttle_quote_api()
        data = await self._request("POST", "/marketfeed/quote", json={"NSE_FNO": [int(security_id)]}, quote=True)
        return ((data.get("data") or {}).get("NSE_FNO") or {}).get(str(security_id)) or {}

    async def ltp(self, security_ids: list[str]) -> dict[str, float]:
        if not security_ids:
            return {}
        await self._throttle_quote_api()
        data = await self._request("POST", "/marketfeed/ltp", json={"NSE_FNO": [int(x) for x in security_ids]}, quote=True)
        raw = (data.get("data") or {}).get("NSE_FNO") or {}
        return {str(k): float(v.get("last_price") or 0) for k, v in raw.items()}

    async def place_market_order(self, *, transaction_type: str, security_id: str, quantity: int, correlation_id: str) -> dict:
        payload = {
            "dhanClientId": self.settings.dhan_client_id,
            "correlationId": correlation_id[:30],
            "transactionType": transaction_type,
            "exchangeSegment": "NSE_FNO",
            "productType": self.settings.product_type,
            "orderType": "MARKET",
            "validity": "DAY",
            "securityId": str(security_id),
            "quantity": int(quantity),
            "disclosedQuantity": 0,
            "price": 0,
            "triggerPrice": 0,
            "afterMarketOrder": False,
            "amoTime": "",
            "boProfitValue": 0,
            "boStopLossValue": 0,
        }
        return await self._request("POST", "/orders", json=payload)

    async def get_order(self, order_id: str) -> dict:
        return await self._request("GET", f"/orders/{order_id}")

    async def get_order_by_correlation(self, correlation_id: str) -> dict:
        return await self._request("GET", f"/orders/external/{correlation_id[:30]}")

    async def cancel_order(self, order_id: str) -> dict:
        return await self._request("DELETE", f"/orders/{order_id}")

    async def await_fill(self, order_id: str, timeout_seconds: float) -> dict:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        last: dict = {}
        while asyncio.get_running_loop().time() < deadline:
            last = await self.get_order(order_id)
            if last.get("orderStatus") in {"TRADED", "REJECTED", "CANCELLED", "EXPIRED"}:
                return last
            await asyncio.sleep(0.35)
        return last
