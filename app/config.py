from __future__ import annotations

import json
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables and `.env`."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    bot_token: str
    database_url: str
    admin_ids: Annotated[list[int], NoDecode]
    subscription_cache_ttl_seconds: int = 180
    # v1 deliberately keeps this as an environment setting rather than an admin screen.
    welcome_message: str = "Добро пожаловать!"
    log_level: str = "INFO"

    @field_validator("admin_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                return json.loads(stripped)
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return value
