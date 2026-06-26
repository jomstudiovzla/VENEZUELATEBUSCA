"""Configuración pública para el frontend."""

from __future__ import annotations

from fastapi import APIRouter

from app.core.config import settings

router = APIRouter(tags=["config"])


@router.get("/api/config/public")
async def public_config():
    return {
        "google_maps_api_key": settings.google_maps_api_key,
        "public_base_url": settings.public_base_url,
        "project": "Red de Esperanza Venezuela",
        "motto": "Fuerza Venezuela — Juntos Salvaremos Vidas",
    }