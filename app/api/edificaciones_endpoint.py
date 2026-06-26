"""Registro ciudadano de edificaciones — GIS humanitario."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.connection_manager import dashboard_ws
from app.core.database import get_session
from app.models.models import Building, TipoEstructura

router = APIRouter(tags=["edificaciones"])


class EdificacionReporte(BaseModel):
    nombre_edificio: str = Field(..., min_length=2, max_length=255)
    tipo_estructura: TipoEstructura
    direccion_texto: str = Field(..., min_length=3, max_length=512)
    latitud: float = Field(..., ge=-90, le=90)
    longitud: float = Field(..., ge=-180, le=180)
    necesidades_urgentes: Optional[str] = Field(None, max_length=4000)
    reportado_por: Optional[str] = Field(None, max_length=255)
    contacto_reportante: Optional[str] = Field(None, max_length=64)


def _marker_color(tipo: str) -> str:
    if tipo == TipoEstructura.COLAPSADO.value:
        return "red"
    if tipo in (TipoEstructura.HOSPITAL.value, TipoEstructura.REFUGIO.value):
        return "green"
    if tipo == TipoEstructura.CENTRO_ACOPIO.value:
        return "blue"
    return "gray"


def _serialize(b: Building) -> dict[str, Any]:
    return {
        "id": b.id,
        "nombre_edificio": b.nombre_edificio,
        "tipo_estructura": b.tipo_estructura,
        "direccion_texto": b.direccion_texto,
        "latitud": b.latitud,
        "longitud": b.longitud,
        "necesidades_urgentes": b.necesidades_urgentes,
        "estado_verificacion": b.estado_verificacion,
        "reportado_por": b.reportado_por,
        "contacto_reportante": b.contacto_reportante,
        "marker_color": _marker_color(b.tipo_estructura),
        "created_at": b.created_at.isoformat() if b.created_at else None,
    }


def _to_geojson(items: list[dict[str, Any]]) -> dict[str, Any]:
    features = []
    for it in items:
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [it["longitud"], it["latitud"]],
            },
            "properties": {k: v for k, v in it.items() if k not in ("latitud", "longitud")},
        })
    return {"type": "FeatureCollection", "features": features}


@router.post("/api/edificaciones/reportar")
async def reportar_edificacion(
    payload: EdificacionReporte,
    session: AsyncSession = Depends(get_session),
):
    building = Building(
        nombre_edificio=payload.nombre_edificio.strip(),
        tipo_estructura=payload.tipo_estructura.value,
        direccion_texto=payload.direccion_texto.strip(),
        latitud=payload.latitud,
        longitud=payload.longitud,
        necesidades_urgentes=(payload.necesidades_urgentes or "").strip() or None,
        reportado_por=(payload.reportado_por or "").strip() or None,
        contacto_reportante=(payload.contacto_reportante or "").strip() or None,
        estado_verificacion=False,
    )
    session.add(building)
    await session.flush()
    card = _serialize(building)
    await dashboard_ws.broadcast("edificacion_reportada", {**card, "pulse": True})
    return {"ok": True, "item": card, "message": "¡Reporte registrado! Fuerza Venezuela — Juntos Salvaremos Vidas"}


@router.get("/api/edificaciones/mapa")
async def mapa_edificaciones(
    session: AsyncSession = Depends(get_session),
    format: str = Query("geojson", pattern="^(geojson|list)$"),
    solo_verificados: bool = Query(False),
):
    q = select(Building).order_by(Building.created_at.desc())
    if solo_verificados:
        q = q.where(Building.estado_verificacion.is_(True))
    rows = (await session.execute(q)).scalars().all()
    items = [_serialize(b) for b in rows]
    if format == "list":
        return {"count": len(items), "items": items}
    return _to_geojson(items)


@router.patch("/api/edificaciones/{building_id}/verificar")
async def verificar_edificacion(
    building_id: str,
    session: AsyncSession = Depends(get_session),
):
    building = await session.get(Building, building_id)
    if not building:
        raise HTTPException(404, "Edificación no encontrada")
    building.estado_verificacion = True
    building.verified_at = datetime.now(timezone.utc)
    card = _serialize(building)
    await dashboard_ws.broadcast("edificacion_verificada", card)
    return {"ok": True, "item": card}