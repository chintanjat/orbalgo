from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import struct
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .config import Settings

log = logging.getLogger("or15.dhan")
IST = ZoneInfo("Asia/Kolkata")


class DhanError(RuntimeError):
    pass


class DhanClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.client = httpx.AsyncClient(base_url=settings.dhan_base_url, timeout=10.0, transport=transport)
        self._token_lock = asyncio.Lock()
        self._access_token = settings.dhan_access_token.strip()
        self._token_expiry_utc: datetime | None = None
        self._quote_lock = asyncio.Lock()
        self._last_quote_at = 0.0

    async def close(self):
        await self.client.aclose()

    @property
    def auto_auth_configured(self) -> bool:
        return bool(self.settings.dhan_client_id and self.settings.dhan_pin and self.settings.dhan_totp_secret)

    @staticmethod
    def _totp(secret: str, *, at_time: int | None = None) -> str:
        cleaned = "".join(secret.split()).upper()
        if not cleaned:
            raise DhanError("DHAN_TOTP_SECRET is blank")
        padding = "=" * ((8 - len(cleaned) % 8) % 8)
        try:
            key = base64.b32decode(cleaned + padding, casefold=True)
        except Exception as exc:
            raise DhanError("DHAN_TOTP_SECRET is not valid Base32") from exc
        unix_time = int(time.time() if at_time is None else at_time)
        counter = unix_time // 30
        digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
        offset = digest[-1] & 0x0F
        code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
        return f"{code:06d}"

    @staticmethod
    def _parse_expiry(value: Any) -> datetime | None:
        if not value:
            return None
        text = str(value).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        return dt.astimezone(timezone.utc)

    def _token_needs_refresh(self) -> bool:
        if not self._access_token:
            return True
        if not self.auto_auth_configured:
            return False
        if self._token_expiry_utc is None:
            return True
        return datetime.now(timezone.utc) >= self._token_expiry_utc - timedelta(minutes=5)

    async def _generate_access_token(self) -> str:
        if not self.settings.dhan_client_id:
            raise DhanError("DHAN_CLIENT_ID is not configured")
        if not self.settings.dhan_pin:
            raise DhanError("DHAN_PIN is not configured")
        if not self.settings.dhan_totp_secret:
            raise DhanError("DHAN_TOTP_SECRET is not configured")

        response = await self.client.request(
            "POST",
            self.settings.dhan_auth_url,
            params={
                "dhanClientId": self.settings.dhan_client_id,
                "pin": self.settings.dhan_pin,
                "totp": self._totp(self.settings.dhan_totp_secret),
            },
            headers={"Accept": "application/json"},
        )
        if response.status_code >= 400:
            raise DhanError(f"Dhan TOTP token generation -> {response.status_code}: {response.text[:500]}")

        data = response.json()
        token = str(data.get("accessToken") or "").strip()
        if not token:
            raise DhanError(f"Dhan TOTP token generation returned no accessToken: {data}")

        self._access_token = token
        self._token_expiry_utc = self._parse_expiry(data.get("expiryTime"))
        if self._token_expiry_utc is None:
            self._token_expiry_utc = datetime.now(timezone.utc) + timedelta(hours=23)
        log.info("generated Dhan access token via TOTP; expires_at=%s", self._token_expiry_utc.isoformat())
        return self._access_token

    async def ensure_authenticated(self, *, force_refresh: bool = False) -> str:
        if not force_refresh and not self._token_needs_refresh():
            return self._access_token
        if not self.auto_auth_configured:
            if self._access_token:
                return self._access_token
            raise DhanError(
                "Dhan auth is not configured. Set DHAN_ACCESS_TOKEN, or configure "
                "DHAN_CLIENT_ID + DHAN_PIN + DHAN_TOTP_SECRET."
            )
        async with self._token_lock:
            if not force_refresh and not self._token_needs_refresh():
                return self._access_token
            return await self._generate_access_token()

    def _headers(self, token: str, *, quote: bool = False) -> dict[str, str]:
        h = {"access-token": token, "Content-Type": "application/json", "Accept": "application/json"}
        if quote:
            if not self.settings.dhan_client_id:
                raise DhanError("DHAN_CLIENT_ID is not configured")
            h["client-id"] = self.settings.dhan_client_id
        return h

    async def _request(self, method: str, path: str, *, json: Any = None, quote: bool = False) -> Any:
        token = await self.ensure_authenticated()
        r = await self.client.request(method, path, headers=self._headers(token, quote=quote), json=json)
        if r.status_code == 401 and self.auto_auth_configured:
            token = await self.ensure_authenticated(force_refresh=True)
            r = await self.client.request(method, path, headers=self._headers(token, quote=quote), json=json)
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
