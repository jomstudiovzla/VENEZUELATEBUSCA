"""Sincronización en tiempo real desde desaparecidosterremotovenezuela.com."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.connection_manager import dashboard_ws
from app.core.victims_database import extract_cedula_from_text, victims_session_factory
from app.models.models import MissingVictim, VictimStatus

logger = logging.getLogger(__name__)

_ESTADO_MAP = {
    "sin-contacto": VictimStatus.DESAPARECIDO.value,
    "desaparecido": VictimStatus.DESAPARECIDO.value,
    "localizado": VictimStatus.LOCALIZADO.value,
    "fallecido": VictimStatus.FALLECIDO.value,
}

_HEIGHT_RE = re.compile(
    r"(?:estatura|mide|altura)\s*[:\-]?\s*(\d+[.,]\d+)\s*(?:m|metros?)?|"
    r"(\d+[.,]\d+)\s*(?:m|metros?)\s*de\s*estatura|"
    r"(\d{3})\s*cm",
    re.IGNORECASE,
)


def _parse_height(descripcion: str | None) -> Optional[float]:
    if not descripcion:
        return None
    match = _HEIGHT_RE.search(descripcion)
    if not match:
        return None
    raw = next(g for g in match.groups() if g)
    value = float(raw.replace(",", "."))
    return value * 100 if value < 3 else value


def _clean_nombre(nombre: str) -> tuple[str, Optional[str]]:
    """Separa nombre y cédula cuando vienen en el mismo campo (ej. 'Nombre\\t17.709.218')."""
    parts = re.split(r"[\t|]+", nombre.strip())
    name = parts[0].strip()
    cedula = None
    for part in parts[1:]:
        digits = re.sub(r"\D", "", part)
        if 6 <= len(digits) <= 10:
            cedula = digits
            break
    if not cedula:
        cedula = extract_cedula_from_text(nombre, name)
    return name, cedula


def _map_api_record(raw: dict[str, Any]) -> dict[str, Any]:
    nombre, cedula_from_name = _clean_nombre(raw.get("nombre") or "Desconocido")
    descripcion = (raw.get("descripcion") or "").strip()
    cedula = cedula_from_name or extract_cedula_from_text(descripcion, nombre)
    estado_api = (raw.get("estado") or "sin-contacto").lower()
    height = _parse_height(descripcion)
    foto = (raw.get("foto") or "").strip()
    ref_path = None
    if foto:
        fname = Path(foto).name
        ref_path = f"reference_photos/{fname}"

    now = datetime.now(timezone.utc)
    return {
        "external_id": raw["id"],
        "full_name": nombre,
        "nombre_completo": nombre,
        "cedula": cedula,
        "age": raw.get("edad"),
        "edad": raw.get("edad"),
        "last_known_location": raw.get("ubicacion"),
        "descripcion_fisica": descripcion or None,
        "distinguishing_marks": descripcion or None,
        "photo_url": foto or None,
        "reference_photo_path": ref_path,
        "estatura_estimada_cm": height,
        "height_cm": height,
        "status": _ESTADO_MAP.get(estado_api, VictimStatus.DESAPARECIDO.value),
        "source_estado": estado_api,
        "source_updated_at": raw.get("updatedAt"),
        "reporter_contact": raw.get("contacto"),
        "created_at": now,
        "updated_at": now,
    }


async def _upsert_victim(session: AsyncSession, data: dict[str, Any]) -> tuple[MissingVictim, str]:
    existing = (
        await session.execute(
            select(MissingVictim).where(MissingVictim.external_id == data["external_id"])
        )
    ).scalar_one_or_none()

    if existing:
        changed = False
        for key, value in data.items():
            if key in ("external_id", "created_at"):
                continue
            if value is not None and getattr(existing, key, None) != value:
                setattr(existing, key, value)
                changed = True
        if changed:
            existing.updated_at = datetime.now(timezone.utc)
            return existing, "updated"
        return existing, "unchanged"

    victim = MissingVictim(**data)
    session.add(victim)
    await session.flush()
    return victim, "created"


async def fetch_api_page(
    client: httpx.AsyncClient,
    page: int,
    page_size: int = 100,
    estado: Optional[str] = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {"page": page, "pageSize": page_size}
    if estado and estado != "todos":
        params["estado"] = estado
    resp = await client.get(f"{settings.victims_api_url}/personas", params=params, timeout=30.0)
    resp.raise_for_status()
    return resp.json()


async def sync_victims_incremental(max_pages: int | None = None) -> dict[str, Any]:
    """Sincroniza páginas recientes de la API externa hacia ojo_de_dios.db."""
    pages = max_pages or settings.victims_sync_max_pages
    stats = {"created": 0, "updated": 0, "unchanged": 0, "pages": 0, "api_total": 0, "api_offline": False}

    try:
        async with httpx.AsyncClient() as client:
            async with victims_session_factory() as session:
                for page in range(1, pages + 1):
                    payload = await fetch_api_page(client, page, settings.victims_sync_page_size)
                    items = payload.get("items") or []
                    if page == 1:
                        stats["api_total"] = payload.get("total") or payload.get("counts", {}).get("total", 0)
                    if not items:
                        break

                    for raw in items:
                        data = _map_api_record(raw)
                        victim, action = await _upsert_victim(session, data)
                        stats[action] += 1
                        if action == "created":
                            await dashboard_ws.broadcast(
                                "victim_synced",
                                {
                                    "action": "new",
                                    "id": victim.id,
                                    "nombre_completo": victim.nombre_completo or victim.full_name,
                                    "estado": victim.status,
                                },
                            )
                        elif action == "updated":
                            await dashboard_ws.broadcast(
                                "victim_synced",
                                {
                                    "action": "updated",
                                    "id": victim.id,
                                    "nombre_completo": victim.nombre_completo or victim.full_name,
                                    "estado": victim.status,
                                },
                            )

                    stats["pages"] = page
                    await session.commit()

                    if page >= (payload.get("totalPages") or page):
                        break

    except Exception:
        stats["api_offline"] = True
        logger.warning("API de desaparecidos no disponible; usando BD local", exc_info=True)

    logger.info(
        "Sync víctimas | páginas=%d | nuevos=%d | actualizados=%d | total_api=%d",
        stats["pages"],
        stats["created"],
        stats["updated"],
        stats["api_total"],
    )
    return stats


async def fetch_api_stats() -> dict[str, Any]:
    try:
        async with httpx.AsyncClient() as client:
            payload = await fetch_api_page(client, 1, 1)
            counts = payload.get("counts") or {}
            return {
                "total": counts.get("total") or payload.get("total") or 0,
                "sin_contacto": counts.get("sinContacto", 0),
                "localizado": counts.get("localizado", 0),
                "api_online": True,
            }
    except Exception:
        return {"total": 0, "sin_contacto": 0, "localizado": 0, "api_online": False}