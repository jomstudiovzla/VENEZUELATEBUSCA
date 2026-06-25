"""Configuración central — sin interceptación de redes no autorizadas."""

from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:///./ojo_de_dios.db"
    yolov7_weights: str = "weights/yolov7-oa.pt"
    tattoo_match_threshold: float = 0.72
    height_tolerance_cm: float = 8.0
    host: str = "0.0.0.0"
    port: int = 8000
    public_base_url: str = ""
    enable_realtime_workers: bool = False
    max_upload_mb: int = 50

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()