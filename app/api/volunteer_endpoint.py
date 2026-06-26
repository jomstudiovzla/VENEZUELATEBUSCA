"""Voluntarios: testimonios, GPS en vivo, traslados."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user, require_role
from app.core.connection_manager import dashboard_ws
from app.core.database import get_session
from app.core.victims_database import get_victims_session, normalize_cedula
from app.models.models import LivePosition, MissingVictim, User, UserRole, VolunteerTestimony
from app.services.firebase_bridge import push_family_search, push_testimony_to_firebase

logger = logging.getLogger(__name__)
router = APIRouter(tags=["voluntarios"])


class TestimonyCreate(BaseModel):
    heard_name: str = Field(..., min_length=2)
    heard_cedula: Optional[str] = None
    location_text: str = Field(..., min_length=3)
    lat: float = Field(..., ge=-90, le=90)
    lng: float = Field(..., ge=-180, le=180)
    gps_accuracy_m: Optional[float] = None
    destination_type: Optional[str] = None
    destination_id: Optional[str] = None
    destination_name: Optional[str] = None
    estado_persona: Optional[str] = None
    notes: Optional[str] = None


class PositionUpdate(BaseModel):
    lat: float
    lng: float
    accuracy_m: Optional[float] = None
    heading: Optional[float] = None
    mission_id: Optional[str] = None


class FamilySearchCreate(BaseModel):
    seeker_name: str
    seeker_contact: str
    missing_person_name: str
    description: str = ""
    last_seen_location: Optional[str] = None


async def _match_victim(session: AsyncSession, name: str, cedula: str | None) -> tuple[int | None, float]:
    name_col = func.coalesce(MissingVictim.nombre_completo, MissingVictim.full_name)
    clauses = [name_col.ilike(f"%{name.strip()}%")]
    cedula_norm = normalize_cedula(cedula or "")
    if cedula_norm:
        clauses.append(MissingVictim.cedula == cedula_norm)
    row = (
        await session.execute(
            select(MissingVictim).where(or_(*clauses)).order_by(MissingVictim.id.desc()).limit(1)
        )
    ).scalar_one_or_none()
    if not row:
        return None, 0.0
    score = 1.0 if cedula_norm and row.cedula == cedula_norm else 0.75
    return row.id, score


def _testimony_card(t: VolunteerTestimony) -> dict[str, Any]:
    return {
        "id": t.id,
        "volunteer_name": t.volunteer_name,
        "heard_name": t.heard_name,
        "heard_cedula": t.heard_cedula,
        "location_text": t.location_text,
        "lat": t.lat,
        "lng": t.lng,
        "gps_accuracy_m": t.gps_accuracy_m,
        "destination_type": t.destination_type,
        "destination_id": t.destination_id,
        "destination_name": t.destination_name,
        "estado_persona": t.estado_persona,
        "notes": t.notes,
        "matched_victim_id": t.matched_victim_id,
        "match_score": t.match_score,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "maps_url": f"https://www.google.com/maps/search/?api=1&query={t.lat},{t.lng}",
    }


@router.post("/api/volunteer/testimony", status_code=201)
async def create_testimony(
    payload: TestimonyCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    victims_session: AsyncSession = Depends(get_victims_session),
):
    require_role(
        user,
        UserRole.SUPER_ADMIN.value,
        UserRole.ADMIN.value,
        UserRole.VOLUNTARIO.value,
        UserRole.PARAMEDICO.value,
    )
    victim_id, score = await _match_victim(victims_session, payload.heard_name, payload.heard_cedula)
    testimony = VolunteerTestimony(
        volunteer_id=user.id,
        volunteer_name=user.display_name,
        heard_name=payload.heard_name.strip(),
        heard_cedula=normalize_cedula(payload.heard_cedula or "") or None,
        location_text=payload.location_text.strip(),
        lat=payload.lat,
        lng=payload.lng,
        gps_accuracy_m=payload.gps_accuracy_m,
        destination_type=payload.destination_type,
        destination_id=payload.destination_id,
        destination_name=payload.destination_name,
        estado_persona=payload.estado_persona,
        notes=payload.notes,
        matched_victim_id=victim_id,
        match_score=score if victim_id else None,
    )
    session.add(testimony)
    await session.flush()
    card = _testimony_card(testimony)
    await dashboard_ws.broadcast("testimony_created", card)
    if victim_id:
        await dashboard_ws.broadcast("testimony_match", {**card, "matched_victim_id": victim_id})
    fb_ok = await push_testimony_to_firebase(card)
    if fb_ok:
        testimony.firebase_pushed = True
    return card


@router.get("/api/volunteer/testimonies")
async def list_testimonies(
    q: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(VolunteerTestimony).order_by(VolunteerTestimony.created_at.desc())
    if q:
        stmt = stmt.where(
            or_(
                VolunteerTestimony.heard_name.ilike(f"%{q}%"),
                VolunteerTestimony.heard_cedula.ilike(f"%{q}%"),
                VolunteerTestimony.location_text.ilike(f"%{q}%"),
            )
        )
    rows = (await session.execute(stmt.limit(limit))).scalars().all()
    return {"items": [_testimony_card(t) for t in rows], "count": len(rows)}


@router.post("/api/volunteer/position")
async def update_position(
    payload: PositionUpdate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    require_role(
        user,
        UserRole.SUPER_ADMIN.value,
        UserRole.ADMIN.value,
        UserRole.VOLUNTARIO.value,
        UserRole.PARAMEDICO.value,
    )
    pos = await session.scalar(select(LivePosition).where(LivePosition.user_id == user.id))
    if pos:
        pos.lat = payload.lat
        pos.lng = payload.lng
        pos.accuracy_m = payload.accuracy_m
        pos.heading = payload.heading
        pos.mission_id = payload.mission_id
        pos.updated_at = datetime.now(timezone.utc)
    else:
        pos = LivePosition(
            user_id=user.id,
            display_name=user.display_name,
            role=user.role,
            lat=payload.lat,
            lng=payload.lng,
            accuracy_m=payload.accuracy_m,
            heading=payload.heading,
            mission_id=payload.mission_id,
        )
        session.add(pos)
    await session.flush()
    data = {
        "user_id": user.id,
        "display_name": user.display_name,
        "role": user.role,
        "lat": payload.lat,
        "lng": payload.lng,
        "accuracy_m": payload.accuracy_m,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    await dashboard_ws.broadcast("live_position", data)
    return data


@router.get("/api/volunteer/positions")
async def list_positions(session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(LivePosition).order_by(LivePosition.updated_at.desc()))).scalars().all()
    return {
        "items": [
            {
                "user_id": p.user_id,
                "display_name": p.display_name,
                "role": p.role,
                "lat": p.lat,
                "lng": p.lng,
                "accuracy_m": p.accuracy_m,
                "updated_at": p.updated_at.isoformat() if p.updated_at else None,
            }
            for p in rows
        ]
    }


@router.post("/api/familiar/search", status_code=201)
async def family_search(payload: FamilySearchCreate, session: AsyncSession = Depends(get_session)):
    from app.models.models import MissingReport, ReportStatus

    report = MissingReport(
        seeker_name=payload.seeker_name.strip(),
        seeker_contact=payload.seeker_contact.strip(),
        missing_person_name=payload.missing_person_name.strip(),
        description=payload.description or "Búsqueda desde portal familiar",
        last_seen_location=payload.last_seen_location,
    )
    session.add(report)
    await session.flush()
    fb_key = await push_family_search({
        "report_id": report.id,
        "seeker_name": report.seeker_name,
        "missing_person_name": report.missing_person_name,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    await dashboard_ws.broadcast(
        "family_search",
        {
            "id": report.id,
            "missing_person_name": report.missing_person_name,
            "seeker_name": report.seeker_name,
            "firebase_key": fb_key,
        },
    )
    return {"id": report.id, "status": ReportStatus.ACTIVO.value, "firebase_key": fb_key}