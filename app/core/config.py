"""Configuración — Red de Esperanza (logística humanitaria)."""

from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:///./red_esperanza.db"
    host: str = "0.0.0.0"
    port: int = 8000
    public_base_url: str = ""
    match_threshold: float = 0.55
    matcher_interval_seconds: int = 45

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()