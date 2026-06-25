"""Estadísticas instantáneas del tablero (SQLite + caché en vivo)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import func, select

from connection_manager import victim_room_manager
from database import MissingStatus, MissingVictim, async_session_factory
from event_bus import missing_updates_bus
from photo_realtime import get_photo_stats
from terremoto_photos import get_building_photo_stats

EMERGENCIAS_CONFIG = Path("config/emergencias_venezuela.json")

_live_stats_cache: dict[str, Any] = {}


def update_live_stats_cache(stats: dict[str, Any]) -> None:
    global _live_stats_cache
    _live_stats_cache = stats


def get_live_stats_cache() -> dict[str, Any]:
    return _live_stats_cache


async def collect_dashboard_stats(
    *,
    scraper_stats: Optional[dict[str, Any]] = None,
    photo_worker: Optional[dict[str, Any]] = None,
    building_photo_worker: Optional[dict[str, Any]] = None,
    terremoto_cycles: int = 0,
    cameras_online: int = 0,
    cameras_total: int = 0,
) -> dict[str, Any]:
    async with async_session_factory() as session:
        total_local = await session.scalar(select(func.count()).select_from(MissingVictim)) or 0
        desaparecido = (
            await session.scalar(
                select(func.count()).where(MissingVictim.status == MissingStatus.DESAPARECIDO)
            )
            or 0
        )
        localizado = (
            await session.scalar(
                select(func.count()).where(MissingVictim.status == MissingStatus.LOCALIZADO)
            )
            or 0
        )
        fallecido = (
            await session.scalar(
                select(func.count()).where(MissingVictim.status == MissingStatus.FALLECIDO)
            )
            or 0
        )

    fotos = await get_photo_stats()
    edificios_fotos = await get_building_photo_stats()

    emergencias: dict[str, Any] = {}
    if EMERGENCIAS_CONFIG.exists():
        raw = json.loads(EMERGENCIAS_CONFIG.read_text(encoding="utf-8"))
        emergencias = {
            "actualizado": raw.get("actualizado"),
            "operadoras": len(raw.get("telefonos_emergencia", [])),
            "zonas": len(raw.get("zonas", [])),
            "nacional": len(raw.get("nacional", [])),
            "telefonos_emergencia": raw.get("telefonos_emergencia", []),
        }

    live = get_live_stats_cache()

    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "local": {
            "total": total_local,
            "desaparecido": desaparecido,
            "localizado": localizado,
            "fallecido": fallecido,
        },
        "live": live,
        "fotos": fotos,
        "edificios_fotos": edificios_fotos,
        "emergencias": emergencias,
        "workers": {
            "scraper": scraper_stats or {},
            "photo": photo_worker or {},
            "building_photo": building_photo_worker or {},
            "terremoto_cycles": terremoto_cycles,
        },
        "camaras": {
            "total": cameras_total,
            "en_vivo": cameras_online,
        },
    }


async def broadcast_dashboard_stats(**kwargs: Any) -> dict[str, Any]:
    data = await collect_dashboard_stats(**kwargs)
    await missing_updates_bus.publish("dashboard_stats", data)
    await victim_room_manager.broadcast("dashboard_stats", data)
    return data