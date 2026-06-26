"""API REST — Red de Esperanza (offline-first + logística)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.connection_manager import dashboard_ws
from app.core.database import get_session
from app.models.models import (
    Inventory,
    InventoryStatus,
    MissingReport,
    Mission,
    MissionStatus,
    ReportStatus,
    Shelter,
    ShelterType,
    Survivor,
)
from app.services.semantic_matcher import run_matching_cycle

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Schemas ──────────────────────────────────────────────────────────────────

class ShelterCreate(BaseModel):
    name: str
    shelter_type: ShelterType = ShelterType.REFUGIO
    address: str
    city: str
    contact_phone: Optional[str] = None
    max_capacity: int = 100
    lat: float
    lng: float


class InventoryCreate(BaseModel):
    shelter_id: str
    item_name: str
    quantity: int
    unit: str = "unidades"
    status: InventoryStatus
    notes: Optional[str] = None


class InventoryUpdate(BaseModel):
    quantity: Optional[int] = None
    status: Optional[InventoryStatus] = None
    notes: Optional[str] = None


class SurvivorCreate(BaseModel):
    shelter_id: str
    name: str = "Desconocido"
    estado_medico: str = "estable"
    caracteristicas_fisicas: dict[str, Any] = Field(default_factory=dict)
    client_sync_id: Optional[str] = None


class SurvivorSyncItem(BaseModel):
    client_sync_id: str
    shelter_id: str
    name: str = "Desconocido"
    estado_medico: str = "estable"
    caracteristicas_fisicas: dict[str, Any] = Field(default_factory=dict)
    registered_at: Optional[str] = None


class SurvivorSyncBatch(BaseModel):
    items: list[SurvivorSyncItem]


class MissingReportCreate(BaseModel):
    seeker_name: str
    seeker_contact: str
    missing_person_name: str
    description: str
    last_seen_location: Optional[str] = None
    physical_traits: Optional[dict[str, Any]] = None


class MissionCreate(BaseModel):
    title: str
    description: str
    mission_type: str = "rescate"
    address: str
    lat: float
    lng: float
    shelter_id: Optional[str] = None
    priority: int = 2


class MissionAccept(BaseModel):
    volunteer_name: str
    volunteer_contact: str


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _after_survivor(session: AsyncSession, survivor: Survivor) -> None:
    shelter = await session.get(Shelter, survivor.shelter_id)
    if shelter:
        shelter.current_occupancy += 1
    await session.flush()
    await session.commit()

    matches = await run_matching_cycle(session)
    await session.commit()

    for m in matches:
        await dashboard_ws.broadcast(
            "possible_match",
            {
                "report_name": m.report_name,
                "survivor_name": m.survivor_name,
                "score": m.score,
                "tokens": m.matched_tokens,
            },
        )
    await dashboard_ws.broadcast(
        "survivor_registered",
        {
            "id": survivor.id,
            "name": survivor.name,
            "shelter": shelter.name if shelter else "",
            "estado_medico": survivor.estado_medico,
            "caracteristicas": survivor.caracteristicas_fisicas,
        },
    )


def _shelter_dict(s: Shelter) -> dict:
    occ_pct = round(s.current_occupancy / max(s.max_capacity, 1) * 100, 1)
    return {
        "id": s.id,
        "name": s.name,
        "shelter_type": s.shelter_type.value,
        "address": s.address,
        "city": s.city,
        "contact_phone": s.contact_phone,
        "max_capacity": s.max_capacity,
        "current_occupancy": s.current_occupancy,
        "occupancy_pct": occ_pct,
        "lat": s.lat,
        "lng": s.lng,
    }


# ── Health & Dashboard ───────────────────────────────────────────────────────

@router.get("/health")
async def health():
    return {
        "status": "operational",
        "service": "red-de-esperanza",
        "mode": "logistica_humanitaria",
        "ws_clients": dashboard_ws.active_count,
    }


@router.get("/api/emergencias")
async def emergencias_venezuela():
    """Teléfonos y recursos de emergencia (config estática)."""
    path = Path(__file__).resolve().parents[2] / "config" / "emergencias_venezuela.json"
    if not path.is_file():
        raise HTTPException(404, "Configuración de emergencias no disponible")
    return json.loads(path.read_text(encoding="utf-8"))


@router.get("/api/dashboard")
async def dashboard(session: AsyncSession = Depends(get_session)):
    shelters = (await session.execute(select(Shelter).where(Shelter.is_active == True))).scalars().all()  # noqa: E712
    missions_open = await session.scalar(
        select(func.count()).where(Mission.status.in_([MissionStatus.ABIERTA, MissionStatus.ACEPTADA, MissionStatus.EN_CURSO]))
    ) or 0
    survivors_total = await session.scalar(select(func.count()).select_from(Survivor)) or 0
    reports_active = await session.scalar(
        select(func.count()).where(MissingReport.status.in_([ReportStatus.ACTIVO, ReportStatus.POSIBLE_MATCH]))
    ) or 0
    needed = await session.scalar(
        select(func.count()).where(Inventory.status == InventoryStatus.NECESITADO)
    ) or 0
    return {
        "shelters": len(shelters),
        "missions_active": missions_open,
        "survivors_total": survivors_total,
        "missing_reports_active": reports_active,
        "items_needed": needed,
    }


# ── Shelters ─────────────────────────────────────────────────────────────────

@router.get("/api/shelters")
async def list_shelters(session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(Shelter).where(Shelter.is_active == True).order_by(Shelter.name))).scalars().all()  # noqa: E712
    return {"items": [_shelter_dict(s) for s in rows]}


@router.post("/api/shelters", status_code=201)
async def create_shelter(payload: ShelterCreate, session: AsyncSession = Depends(get_session)):
    shelter = Shelter(**payload.model_dump())
    session.add(shelter)
    await session.flush()
    return _shelter_dict(shelter)


# ── Inventory ────────────────────────────────────────────────────────────────

@router.get("/api/inventory")
async def list_inventory(shelter_id: Optional[str] = None, session: AsyncSession = Depends(get_session)):
    q = select(Inventory).order_by(Inventory.updated_at.desc())
    if shelter_id:
        q = q.where(Inventory.shelter_id == shelter_id)
    rows = (await session.execute(q.limit(500))).scalars().all()
    return {
        "items": [
            {
                "id": i.id,
                "shelter_id": i.shelter_id,
                "item_name": i.item_name,
                "quantity": i.quantity,
                "unit": i.unit,
                "status": i.status.value,
                "notes": i.notes,
                "updated_at": i.updated_at.isoformat() if i.updated_at else None,
            }
            for i in rows
        ]
    }


@router.post("/api/inventory", status_code=201)
async def create_inventory(payload: InventoryCreate, session: AsyncSession = Depends(get_session)):
    if not await session.get(Shelter, payload.shelter_id):
        raise HTTPException(404, "Refugio no encontrado")
    item = Inventory(**payload.model_dump())
    session.add(item)
    await session.flush()
    shelter = await session.get(Shelter, payload.shelter_id)
    data = {
        "id": item.id,
        "shelter_id": item.shelter_id,
        "shelter_name": shelter.name if shelter else "",
        "item_name": item.item_name,
        "quantity": item.quantity,
        "status": item.status.value,
    }
    await dashboard_ws.broadcast("inventory_updated", data)
    return data


@router.patch("/api/inventory/{item_id}")
async def update_inventory(item_id: str, payload: InventoryUpdate, session: AsyncSession = Depends(get_session)):
    item = await session.get(Inventory, item_id)
    if not item:
        raise HTTPException(404, "Item no encontrado")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(item, k, v)
    await session.flush()
    shelter = await session.get(Shelter, item.shelter_id)
    data = {
        "id": item.id,
        "shelter_name": shelter.name if shelter else "",
        "item_name": item.item_name,
        "quantity": item.quantity,
        "status": item.status.value,
    }
    await dashboard_ws.broadcast("inventory_updated", data)
    return data


# ── Survivors (offline-first) ────────────────────────────────────────────────

@router.post("/survivors", status_code=201)
@router.post("/api/survivors", status_code=201)
async def register_survivor(
    payload: SurvivorCreate,
    session: AsyncSession = Depends(get_session),
):
    if not await session.get(Shelter, payload.shelter_id):
        raise HTTPException(404, "Refugio no encontrado")

    if payload.client_sync_id:
        existing = (
            await session.execute(select(Survivor).where(Survivor.client_sync_id == payload.client_sync_id))
        ).scalar_one_or_none()
        if existing:
            return {
                "id": existing.id,
                "sync_status": "already_synced",
                "client_sync_id": existing.client_sync_id,
            }

    survivor = Survivor(
        shelter_id=payload.shelter_id,
        name=payload.name or "Desconocido",
        estado_medico=payload.estado_medico,
        caracteristicas_fisicas=payload.caracteristicas_fisicas,
        client_sync_id=payload.client_sync_id,
        synced_offline=bool(payload.client_sync_id),
    )
    session.add(survivor)
    await session.flush()
    await _after_survivor(session, survivor)
    return {
        "id": survivor.id,
        "sync_status": "synced",
        "client_sync_id": survivor.client_sync_id,
        "offline_queued": False,
    }


@router.post("/api/sync/batch", status_code=201)
async def sync_survivors_batch(
    payload: SurvivorSyncBatch,
    session: AsyncSession = Depends(get_session),
):
    """Sincroniza cola offline de la PWA (idempotente por client_sync_id)."""
    results = []
    for item in payload.items:
        existing = (
            await session.execute(select(Survivor).where(Survivor.client_sync_id == item.client_sync_id))
        ).scalar_one_or_none()
        if existing:
            results.append({"client_sync_id": item.client_sync_id, "status": "duplicate", "id": existing.id})
            continue
        if not await session.get(Shelter, item.shelter_id):
            results.append({"client_sync_id": item.client_sync_id, "status": "error", "detail": "shelter_not_found"})
            continue
        survivor = Survivor(
            shelter_id=item.shelter_id,
            name=item.name,
            estado_medico=item.estado_medico,
            caracteristicas_fisicas=item.caracteristicas_fisicas,
            client_sync_id=item.client_sync_id,
            synced_offline=True,
        )
        session.add(survivor)
        await session.flush()
        await _after_survivor(session, survivor)
        results.append({"client_sync_id": item.client_sync_id, "status": "synced", "id": survivor.id})
    return {"synced": len([r for r in results if r["status"] == "synced"]), "results": results}


@router.get("/api/survivors")
async def list_survivors(limit: int = 50, session: AsyncSession = Depends(get_session)):
    rows = (
        await session.execute(select(Survivor).order_by(Survivor.registered_at.desc()).limit(min(limit, 200)))
    ).scalars().all()
    out = []
    for s in rows:
        shelter = await session.get(Shelter, s.shelter_id)
        out.append({
            "id": s.id,
            "name": s.name,
            "estado_medico": s.estado_medico,
            "caracteristicas_fisicas": s.caracteristicas_fisicas,
            "shelter_name": shelter.name if shelter else "",
            "matched_report_id": s.matched_report_id,
            "match_score": s.match_score,
            "registered_at": s.registered_at.isoformat() if s.registered_at else None,
        })
    return {"items": out}


# ── Missing Reports ──────────────────────────────────────────────────────────

@router.post("/api/missing-reports", status_code=201)
async def create_missing_report(payload: MissingReportCreate, session: AsyncSession = Depends(get_session)):
    report = MissingReport(**payload.model_dump())
    session.add(report)
    await session.flush()
    return {"id": report.id, "missing_person_name": report.missing_person_name, "status": report.status.value}


@router.get("/api/missing-reports")
async def list_missing_reports(session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(MissingReport).order_by(MissingReport.created_at.desc()).limit(200))).scalars().all()
    return {
        "items": [
            {
                "id": r.id,
                "missing_person_name": r.missing_person_name,
                "description": r.description,
                "seeker_name": r.seeker_name,
                "seeker_contact": r.seeker_contact,
                "status": r.status.value,
                "match_score": r.match_score,
                "matched_survivor_id": r.matched_survivor_id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


# ── Missions ─────────────────────────────────────────────────────────────────

@router.get("/api/missions")
async def list_missions(active_only: bool = True, session: AsyncSession = Depends(get_session)):
    q = select(Mission).order_by(Mission.priority.desc(), Mission.created_at.desc())
    if active_only:
        q = q.where(Mission.status != MissionStatus.COMPLETADA)
    rows = (await session.execute(q.limit(100))).scalars().all()
    return {
        "items": [
            {
                "id": m.id,
                "title": m.title,
                "description": m.description,
                "mission_type": m.mission_type,
                "address": m.address,
                "lat": m.lat,
                "lng": m.lng,
                "status": m.status.value,
                "priority": m.priority,
                "volunteer_name": m.volunteer_name,
                "volunteer_contact": m.volunteer_contact,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in rows
        ]
    }


@router.post("/api/missions", status_code=201)
async def create_mission(payload: MissionCreate, session: AsyncSession = Depends(get_session)):
    mission = Mission(**payload.model_dump())
    session.add(mission)
    await session.flush()
    data = {"id": mission.id, "title": mission.title, "address": mission.address, "priority": mission.priority}
    await dashboard_ws.broadcast("mission_created", data)
    return data


@router.patch("/api/missions/{mission_id}/accept")
async def accept_mission(mission_id: str, payload: MissionAccept, session: AsyncSession = Depends(get_session)):
    mission = await session.get(Mission, mission_id)
    if not mission:
        raise HTTPException(404, "Misión no encontrada")
    if mission.status != MissionStatus.ABIERTA:
        raise HTTPException(400, "La misión ya fue asignada")
    mission.status = MissionStatus.ACEPTADA
    mission.volunteer_name = payload.volunteer_name
    mission.volunteer_contact = payload.volunteer_contact
    mission.accepted_at = datetime.now(timezone.utc)
    await session.flush()
    data = {
        "id": mission.id,
        "title": mission.title,
        "volunteer_name": mission.volunteer_name,
        "address": mission.address,
    }
    await dashboard_ws.broadcast("mission_accepted", data)
    return data


# ── WebSocket ────────────────────────────────────────────────────────────────

@router.websocket("/ws/dashboard")
async def dashboard_websocket(websocket: WebSocket):
    conn_id = await dashboard_ws.connect(websocket)
    try:
        await websocket.send_json({
            "event": "connected",
            "data": {"message": "Tablero de Esperanza conectado"},
        })
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await dashboard_ws.disconnect(conn_id)