"""Mapa unificado — Punto de Apoyo + locales + posiciones en vivo."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.models.models import LivePosition, MapPoint
from app.services.punto_apoyo_sync import sync_all_map_points

router = APIRouter(tags=["mapa"])

_CATEGORY_COLORS = {
    "acopio": "#3b82f6",
    "hospital": "#ef4444",
    "refugio": "#8b5cf6",
    "energia": "#f59e0b",
    "senal": "#06b6d4",
    "suministros": "#10b981",
    "medica": "#ec4899",
    "peligro": "#dc2626",
    "movilidad": "#6366f1",
    "ofrecen": "#22c55e",
    "solicitan": "#f97316",
    "plaza": "#a855f7",
    "otro": "#9ca3af",
}


def _point_card(p: MapPoint) -> dict:
    return {
        "id": p.id,
        "external_id": p.external_id,
        "source": p.source,
        "name": p.name,
        "category": p.category,
        "point_type": p.point_type,
        "address": p.address,
        "city": p.city,
        "state": p.state,
        "description": p.description,
        "contact": p.contact,
        "lat": p.lat,
        "lng": p.lng,
        "color": _CATEGORY_COLORS.get(p.category, "#9ca3af"),
        "confirmations": p.confirmations,
        "maps_url": f"https://www.google.com/maps/search/?api=1&query={p.lat},{p.lng}",
        "extra": p.extra,
    }


@router.get("/api/map/points")
async def get_map_points(
    category: Optional[str] = Query(None),
    city: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
):
    q = select(MapPoint).where(MapPoint.is_active == True).order_by(MapPoint.category, MapPoint.name)  # noqa: E712
    if category:
        q = q.where(MapPoint.category == category.lower())
    if city:
        q = q.where(MapPoint.city.ilike(f"%{city}%"))
    if source:
        q = q.where(MapPoint.source == source)
    rows = (await session.execute(q)).scalars().all()
    categories = sorted({p.category for p in rows})
    return {
        "items": [_point_card(p) for p in rows],
        "count": len(rows),
        "categories": categories,
        "category_colors": _CATEGORY_COLORS,
    }


@router.get("/api/map/live")
async def get_live_map(session: AsyncSession = Depends(get_session)):
    points = (await session.execute(select(MapPoint).where(MapPoint.is_active == True))).scalars().all()  # noqa: E712
    positions = (await session.execute(select(LivePosition))).scalars().all()
    return {
        "points": [_point_card(p) for p in points],
        "volunteers": [
            {
                "user_id": v.user_id,
                "display_name": v.display_name,
                "role": v.role,
                "lat": v.lat,
                "lng": v.lng,
                "accuracy_m": v.accuracy_m,
            }
            for v in positions
        ],
        "sync_interval_ms": 250,
    }


@router.post("/api/map/sync", status_code=202)
async def trigger_map_sync():
    stats = await sync_all_map_points()
    return {"status": "synced", **stats}