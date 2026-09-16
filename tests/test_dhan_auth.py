import httpx
import pytest

from app.config import Settings
from app.dhan import DhanClient


def test_totp_rfc6238_vector():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert DhanClient._totp(secret, at_time=59) == "287082"


@pytest.mark.asyncio
async def test_totp_auth_generates_and_uses_token():
    calls = []

    def handler(request: httpx.Request):
        calls.append(request)
        if request.url.host == "auth.dhan.co":
            assert request.url.params["dhanClientId"] == "123456"
            assert request.url.params["pin"] == "123456"
            assert len(request.url.params["totp"]) == 6
            return httpx.Response(200, json={
                "accessToken": "generated-token",
                "expiryTime": "2099-01-01T12:00:00.000",
            })
        assert request.headers["access-token"] == "generated-token"
        return httpx.Response(200, json={"data": ["2099-01-01"]})

    settings = Settings(
        dhan_client_id="123456",
        dhan_pin="123456",
        dhan_totp_secret="GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ",
        dhan_access_token="",
    )
    client = DhanClient(settings, transport=httpx.MockTransport(handler))
    try:
        assert await client.expiry_list(13) == ["2099-01-01"]
        assert len(calls) == 2
    finally:
        await client.close()
