"""Centros de acopio — registro comunitario, verificación anti-fraude y tiempo real."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.acopio_auth import VERIFIER_ROLE, require_acopio_verifier
from app.core.connection_manager import dashboard_ws
from app.core.database import get_session
from app.models.models import Inventory, InventoryStatus, Shelter, ShelterType, VerificationStatus

logger = logging.getLogger(__name__)
router = APIRouter(tags=["acopio"])

_SERVICE_OPTIONS = [
    "alimentos", "agua", "ropa", "medicinas", "higiene", "cobijas",
    "logística", "transporte", "voluntariado", "herramientas", "abrigos",
    "pañales", "refugio temporal",
]


class AcopioSubmit(BaseModel):
    name: str = Field(..., min_length=3, max_length=255)
    address: str = Field(..., min_length=5, max_length=512)
    city: str = Field(..., min_length=2, max_length=128)
    state: str = Field(..., min_length=2, max_length=64)
    contact_phone: str = Field(..., min_length=7, max_length=64)
    lat: float = Field(..., ge=-90, le=90)
    lng: float = Field(..., ge=-180, le=180)
    description: str = Field(..., min_length=10, max_length=2000)
    services_offered: list[str] = Field(..., min_length=1, max_length=12)
    submitted_by_name: str = Field(..., min_length=2, max_length=255)
    submitted_by_contact: str = Field(..., min_length=7, max_length=64)
    submitted_by_org: Optional[str] = Field(None, max_length=255)


class AcopioVerify(BaseModel):
    action: str = Field(..., description="verificar | rechazar | suspender")
    verification_notes: Optional[str] = Field(None, max_length=1000)
    rejection_reason: Optional[str] = Field(None, max_length=1000)
    verifier_name: Optional[str] = Field(None, max_length=255)


def _shelter_base(s: Shelter) -> dict[str, Any]:
    occ_pct = round(s.current_occupancy / max(s.max_capacity, 1) * 100, 1)
    return {
        "id": s.id,
        "name": s.name,
        "shelter_type": s.shelter_type.value,
        "address": s.address,
        "city": s.city,
        "state": s.state,
        "description": s.description,
        "services_offered": s.services_offered or [],
        "contact_phone": s.contact_phone,
        "max_capacity": s.max_capacity,
        "current_occupancy": s.current_occupancy,
        "occupancy_pct": occ_pct,
        "lat": s.lat,
        "lng": s.lng,
        "maps_url": f"https://www.google.com/maps/search/?api=1&query={s.lat},{s.lng}",
        "is_official": s.is_official,
        "verification_status": s.verification_status,
        "submitted_by_name": s.submitted_by_name,
        "submitted_by_contact": s.submitted_by_contact,
        "submitted_by_org": s.submitted_by_org,
        "verification_notes": s.verification_notes,
        "verified_at": s.verified_at.isoformat() if s.verified_at else None,
        "verified_by": s.verified_by,
        "rejection_reason": s.rejection_reason,
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }


def _inventory_row(i: Inventory) -> dict[str, Any]:
    return {
        "id": i.id,
        "shelter_id": i.shelter_id,
        "item_name": i.item_name,
        "quantity": i.quantity,
        "unit": i.unit,
        "status": i.status.value,
        "notes": i.notes,
    }


def _acopio_traffic(items: list[Inventory]) -> tuple[str, str]:
    needed = sum(1 for i in items if i.status == InventoryStatus.NECESITADO)
    surplus = sum(1 for i in items if i.status == InventoryStatus.EXCEDENTE)
    if needed >= 2 and surplus == 0:
        return "red", "URGENTE"
    if needed == 0 and surplus > 0:
        return "green", "ABASTECIDO"
    if needed == 0 and not items:
        return "yellow", "SIN DATOS"
    return "yellow", "PARCIAL"


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


async def _find_duplicate(
    session: AsyncSession,
    name: str,
    lat: float,
    lng: float,
    *,
    exclude_id: str | None = None,
) -> Shelter | None:
    term = name.strip().lower()
    q = select(Shelter).where(
        Shelter.shelter_type == ShelterType.ACOPIO,
        Shelter.is_active == True,  # noqa: E712
        Shelter.verification_status.in_([
            VerificationStatus.PENDIENTE.value,
            VerificationStatus.VERIFICADO.value,
        ]),
        or_(
            Shelter.name.ilike(f"%{term}%"),
            func.lower(Shelter.name) == term,
        ),
    )
    if exclude_id:
        q = q.where(Shelter.id != exclude_id)
    rows = (await session.execute(q)).scalars().all()
    for row in rows:
        if _haversine_km(lat, lng, row.lat, row.lng) <= 0.8:
            return row
    return None


def _acopio_card(s: Shelter, inv: list[Inventory]) -> dict[str, Any]:
    color, label = _acopio_traffic(inv)
    needed = [i for i in inv if i.status == InventoryStatus.NECESITADO]
    surplus = [i for i in inv if i.status == InventoryStatus.EXCEDENTE]
    return {
        **_shelter_base(s),
        "status_color": color,
        "status_label": label,
        "inventory": [_inventory_row(i) for i in inv],
        "needed_items": [_inventory_row(i) for i in needed],
        "surplus_items": [_inventory_row(i) for i in surplus],
        "can_donate": bool(needed),
        "can_pickup": bool(surplus),
    }


async def _load_inventory_map(session: AsyncSession, shelter_ids: list[str]) -> dict[str, list[Inventory]]:
    if not shelter_ids:
        return {}
    rows = (await session.execute(select(Inventory).where(Inventory.shelter_id.in_(shelter_ids)))).scalars().all()
    out: dict[str, list[Inventory]] = {}
    for item in rows:
        out.setdefault(item.shelter_id, []).append(item)
    return out


@router.get("/api/acopio")
async def list_acopio_centers(
    state: Optional[str] = None,
    session: AsyncSession = Depends(get_session),
):
    """Solo centros verificados — visibles en el mapa público."""
    q = (
        select(Shelter)
        .where(
            Shelter.is_active == True,  # noqa: E712
            Shelter.shelter_type == ShelterType.ACOPIO,
            Shelter.verification_status == VerificationStatus.VERIFICADO.value,
        )
        .order_by(Shelter.state, Shelter.city, Shelter.name)
    )
    if state:
        q = q.where(Shelter.state.ilike(f"%{state.strip()}%"))
    shelters = (await session.execute(q)).scalars().all()
    by_shelter = await _load_inventory_map(session, [s.id for s in shelters])

    items = []
    states_set: set[str] = set()
    for s in shelters:
        if s.state:
            states_set.add(s.state)
        items.append(_acopio_card(s, by_shelter.get(s.id, [])))

    pending_count = await session.scalar(
        select(func.count()).where(
            Shelter.shelter_type == ShelterType.ACOPIO,
            Shelter.verification_status == VerificationStatus.PENDIENTE.value,
        )
    ) or 0

    return {
        "items": items,
        "count": len(items),
        "states": sorted(states_set),
        "pending_count": pending_count,
        "verifier_role_required": VERIFIER_ROLE,
        "service_options": _SERVICE_OPTIONS,
    }


@router.get("/api/acopio/pending")
async def list_pending_acopio(
    x_acopio_verifier: str | None = Header(None, alias="X-Acopio-Verifier"),
    session: AsyncSession = Depends(get_session),
):
    """Cola de moderación — solo verificadores autorizados."""
    require_acopio_verifier(x_acopio_verifier)
    rows = (
        await session.execute(
            select(Shelter)
            .where(
                Shelter.shelter_type == ShelterType.ACOPIO,
                Shelter.verification_status == VerificationStatus.PENDIENTE.value,
            )
            .order_by(Shelter.created_at.asc())
        )
    ).scalars().all()
    by_shelter = await _load_inventory_map(session, [s.id for s in rows])
    return {
        "items": [_acopio_card(s, by_shelter.get(s.id, [])) for s in rows],
        "count": len(rows),
    }


@router.post("/api/acopio/submit", status_code=202)
async def submit_acopio_center(payload: AcopioSubmit, session: AsyncSession = Depends(get_session)):
    """Registro comunitario — queda pendiente hasta verificación."""
    services = [s.strip().lower() for s in payload.services_offered if s.strip()]
    invalid = [s for s in services if s not in _SERVICE_OPTIONS]
    if invalid:
        raise HTTPException(400, f"Servicios inválidos: {', '.join(invalid)}")

    dup = await _find_duplicate(session, payload.name, payload.lat, payload.lng)
    if dup:
        raise HTTPException(
            409,
            f"Ya existe un centro similar cerca: «{dup.name}» ({dup.verification_status}). "
            "Si es el mismo punto, contacta al verificador.",
        )

    shelter = Shelter(
        name=payload.name.strip(),
        shelter_type=ShelterType.ACOPIO,
        address=payload.address.strip(),
        city=payload.city.strip(),
        state=payload.state.strip(),
        contact_phone=payload.contact_phone.strip(),
        description=payload.description.strip(),
        services_offered=services,
        lat=payload.lat,
        lng=payload.lng,
        max_capacity=500,
        current_occupancy=0,
        is_official=False,
        verification_status=VerificationStatus.PENDIENTE.value,
        submitted_by_name=payload.submitted_by_name.strip(),
        submitted_by_contact=payload.submitted_by_contact.strip(),
        submitted_by_org=(payload.submitted_by_org or "").strip() or None,
    )
    session.add(shelter)
    await session.flush()

    card = _acopio_card(shelter, [])
    await dashboard_ws.broadcast(
        "acopio_submitted",
        {
            "id": shelter.id,
            "name": shelter.name,
            "city": shelter.city,
            "state": shelter.state,
            "submitted_by_name": shelter.submitted_by_name,
            "verification_status": shelter.verification_status,
        },
    )
    logger.info("Nuevo acopio pendiente: %s (%s)", shelter.name, shelter.id)
    return {
        "status": "pendiente",
        "message": "Centro registrado. Un verificador debe aprobarlo antes de aparecer en el mapa público.",
        "center": card,
    }


@router.patch("/api/acopio/{center_id}/verification")
async def verify_acopio_center(
    center_id: str,
    payload: AcopioVerify,
    x_acopio_verifier: str | None = Header(None, alias="X-Acopio-Verifier"),
    session: AsyncSession = Depends(get_session),
):
    """Aprobar, rechazar o suspender un centro — anti-fraude."""
    require_acopio_verifier(x_acopio_verifier)
    shelter = await session.get(Shelter, center_id)
    if not shelter or shelter.shelter_type != ShelterType.ACOPIO:
        raise HTTPException(404, "Centro de acopio no encontrado")

    action = payload.action.strip().lower()
    verifier = (payload.verifier_name or "Verificador").strip()
    now = datetime.now(timezone.utc)

    if action in ("verificar", "aprobar", "approve"):
        if shelter.verification_status == VerificationStatus.VERIFICADO.value:
            raise HTTPException(400, "Este centro ya está verificado")
        dup = await _find_duplicate(session, shelter.name, shelter.lat, shelter.lng, exclude_id=shelter.id)
        if dup and dup.verification_status == VerificationStatus.VERIFICADO.value:
            raise HTTPException(
                409,
                f"No se puede verificar: ya existe «{dup.name}» verificado a menos de 800 m",
            )
        shelter.verification_status = VerificationStatus.VERIFICADO.value
        shelter.verified_at = now
        shelter.verified_by = verifier
        shelter.verification_notes = payload.verification_notes
        shelter.rejection_reason = None
        event = "acopio_verified"
    elif action in ("rechazar", "reject"):
        if not payload.rejection_reason or len(payload.rejection_reason.strip()) < 5:
            raise HTTPException(400, "Indique el motivo del rechazo (rejection_reason)")
        shelter.verification_status = VerificationStatus.RECHAZADO.value
        shelter.rejection_reason = payload.rejection_reason.strip()
        shelter.verification_notes = payload.verification_notes
        shelter.verified_at = now
        shelter.verified_by = verifier
        event = "acopio_rejected"
    elif action in ("suspender", "suspend"):
        shelter.verification_status = VerificationStatus.SUSPENDIDO.value
        shelter.rejection_reason = payload.rejection_reason or "Suspendido por verificador"
        shelter.verification_notes = payload.verification_notes
        shelter.verified_at = now
        shelter.verified_by = verifier
        event = "acopio_suspended"
    else:
        raise HTTPException(400, "Acción inválida. Use: verificar, rechazar o suspender")

    await session.flush()
    inv = (await session.execute(select(Inventory).where(Inventory.shelter_id == shelter.id))).scalars().all()
    card = _acopio_card(shelter, inv)

    await dashboard_ws.broadcast(
        event,
        {
            "id": shelter.id,
            "name": shelter.name,
            "city": shelter.city,
            "state": shelter.state,
            "verification_status": shelter.verification_status,
            "verified_by": shelter.verified_by,
            "rejection_reason": shelter.rejection_reason,
        },
    )
    return {"status": shelter.verification_status, "center": card}