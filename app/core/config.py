"""Configuración — Red de Esperanza (logística humanitaria)."""

from __future__ import annotations

from pydantic import model_validator
from pydantic_settings import BaseSettings


def _default_firebase_database_url(project_id: str) -> str:
    slug = (project_id or "").strip().lower()
    if not slug:
        return ""
    return f"https://{slug}-default-rtdb.firebaseio.com"


class Settings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:///./red_esperanza.db"
    victims_database_url: str = "sqlite+aiosqlite:///./ojo_de_dios.db"
    victims_api_url: str = "https://desaparecidos-terremoto-api.theempire.tech/api"
    victims_sync_interval_seconds: int = 20
    victims_sync_page_size: int = 100
    victims_sync_max_pages: int = 5
    victims_cedula_batch_size: int = 1000
    host: str = "0.0.0.0"
    port: int = 8000
    public_base_url: str = ""
    match_threshold: float = 0.55
    matcher_interval_seconds: int = 45
    jwt_secret: str = "cambiar-en-produccion-red-esperanza-2026"
    jwt_expire_hours: int = 72
    super_admin_username: str = "JOM"
    super_admin_password: str = "Studio"
    live_sync_interval_seconds: float = 0.25
    punto_apoyo_sync_seconds: int = 25
    punto_apoyo_centros_url: str = "https://punto-de-apoyo.vercel.app/centros.js"
    punto_apoyo_supabase_url: str = "https://hkvtqoivrcicipwwjsmz.supabase.co"
    punto_apoyo_supabase_key: str = ""
    firebase_project_id: str = "Esperanzavzla"
    firebase_database_url: str = ""
    firebase_api_key: str = ""
    firebase_app_id: str = "1:192287687789:web:a376ad649a10a0bbe8ab64"
    firebase_messaging_sender_id: str = "192287687789"
    firebase_credentials_path: str = ""
    google_maps_api_key: str = ""
    redis_url: str = ""

    @model_validator(mode="after")
    def _derive_firebase_url(self) -> "Settings":
        if self.firebase_project_id and not self.firebase_database_url:
            object.__setattr__(
                self,
                "firebase_database_url",
                _default_firebase_database_url(self.firebase_project_id),
            )
        return self

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()