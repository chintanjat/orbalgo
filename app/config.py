from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    execution_mode: Literal["paper", "live"] = "paper"
    webhook_secret: str = "change-me"

    dhan_client_id: str = ""
    dhan_access_token: str = ""
    dhan_base_url: str = "https://api.dhan.co/v2"

    nifty_lots: int = Field(default=1, ge=1)
    banknifty_lots: int = Field(default=1, ge=1)
    allow_expiry_day: bool = False
    option_stop_pct: float = Field(default=0.30, gt=0, lt=1)

    max_option_spread_pct: float = Field(default=0.0, ge=0)
    min_option_volume: int = Field(default=0, ge=0)
    min_top_ask_multiple: float = Field(default=0.0, ge=0)

    max_alert_age_seconds: int = Field(default=180, ge=0)
    order_fill_timeout_seconds: float = Field(default=6.0, gt=0)
    stop_poll_seconds: float = Field(default=1.0, ge=1.0)
    product_type: Literal["INTRADAY", "MARGIN"] = "INTRADAY"

    nifty_underlying_security_id: int = 13
    banknifty_underlying_security_id: int = 25

    db_path: Path = Path("./or15_executor.sqlite3")
    instrument_master_cache: Path = Path("./api-scrip-master-detailed.csv")
    instrument_master_url: str = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
    log_level: str = "INFO"

    @field_validator("webhook_secret")
    @classmethod
    def secret_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("WEBHOOK_SECRET cannot be blank")
        return value

    @model_validator(mode="after")
    def validate_live_safety(self):
        if self.execution_mode == "live":
            if self.webhook_secret in {"change-me", "replace-with-a-long-random-secret"} or len(self.webhook_secret) < 16:
                raise ValueError("LIVE mode requires a non-default WEBHOOK_SECRET of at least 16 characters")
            if not self.dhan_client_id or not self.dhan_access_token:
                raise ValueError("LIVE mode requires DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN")
        return self

    def lots_for(self, underlying: str) -> int:
        return self.nifty_lots if underlying == "NIFTY" else self.banknifty_lots

    def underlying_id_for(self, underlying: str) -> int:
        return self.nifty_underlying_security_id if underlying == "NIFTY" else self.banknifty_underlying_security_id


@lru_cache
def get_settings() -> Settings:
    return Settings()
