"""Sincroniza puntos de Punto de Apoyo + hospitales + locales en map_points."""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.connection_manager import dashboard_ws
from app.core.database import async_session_factory
from app.models.models import MapPoint, MapPointCategory, Shelter, ShelterType, VerificationStatus

logger = logging.getLogger(__name__)

_CITY_COORDS: dict[str, tuple[float, float]] = {
    "caracas": (10.4806, -66.9036),
    "valencia": (10.1620, -68.0077),
    "maracaibo": (10.6668, -71.6125),
    "barquisimeto": (10.0647, -69.3570),
    "maracay": (10.2469, -67.5958),
    "ciudad guayana": (8.2890, -62.7300),
    "maturín": (9.7457, -63.1832),
    "barcelona": (10.1362, -64.6862),
    "san cristóbal": (7.7669, -72.2250),
    "mérida": (8.5897, -71.1561),
    "cumaná": (10.4530, -64.1826),
    "coro": (11.4045, -69.6737),
    "la guaira": (10.5995, -66.9346),
    "altamira": (10.4989, -66.8483),
    "distrito capital": (10.4806, -66.9036),
}


def _guess_coords(city: str, address: str, state: str) -> tuple[float, float] | None:
    for text in (city, address, state):
        if not text:
            continue
        key = text.strip().lower()
        for name, coords in _CITY_COORDS.items():
            if name in key:
                return coords
    return None

_CATEGORY_MAP = {
    "energia": MapPointCategory.ENERGIA.value,
    "signal": MapPointCategory.SENAL.value,
    "senal": MapPointCategory.SENAL.value,
    "supplies": MapPointCategory.SUMINISTROS.value,
    "suministros": MapPointCategory.SUMINISTROS.value,
    "medical": MapPointCategory.MEDICA.value,
    "medica": MapPointCategory.MEDICA.value,
    "danger": MapPointCategory.PELIGRO.value,
    "peligro": MapPointCategory.PELIGRO.value,
    "mobility": MapPointCategory.MOVILIDAD.value,
    "movilidad": MapPointCategory.MOVILIDAD.value,
}


async def _upsert_point(session: AsyncSession, data: dict[str, Any]) -> bool:
    ext_id = data["external_id"]
    source = data["source"]
    existing = await session.scalar(
        select(MapPoint).where(MapPoint.source == source, MapPoint.external_id == ext_id)
    )
    if existing:
        for k, v in data.items():
            if k not in ("external_id", "source", "created_at"):
                setattr(existing, k, v)
        existing.synced_at = datetime.now(timezone.utc)
        return False
    session.add(MapPoint(**data))
    return True


async def sync_centros_js() -> int:
    created = 0
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(settings.punto_apoyo_centros_url)
        r.raise_for_status()
        text = r.text
    blocks = re.findall(r"\{[^{}]+\}", text)
    centros = []
    for block in blocks:
        if "lat:" not in block or "lng:" not in block:
            continue
        def _field(key: str) -> str:
            m = re.search(rf'{key}:\s*"([^"]*)"', block)
            return m.group(1) if m else ""
        def _num(key: str) -> float | None:
            m = re.search(rf"{key}:\s*([-+]?\d+\.?\d*)", block)
            return float(m.group(1)) if m else None
        lat, lng = _num("lat"), _num("lng")
        if lat is None or lng is None:
            continue
        centros.append({
            "org": _field("org") or "Centro de acopio",
            "addr": _field("addr"),
            "ciudad": _field("ciudad"),
            "acepta": _field("acepta"),
            "contacto": _field("contacto"),
            "lat": lat,
            "lng": lng,
        })
    if not centros:
        logger.warning("No se parsearon centros desde centros.js")
        return 0
    async with async_session_factory() as session:
        for i, c in enumerate(centros):
            ext = f"centros-{hashlib.md5(f'{c.get("org")}{c.get("addr")}{c.get("lat")}'.encode()).hexdigest()[:16]}"
            is_new = await _upsert_point(session, {
                "external_id": ext,
                "source": "punto_apoyo",
                "name": c.get("org", "Centro de acopio"),
                "category": MapPointCategory.ACOPIO.value,
                "point_type": "oficial",
                "address": c.get("addr", ""),
                "city": c.get("ciudad", ""),
                "description": c.get("acepta", ""),
                "contact": c.get("contacto", ""),
                "lat": float(c["lat"]),
                "lng": float(c["lng"]),
                "extra": c,
            })
            if is_new:
                created += 1
        await session.commit()
    return created


async def sync_hospitals_api() -> int:
    created = 0
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get("https://terremotovenezuela.app/api/hospitals")
        r.raise_for_status()
        data = r.json()
    hospitals = data.get("hospitals") or data if isinstance(data, list) else []
    async with async_session_factory() as session:
        for h in hospitals:
            lat = h.get("lat") or h.get("latitude")
            lng = h.get("lng") or h.get("longitude")
            name = h.get("name") or h.get("nombre") or "Hospital"
            city = h.get("municipality") or h.get("city") or h.get("ciudad", "")
            state = h.get("state") or h.get("estado", "")
            address = h.get("address") or h.get("direccion", "")
            if lat is None or lng is None:
                guessed = _guess_coords(city, address, state)
                if not guessed:
                    continue
                lat, lng = guessed
            ext = h.get("id") or f"hosp-{hashlib.md5(name.encode()).hexdigest()[:16]}"
            is_new = await _upsert_point(session, {
                "external_id": ext,
                "source": "terremoto_ve",
                "name": name,
                "category": MapPointCategory.HOSPITAL.value,
                "address": h.get("address") or h.get("direccion", ""),
                "city": h.get("city") or h.get("ciudad", ""),
                "state": h.get("state") or h.get("estado", ""),
                "lat": float(lat),
                "lng": float(lng),
                "extra": h,
            })
            if is_new:
                created += 1
        await session.commit()
    return created


async def sync_supabase_reports() -> int:
    key = settings.punto_apoyo_supabase_key
    if not key:
        return 0
    created = 0
    url = f"{settings.punto_apoyo_supabase_url.rstrip('/')}/rest/v1/reports"
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, headers=headers, params={"select": "*", "order": "created_at.desc", "limit": "500"})
        if r.status_code >= 400:
            logger.warning("Supabase reports: HTTP %s", r.status_code)
            return 0
        rows = r.json()
    async with async_session_factory() as session:
        for row in rows:
            lat, lng = row.get("lat"), row.get("lng")
            if lat is None or lng is None:
                continue
            cat_raw = (row.get("category") or row.get("tipo") or "otro").lower()
            cat = _CATEGORY_MAP.get(cat_raw, MapPointCategory.OTRO.value)
            if row.get("report_type") == "offer" or row.get("tipo_reporte") == "ofrece":
                cat = MapPointCategory.OFRECEN.value
            elif row.get("report_type") == "need" or row.get("tipo_reporte") == "solicita":
                cat = MapPointCategory.SOLICITAN.value
            ext = str(row.get("id") or hashlib.md5(f"{lat}{lng}{cat}".encode()).hexdigest()[:16])
            is_new = await _upsert_point(session, {
                "external_id": ext,
                "source": "punto_apoyo_report",
                "name": row.get("note") or row.get("nota") or cat,
                "category": cat,
                "point_type": row.get("report_type") or row.get("tipo_reporte"),
                "description": row.get("note") or row.get("nota", ""),
                "lat": float(lat),
                "lng": float(lng),
                "confirmations": int(row.get("confirmations") or row.get("confirmaciones") or 0),
                "extra": row,
            })
            if is_new:
                created += 1
        await session.commit()
    return created


async def sync_local_shelters() -> int:
    created = 0
    type_map = {
        ShelterType.ACOPIO: MapPointCategory.ACOPIO.value,
        ShelterType.HOSPITAL: MapPointCategory.HOSPITAL.value,
        ShelterType.REFUGIO: MapPointCategory.REFUGIO.value,
    }
    async with async_session_factory() as session:
        rows = (
            await session.execute(
                select(Shelter).where(
                    Shelter.is_active == True,  # noqa: E712
                    Shelter.verification_status == VerificationStatus.VERIFICADO.value,
                )
            )
        ).scalars().all()
        for s in rows:
            is_new = await _upsert_point(session, {
                "external_id": s.id,
                "source": "red_esperanza",
                "name": s.name,
                "category": type_map.get(s.shelter_type, MapPointCategory.OTRO.value),
                "address": s.address,
                "city": s.city,
                "state": s.state,
                "description": s.description,
                "contact": s.contact_phone,
                "lat": s.lat,
                "lng": s.lng,
                "extra": {"shelter_type": s.shelter_type.value, "is_official": s.is_official},
            })
            if is_new:
                created += 1
        await session.commit()
    return created


async def sync_all_map_points() -> dict[str, int]:
    stats = {
        "centros": await sync_centros_js(),
        "hospitals": await sync_hospitals_api(),
        "reports": await sync_supabase_reports(),
        "local": await sync_local_shelters(),
    }
    total_new = sum(stats.values())
    if total_new:
        await dashboard_ws.broadcast("map_points_synced", stats)
    return stats