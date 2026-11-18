from __future__ import annotations

import re
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _http_url(value: str, name: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"{name} must be an http(s) URL")
    return value.rstrip("/")


class Settings(BaseSettings):
    """Environment configuration. Validated once at startup; any error stops the process.

    No .env file is read: values come from the process environment only.
    """

    model_config = SettingsConfigDict(extra="ignore", populate_by_name=True)

    issuer: str = Field(alias="RERO_ISSUER", min_length=1)
    jwks_uri: str = Field(alias="RERO_JWKS_URI")
    required_audiences: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["rero-api", "ai-gateway"], alias="RERO_REQUIRED_AUDIENCES"
    )
    reconcile_internal_base_url: str = Field(alias="RECONCILE_INTERNAL_BASE_URL")
    model_adapter: Literal["fake", "gateway"] = Field(default="fake", alias="MODEL_ADAPTER")
    gateway_base_url: str | None = Field(default=None, alias="GATEWAY_BASE_URL")
    result_ttl_seconds: int = Field(default=86400, alias="RERO_RESULT_TTL", ge=1)
    db_path: str = Field(default="rero.db", alias="RERO_DB_PATH", min_length=1)
    tool_timeout_seconds: float = Field(default=10.0, alias="RERO_TOOL_TIMEOUT_SECONDS", gt=0, le=60)
    gateway_timeout_seconds: float = Field(default=30.0, alias="RERO_GATEWAY_TIMEOUT_SECONDS", gt=0, le=120)

    @field_validator("required_audiences", mode="before")
    @classmethod
    def _split_audiences(cls, value: object) -> object:
        if isinstance(value, str):
            value = [part.strip() for part in value.split(",") if part.strip()]
        if not value:
            raise ValueError("RERO_REQUIRED_AUDIENCES must not be empty")
        return value

    @field_validator("result_ttl_seconds", mode="before")
    @classmethod
    def _parse_ttl(cls, value: object) -> object:
        """Accept seconds (86400) or a suffixed duration (30m, 24h, 1d)."""
        if isinstance(value, str):
            match = re.fullmatch(r"\s*(\d+)\s*([smhd]?)\s*", value)
            if not match:
                raise ValueError("RERO_RESULT_TTL must look like 86400, 30m, 24h or 1d")
            return int(match.group(1)) * _UNITS.get(match.group(2) or "s")
        return value

    @field_validator("jwks_uri")
    @classmethod
    def _check_jwks(cls, value: str) -> str:
        return _http_url(value, "RERO_JWKS_URI")

    @field_validator("reconcile_internal_base_url")
    @classmethod
    def _check_reconcile(cls, value: str) -> str:
        return _http_url(value, "RECONCILE_INTERNAL_BASE_URL")

    @field_validator("gateway_base_url")
    @classmethod
    def _check_gateway(cls, value: str | None) -> str | None:
        return _http_url(value, "GATEWAY_BASE_URL") if value else None

    @model_validator(mode="after")
    def _gateway_required(self) -> Settings:
        if self.model_adapter == "gateway" and not self.gateway_base_url:
            raise ValueError("GATEWAY_BASE_URL is required when MODEL_ADAPTER=gateway")
        return self
