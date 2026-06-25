"""Rutas API del Centro de Comando SAR-DVI — solo fuentes consensuadas."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.connection_manager import command_center_manager
from app.core.database import async_session_factory, get_session
from app.core.paths import DRONE_FRAMES, EVIDENCE_VIDEOS, MEDICAL_PHOTOS, ROOT
from app.models.models import (
    CrowdsourcedEvidence,
    DroneTelemetry,
    EvidenceStatus,
    FeedStatus,
    MedicalTriage,
    MissingStatus,
    MissingVictim,
    SosSignal,
    SosVitalStatus,
    VoluntaryCamera,
)
from app.services.ai_processor import AIProcessor
from app.services.biometrics import BiometricsService
from app.video.processor import VideoProcessor

logger = logging.getLogger(__name__)
router = APIRouter()

ai_processor = AIProcessor()
biometrics = BiometricsService()
video_processor = VideoProcessor()

ALLOWED_IMAGE = {".jpg", ".jpeg", ".png", ".webp"}
ALLOWED_VIDEO = {".mp4", ".mov", ".webm", ".avi", ".mkv"}
MAX_BYTES = settings.max_upload_mb * 1024 * 1024


class SosPanicPayload(BaseModel):
    lat: float
    lng: float
    vital_status: SosVitalStatus
    message: Optional[str] = None
    contact_phone: Optional[str] = None
    device_token: Optional[str] = None


class VoluntaryCameraPayload(BaseModel):
    condominium_name: str
    contact_name: str
    contact_phone: str
    rtsp_url: str
    city: str
    zone: Optional[str] = None
    terms_accepted: bool = Field(..., description="Debe ser true para registrar la cámara")
    terms_version: str = "2026-06-01"


class DroneTelemetryPayload(BaseModel):
    operator_name: str
    authorization_ref: str
    stream_url: str
    zone: str
    lat: Optional[float] = None
    lng: Optional[float] = None
    altitude_m: Optional[float] = None
    photogrammetry_url: Optional[str] = None


async def _save_upload(upload: UploadFile, dest_dir: Path, allowed_ext: set[str]) -> Path:
    if not upload.filename:
        raise HTTPException(400, "Archivo requerido")
    ext = Path(upload.filename).suffix.lower()
    if ext not in allowed_ext:
        raise HTTPException(400, f"Formato no permitido. Use: {', '.join(sorted(allowed_ext))}")
    data = await upload.read()
    if len(data) > MAX_BYTES:
        raise HTTPException(400, f"Archivo excede {settings.max_upload_mb} MB")
    dest = dest_dir / f"{uuid.uuid4().hex}{ext}"
    dest.write_bytes(data)
    return dest


async def _broadcast_match(triage: MedicalTriage, match_data: dict) -> None:
    payload = {
        "triage_id": triage.id,
        "case_code": triage.case_code,
        "hospital_name": triage.hospital_name,
        "victim_id": match_data.get("victim_id"),
        "victim_name": match_data.get("victim_name"),
        "confidence": match_data.get("confidence"),
        "tattoo_similarity": match_data.get("tattoo_similarity"),
        "height_delta_cm": match_data.get("height_delta_cm"),
    }
    await command_center_manager.broadcast("victim_identified", payload)


async def _process_triage_background(triage_id: str) -> None:
    async with async_session_factory() as session:
        triage = await session.get(MedicalTriage, triage_id)
        if not triage:
            return
        try:
            analysis = biometrics.analyze_medical_photo(ROOT / triage.photo_path)
            triage.estatura_estimada_cm = analysis.estatura_estimada_cm
            triage.tatuajes_clasificados = analysis.tatuajes_clasificados
            triage.tattoo_embeddings = analysis.tattoo_embeddings
            await session.flush()

            match = await biometrics.match_triage_against_missing(session, triage)
            if match:
                triage.matched_victim_id = match.victim_id
                triage.match_confidence = match.confidence
                triage.match_details = {
                    "victim_name": match.victim_name,
                    "tattoo_similarity": match.tattoo_similarity,
                    "height_delta_cm": match.height_delta_cm,
                }
                await session.commit()
                await _broadcast_match(
                    triage,
                    {
                        "victim_id": match.victim_id,
                        "victim_name": match.victim_name,
                        "confidence": match.confidence,
                        "tattoo_similarity": match.tattoo_similarity,
                        "height_delta_cm": match.height_delta_cm,
                    },
                )
            else:
                await session.commit()
        except Exception:
            logger.exception("Error procesando triaje %s", triage_id)
            await session.rollback()


async def _process_evidence_background(evidence_id: str) -> None:
    async with async_session_factory() as session:
        evidence = await session.get(CrowdsourcedEvidence, evidence_id)
        if not evidence:
            return
        evidence.processing_status = EvidenceStatus.PROCESANDO
        await session.commit()

        try:
            result = video_processor.analyze_evidence(ROOT / evidence.video_path, evidence_id)
            evidence.processing_status = EvidenceStatus.PROCESADO
            evidence.detections_count = result.get("total_detections", 0)
            evidence.ai_summary = result
            await session.commit()
            await command_center_manager.broadcast(
                "evidence_processed",
                {"id": evidence_id, "detections": evidence.detections_count},
            )
        except Exception as exc:
            evidence.processing_status = EvidenceStatus.ERROR
            evidence.ai_summary = {"error": str(exc)}
            await session.commit()


@router.get("/health")
async def health():
    return {
        "status": "operational",
        "service": "ojo-de-dios-sar-dvi",
        "mode": "crowdsourcing_autorizado",
        "realtime_workers": settings.enable_realtime_workers,
        "ws_clients": command_center_manager.active_count,
    }


@router.get("/api/dashboard/summary")
async def dashboard_summary(session: AsyncSession = Depends(get_session)):
    missing_total = await session.scalar(select(func.count()).select_from(MissingVictim)) or 0
    sos_active = await session.scalar(
        select(func.count()).where(SosSignal.is_active == True)  # noqa: E712
    ) or 0
    triage_count = await session.scalar(select(func.count()).select_from(MedicalTriage)) or 0
    cameras = await session.scalar(
        select(func.count()).where(VoluntaryCamera.is_active == True)  # noqa: E712
    ) or 0
    evidence = await session.scalar(select(func.count()).select_from(CrowdsourcedEvidence)) or 0
    drones = await session.scalar(select(func.count()).select_from(DroneTelemetry)) or 0
    matches = await session.scalar(
        select(func.count()).where(MedicalTriage.matched_victim_id.isnot(None))
    ) or 0
    return {
        "missing_persons": missing_total,
        "sos_active": sos_active,
        "medical_triage": triage_count,
        "voluntary_cameras": cameras,
        "crowdsourced_evidence": evidence,
        "drone_feeds": drones,
        "victim_matches": matches,
    }


@router.post("/api/sos/panic", status_code=201)
async def sos_panic(payload: SosPanicPayload, session: AsyncSession = Depends(get_session)):
    signal = SosSignal(
        lat=payload.lat,
        lng=payload.lng,
        vital_status=payload.vital_status,
        message=payload.message,
        contact_phone=payload.contact_phone,
        device_token=payload.device_token,
        is_active=True,
    )
    session.add(signal)
    await session.flush()
    data = {
        "id": signal.id,
        "lat": signal.lat,
        "lng": signal.lng,
        "vital_status": signal.vital_status.value,
        "message": signal.message,
        "created_at": signal.created_at.isoformat() if signal.created_at else None,
    }
    await command_center_manager.broadcast("sos_signal", data)
    return data


@router.get("/api/sos/signals")
async def list_sos_signals(active_only: bool = True, session: AsyncSession = Depends(get_session)):
    query = select(SosSignal).order_by(SosSignal.created_at.desc()).limit(500)
    if active_only:
        query = query.where(SosSignal.is_active == True)  # noqa: E712
    result = await session.execute(query)
    return [
        {
            "id": s.id,
            "lat": s.lat,
            "lng": s.lng,
            "vital_status": s.vital_status.value,
            "message": s.message,
            "contact_phone": s.contact_phone,
            "is_active": s.is_active,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        }
        for s in result.scalars().all()
    ]


@router.post("/api/triage/medical", status_code=201)
async def register_medical_triage(
    background_tasks: BackgroundTasks,
    hospital_name: str = Form(...),
    ward: Optional[str] = Form(None),
    clinical_notes: Optional[str] = Form(None),
    photo: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
):
    photo_path = await _save_upload(photo, MEDICAL_PHOTOS, ALLOWED_IMAGE)
    case_code = f"JD-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
    triage = MedicalTriage(
        case_code=case_code,
        hospital_name=hospital_name,
        ward=ward,
        photo_path=str(photo_path.relative_to(ROOT)),
        clinical_notes=clinical_notes,
    )
    session.add(triage)
    await session.flush()
    background_tasks.add_task(_process_triage_background, triage.id)
    return {
        "id": triage.id,
        "case_code": case_code,
        "hospital_name": hospital_name,
        "photo_url": f"/{triage.photo_path}",
        "status": "procesando_biometria",
    }


@router.get("/api/triage/medical")
async def list_medical_triage(session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(MedicalTriage).order_by(MedicalTriage.created_at.desc()).limit(200))
    items = []
    for t in result.scalars().all():
        items.append(
            {
                "id": t.id,
                "case_code": t.case_code,
                "hospital_name": t.hospital_name,
                "ward": t.ward,
                "photo_url": f"/{t.photo_path}",
                "estatura_estimada_cm": t.estatura_estimada_cm,
                "tatuajes_clasificados": t.tatuajes_clasificados or [],
                "matched_victim_id": t.matched_victim_id,
                "match_confidence": t.match_confidence,
                "match_details": t.match_details,
                "clinical_notes": t.clinical_notes,
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
        )
    return {"items": items, "count": len(items)}


@router.post("/api/cameras/voluntary", status_code=201)
async def register_voluntary_camera(
    payload: VoluntaryCameraPayload,
    session: AsyncSession = Depends(get_session),
):
    if not payload.terms_accepted:
        raise HTTPException(400, "Debe aceptar los Términos y Condiciones para registrar la cámara")
    if not payload.rtsp_url.lower().startswith("rtsp://"):
        raise HTTPException(400, "Solo se aceptan URLs RTSP proporcionadas voluntariamente por el condominio")

    camera = VoluntaryCamera(
        condominium_name=payload.condominium_name,
        contact_name=payload.contact_name,
        contact_phone=payload.contact_phone,
        rtsp_url=payload.rtsp_url,
        city=payload.city,
        zone=payload.zone,
        terms_accepted=True,
        terms_version=payload.terms_version,
        terms_accepted_at=datetime.now(timezone.utc),
        authorized_until=datetime.now(timezone.utc) + timedelta(days=30),
        is_active=True,
        status=FeedStatus.ACTIVE,
    )
    session.add(camera)
    await session.flush()
    data = {
        "id": camera.id,
        "condominium_name": camera.condominium_name,
        "city": camera.city,
        "zone": camera.zone,
        "status": camera.status.value,
        "authorized_until": camera.authorized_until.isoformat() if camera.authorized_until else None,
    }
    await command_center_manager.broadcast("voluntary_camera_registered", data)
    return data


@router.get("/api/cameras/voluntary")
async def list_voluntary_cameras(session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(VoluntaryCamera).where(VoluntaryCamera.is_active == True).order_by(VoluntaryCamera.created_at.desc())  # noqa: E712
    )
    return {
        "items": [
            {
                "id": c.id,
                "condominium_name": c.condominium_name,
                "contact_name": c.contact_name,
                "city": c.city,
                "zone": c.zone,
                "status": c.status.value,
                "terms_accepted_at": c.terms_accepted_at.isoformat() if c.terms_accepted_at else None,
                "authorized_until": c.authorized_until.isoformat() if c.authorized_until else None,
                "rtsp_url_masked": c.rtsp_url[:30] + "…" if len(c.rtsp_url) > 30 else c.rtsp_url,
            }
            for c in result.scalars().all()
        ]
    }


@router.post("/api/evidence/upload", status_code=201)
async def upload_crowdsourced_evidence(
    background_tasks: BackgroundTasks,
    uploader_name: str = Form(...),
    contact_phone: str = Form(...),
    location_description: str = Form(...),
    description: Optional[str] = Form(None),
    lat: Optional[float] = Form(None),
    lng: Optional[float] = Form(None),
    consent_given: bool = Form(True),
    video: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
):
    if not consent_given:
        raise HTTPException(400, "Se requiere consentimiento explícito para procesar la evidencia")
    video_path = await _save_upload(video, EVIDENCE_VIDEOS, ALLOWED_VIDEO)
    evidence = CrowdsourcedEvidence(
        uploader_name=uploader_name,
        contact_phone=contact_phone,
        video_path=str(video_path.relative_to(ROOT)),
        location_description=location_description,
        lat=lat,
        lng=lng,
        description=description,
        consent_given=True,
        processing_status=EvidenceStatus.PENDIENTE,
    )
    session.add(evidence)
    await session.flush()
    background_tasks.add_task(_process_evidence_background, evidence.id)
    return {"id": evidence.id, "status": "pendiente", "video_url": f"/{evidence.video_path}"}


@router.get("/api/evidence")
async def list_evidence(session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(CrowdsourcedEvidence).order_by(CrowdsourcedEvidence.created_at.desc()).limit(100)
    )
    return {
        "items": [
            {
                "id": e.id,
                "uploader_name": e.uploader_name,
                "location_description": e.location_description,
                "lat": e.lat,
                "lng": e.lng,
                "description": e.description,
                "processing_status": e.processing_status.value,
                "detections_count": e.detections_count,
                "ai_summary": e.ai_summary,
                "video_url": f"/{e.video_path}",
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in result.scalars().all()
        ]
    }


@router.post("/api/drones/telemetry", status_code=201)
async def register_drone_telemetry(
    payload: DroneTelemetryPayload,
    session: AsyncSession = Depends(get_session),
):
    drone = DroneTelemetry(
        operator_name=payload.operator_name,
        authorization_ref=payload.authorization_ref,
        stream_url=payload.stream_url,
        zone=payload.zone,
        lat=payload.lat,
        lng=payload.lng,
        altitude_m=payload.altitude_m,
        photogrammetry_url=payload.photogrammetry_url,
        status=FeedStatus.ACTIVE,
    )
    session.add(drone)
    await session.flush()
    data = {
        "id": drone.id,
        "operator_name": drone.operator_name,
        "zone": drone.zone,
        "authorization_ref": drone.authorization_ref,
        "photogrammetry_url": drone.photogrammetry_url,
    }
    await command_center_manager.broadcast("drone_registered", data)
    return data


@router.get("/api/drones/telemetry")
async def list_drone_telemetry(session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(DroneTelemetry).order_by(DroneTelemetry.created_at.desc()).limit(100))
    return {
        "items": [
            {
                "id": d.id,
                "operator_name": d.operator_name,
                "authorization_ref": d.authorization_ref,
                "zone": d.zone,
                "lat": d.lat,
                "lng": d.lng,
                "altitude_m": d.altitude_m,
                "photogrammetry_url": d.photogrammetry_url,
                "status": d.status.value,
                "detections_count": d.detections_count,
                "last_frame_at": d.last_frame_at.isoformat() if d.last_frame_at else None,
            }
            for d in result.scalars().all()
        ]
    }


@router.get("/api/missing")
async def list_missing_local(
    q: Optional[str] = None,
    status: Optional[MissingStatus] = None,
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
):
    query = select(MissingVictim).order_by(MissingVictim.updated_at.desc()).limit(min(limit, 200))
    if status:
        query = query.where(MissingVictim.status == status)
    if q:
        needle = f"%{q.strip()}%"
        query = query.where(MissingVictim.full_name.ilike(needle))
    result = await session.execute(query)
    return [
        {
            "id": p.id,
            "full_name": p.full_name,
            "age": p.age or p.edad,
            "status": p.status.value,
            "photo_url": p.photo_url,
            "local_photo": f"/photos/{Path(p.reference_photo_path).name}" if p.reference_photo_path else None,
            "estatura_estimada_cm": p.estatura_estimada_cm or p.height_cm,
            "last_known_location": p.last_known_location,
        }
        for p in result.scalars().all()
    ]


@router.websocket("/ws/command-center")
async def command_center_ws(websocket: WebSocket):
    conn_id = await command_center_manager.connect(websocket)
    try:
        await websocket.send_json(
            {
                "event": "connected",
                "data": {"message": "Centro de Comando SAR-DVI conectado"},
            }
        )
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await command_center_manager.disconnect(conn_id)