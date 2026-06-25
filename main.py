"""API FastAPI del Sistema Humanitario SAR/DVI — Venezuela te Busca."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import cv2
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from connection_manager import status_updates_manager, victim_room_manager
from forensic_utils import person_to_forensic_dict, sync_forensic_fields
from data_ingestor import (
    DesaparecidosIngestor,
    extract_cedula,
    map_source_status,
    normalize_search_query,
    run_photo_download,
    run_sync,
    source_estado_for_status,
)
from event_bus import missing_updates_bus
from photo_realtime import PhotoRealtimeWorker, get_photo_stats
from scraper_realtime import RealtimeScraper
from building_photo_worker import BuildingPhotoWorker
from camera_service import SNAPSHOT_DIR as CAMERA_SNAPSHOT_DIR, camera_service
from terremoto_ingestor import TerremotoVenezuelaClient, fetch_live_unified_stats
from terremoto_photos import BUILDING_PHOTOS_DIR, enrich_building, get_building_photo_stats
from terremoto_realtime import TerremotoRealtimeWorker
from stats_dashboard import collect_dashboard_stats, get_live_stats_cache, update_live_stats_cache
from database import (
    AuthorizedRescueFeed,
    FeedStatus,
    MissingPerson,
    MissingStatus,
    MissingVictim,
    RescueAlert,
    SyncLog,
    async_session_factory,
    commit_with_retry,
    get_session,
    init_db,
    settings,
)
from reports import create_building_report, create_person_report, list_building_reports
from height_estimator import HeightEstimator
from rescue_matcher import RescueMatcher
from tattoo_analyzer import TattooAnalyzer
from victim_detector import YOLOv7OADetector, process_rescue_feed

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ojo_de_dios")

SNAPSHOT_DIR = Path("snapshots")
SNAPSHOT_DIR.mkdir(exist_ok=True)

detector: Optional[YOLOv7OADetector] = None
tattoo_analyzer: Optional[TattooAnalyzer] = None
height_estimator = HeightEstimator()
rescue_matcher: Optional[RescueMatcher] = None


def _get_ml_pipeline():
    global detector, tattoo_analyzer, rescue_matcher
    if detector is None:
        detector = YOLOv7OADetector()
    if tattoo_analyzer is None:
        tattoo_analyzer = TattooAnalyzer()
    if rescue_matcher is None:
        rescue_matcher = RescueMatcher(tattoo_analyzer)
    return detector, tattoo_analyzer, rescue_matcher

active_feed_tasks: dict[int, asyncio.Task] = {}
sync_task: Optional[asyncio.Task] = None
scraper: Optional[RealtimeScraper] = None
scraper_task: Optional[asyncio.Task] = None
photo_worker: Optional[PhotoRealtimeWorker] = None
photo_task: Optional[asyncio.Task] = None
terremoto_worker: Optional[TerremotoRealtimeWorker] = None
terremoto_task: Optional[asyncio.Task] = None
building_photo_worker: Optional[BuildingPhotoWorker] = None
building_photo_task: Optional[asyncio.Task] = None
camera_network_task: Optional[asyncio.Task] = None

STATIC_DIR = Path("static")
STATIC_DIR.mkdir(exist_ok=True)
TEMPLATES_DIR = Path("templates")
TEMPLATES_DIR.mkdir(exist_ok=True)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


class VictimCreate(BaseModel):
    full_name: str
    age: Optional[int] = None
    gender: Optional[str] = None
    height_cm: Optional[float] = None
    skin_tone: Optional[str] = None
    hair_description: Optional[str] = None
    clothing_description: Optional[str] = None
    distinguishing_marks: Optional[str] = None
    tattoo_descriptions: Optional[list[str]] = None
    last_known_location: Optional[str] = None
    reporter_contact: Optional[str] = None


class FeedCreate(BaseModel):
    name: str
    source_type: str = Field(..., description="drone | public_camera | volunteer")
    rtsp_url: str
    disaster_zone: Optional[str] = None
    authorized_by: Optional[str] = None


class StatusUpdatePayload(BaseModel):
    nuevo_estado: MissingStatus


class AlertResponse(BaseModel):
    id: int
    victim_id: int
    feed_id: int
    confidence: float
    tattoo_similarity: Optional[float]
    height_delta_cm: Optional[float]
    acknowledged: bool
    created_at: datetime


async def _process_human_frame(feed_id: int, frame, detections) -> None:
    from database import async_session_factory

    _, analyzer, matcher = _get_ml_pipeline()
    async with async_session_factory() as session:
        feed = await session.get(AuthorizedRescueFeed, feed_id)
        if feed and feed.camera_matrix and feed.distortion_coeffs:
            height_estimator.load_calibration(feed.camera_matrix, feed.distortion_coeffs)

        for detection in detections:
            tattoo_regions = analyzer.extract_from_crop(detection.crop)
            height_estimate = height_estimator.estimate_height(frame, detection.bbox)

            snapshot_name = f"{feed_id}_{uuid.uuid4().hex[:8]}.jpg"
            snapshot_path = SNAPSHOT_DIR / snapshot_name
            cv2.imwrite(str(snapshot_path), detection.crop)

            await matcher.match_detection(
                session=session,
                detection=detection,
                tattoo_regions=tattoo_regions,
                height_estimate=height_estimate,
                feed_id=feed_id,
                frame_snapshot_path=str(snapshot_path),
            )

        await session.commit()


async def _run_feed_pipeline(feed_id: int, rtsp_url: str) -> None:
    async def callback(frame, detections):
        await _process_human_frame(feed_id, frame, detections)

    try:
        det, _, _ = _get_ml_pipeline()
        await process_rescue_feed(rtsp_url, det, callback)
    except asyncio.CancelledError:
        logger.info("Pipeline detenido para feed %d", feed_id)
    except Exception:
        logger.exception("Error en pipeline del feed %d", feed_id)


async def _background_sync() -> None:
    from sqlalchemy import func

    try:
        async with async_session_factory() as session:
            count = await session.scalar(select(func.count()).select_from(MissingVictim))
        if not count:
            await run_sync(download_photos=False, extract_embeddings=False)
        else:
            logger.info("Base con %d registros; omitiendo re-sync inicial", count)
    except Exception:
        logger.exception("Sincronización automática fallida")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global sync_task, scraper, scraper_task, photo_worker, photo_task
    global terremoto_worker, terremoto_task
    global building_photo_worker, building_photo_task, camera_network_task
    await init_db()
    sync_task = asyncio.create_task(_background_sync())
    scraper = RealtimeScraper()
    scraper_task = scraper.start()
    photo_worker = PhotoRealtimeWorker(
        batch_size=settings.photo_batch_size,
        pause_seconds=settings.photo_pause_seconds,
    )
    photo_task = photo_worker.start()
    terremoto_worker = TerremotoRealtimeWorker(
        poll_interval=float(settings.terremoto_poll_interval),
    )
    terremoto_task = terremoto_worker.start()
    building_photo_worker = BuildingPhotoWorker(
        batch_size=settings.building_photo_batch_size,
        pause_seconds=settings.building_photo_pause_seconds,
    )
    building_photo_task = building_photo_worker.start()
    camera_service.load_configs()
    camera_network_task = asyncio.create_task(camera_service.start_all())
    yield
    await camera_service.stop_all()
    if camera_network_task and not camera_network_task.done():
        camera_network_task.cancel()
    if building_photo_worker:
        building_photo_worker.stop()
    if building_photo_task and not building_photo_task.done():
        building_photo_task.cancel()
    if terremoto_worker:
        terremoto_worker.stop()
    if terremoto_task and not terremoto_task.done():
        terremoto_task.cancel()
    if photo_worker:
        photo_worker.stop()
    if photo_task and not photo_task.done():
        photo_task.cancel()
    if scraper:
        scraper.stop()
    if scraper_task and not scraper_task.done():
        scraper_task.cancel()
    if sync_task and not sync_task.done():
        sync_task.cancel()
    for task in active_feed_tasks.values():
        task.cancel()
    active_feed_tasks.clear()


app = FastAPI(
    title="Venezuela te Busca — SAR/DVI",
    description="Sistema Humanitario de Identificación de Víctimas y Búsqueda y Rescate",
    version="1.0.0",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
PHOTOS_DIR = Path("reference_photos")
PHOTOS_DIR.mkdir(exist_ok=True)
app.mount("/photos", StaticFiles(directory=str(PHOTOS_DIR)), name="photos")
BUILDING_PHOTOS_DIR.mkdir(exist_ok=True)
app.mount("/building-photos", StaticFiles(directory=str(BUILDING_PHOTOS_DIR)), name="building_photos")
CAMERA_SNAPSHOT_DIR.mkdir(exist_ok=True)
app.mount("/camera-snapshots", StaticFiles(directory=str(CAMERA_SNAPSHOT_DIR)), name="camera_snapshots")


@app.get("/health")
async def health():
    return {
        "status": "operational",
        "service": "venezuela-te-busca-sar-dvi",
        "ws_subscribers": missing_updates_bus.subscriber_count,
        "status_ws_operators": status_updates_manager.active_count,
        "scraper_cycles": scraper.stats.cycles if scraper else 0,
        "photo_cycles": photo_worker.stats.cycles if photo_worker else 0,
        "photos_downloaded_session": photo_worker.stats.downloaded if photo_worker else 0,
        "terremoto_cycles": terremoto_worker.cycles if terremoto_worker else 0,
        "terremoto_last_fetch": (
            terremoto_worker.last_stats.get("fetched_at") if terremoto_worker and terremoto_worker.last_stats else None
        ),
        "building_photo_cycles": building_photo_worker.stats.cycles if building_photo_worker else 0,
        "building_photos_downloaded_session": (
            building_photo_worker.stats.downloaded if building_photo_worker else 0
        ),
    }


@app.get("/api/photos/stats")
async def photos_stats():
    stats = await get_photo_stats()
    if photo_worker:
        stats["worker"] = {
            "cycles": photo_worker.stats.cycles,
            "downloaded_session": photo_worker.stats.downloaded,
            "failed_session": photo_worker.stats.failed,
            "last_batch": photo_worker.stats.last_batch,
        }
    return stats


EMERGENCIAS_CONFIG = Path("config/emergencias_venezuela.json")


@app.get("/api/emergencias")
async def emergencias_venezuela(zona: Optional[str] = None):
    if not EMERGENCIAS_CONFIG.exists():
        raise HTTPException(404, "Configuración de emergencias no encontrada")
    data = json.loads(EMERGENCIAS_CONFIG.read_text(encoding="utf-8"))
    if zona:
        zonas = [z for z in data.get("zonas", []) if z.get("id") == zona or z.get("estado", "").lower() == zona.lower()]
        if zonas:
            data = {**data, "zonas": zonas, "zona_filtrada": zona}
    return data


@app.get("/")
async def visor_principal():
    visor = STATIC_DIR / "visor.html"
    if not visor.exists():
        raise HTTPException(404, "Visor no encontrado")
    return FileResponse(visor)


@app.get("/tablero")
async def triage_board():
    index = STATIC_DIR / "index.html"
    if not index.exists():
        raise HTTPException(404, "Tablero no encontrado")
    return FileResponse(index)


def _serialize_person(p: MissingPerson) -> dict:
    ubicacion = p.last_known_location or ""
    descripcion = p.distinguishing_marks or ""
    cedula = extract_cedula(ubicacion) or extract_cedula(descripcion) or extract_cedula(p.reporter_contact)
    return {
        "id": p.id,
        "external_id": p.external_id,
        "full_name": p.full_name,
        "age": p.age,
        "gender": p.gender,
        "cedula": cedula,
        "photo_url": p.photo_url,
        "local_photo_url": (
            f"/photos/{p.reference_photo_path.split('/')[-1]}"
            if p.reference_photo_path
            else None
        ),
        "has_photo": bool(p.reference_photo_path),
        "height_cm": p.height_cm,
        "distinguishing_marks": p.distinguishing_marks,
        "last_known_location": p.last_known_location,
        "reporter_contact": p.reporter_contact,
        "status": p.status.value,
        "nuevo_estado": p.status.value,
        "is_local_report": bool(p.external_id and p.external_id.startswith("report-")),
    }


def _serialize_live_item(item: dict[str, Any], local: Optional[MissingPerson] = None) -> dict:
    if local:
        data = _serialize_person(local)
        data["source"] = "local"
        return data

    ubicacion = item.get("ubicacion") or ""
    descripcion = item.get("descripcion") or ""
    contacto = item.get("contacto") or ""
    estado = map_source_status(item.get("estado", "sin-contacto"))
    cedula = extract_cedula(ubicacion) or extract_cedula(descripcion) or extract_cedula(contacto)
    foto = item.get("foto") or ""
    return {
        "id": None,
        "external_id": item.get("id"),
        "full_name": item.get("nombre", "").strip(),
        "age": item.get("edad"),
        "gender": None,
        "cedula": cedula,
        "photo_url": foto or None,
        "local_photo_url": None,
        "has_photo": bool(foto),
        "height_cm": None,
        "distinguishing_marks": descripcion,
        "last_known_location": ubicacion,
        "reporter_contact": contacto,
        "status": estado.value,
        "nuevo_estado": estado.value,
        "source": "live_api",
        "live_only": True,
    }


async def _search_missing_live(
    q: str,
    status: Optional[MissingStatus],
    page: int,
    page_size: int,
    session: AsyncSession,
) -> dict:
    estado = source_estado_for_status(status)
    payload: dict[str, Any] = {"items": [], "total": 0, "totalPages": 0}

    async with DesaparecidosIngestor() as ingestor:
        for variant in normalize_search_query(q):
            payload = await ingestor.fetch_page(
                page=page,
                page_size=page_size,
                estado=estado,
                query=variant,
            )
            if payload.get("total", 0) > 0:
                break

    live_items = payload.get("items", [])
    external_ids = [item["id"] for item in live_items if item.get("id")]
    local_by_ext: dict[str, MissingPerson] = {}
    if external_ids:
        result = await session.execute(
            select(MissingPerson).where(MissingPerson.external_id.in_(external_ids))
        )
        local_by_ext = {p.external_id: p for p in result.scalars().all() if p.external_id}

    items = [
        _serialize_live_item(item, local_by_ext.get(item.get("id", "")))
        for item in live_items
    ]
    total = int(payload.get("total", len(items)))
    total_pages = int(payload.get("totalPages") or max(1, (total + page_size - 1) // page_size))
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "source": "live_api",
        "query": q,
    }


async def _list_missing_local(
    status: Optional[MissingStatus],
    page: int,
    page_size: int,
    session: AsyncSession,
) -> dict:
    filters = []
    if status:
        filters.append(MissingPerson.status == status)

    count_query = select(func.count()).select_from(MissingPerson)
    if filters:
        count_query = count_query.where(*filters)
    total = int((await session.execute(count_query)).scalar_one())

    query = (
        select(MissingPerson)
        .order_by(MissingPerson.updated_at.desc(), MissingPerson.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    if filters:
        query = query.where(*filters)

    result = await session.execute(query)
    persons = result.scalars().all()
    total_pages = max(1, (total + page_size - 1) // page_size)
    return {
        "items": [_serialize_person(p) for p in persons],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "source": "local_db",
    }


@app.get("/missing")
async def list_missing(
    status: Optional[MissingStatus] = None,
    q: Optional[str] = None,
    page: Optional[int] = None,
    page_size: int = 100,
    limit: Optional[int] = None,
    session: AsyncSession = Depends(get_session),
):
    if q and q.strip():
        return await _search_missing_live(
            q=q.strip(),
            status=status,
            page=max(page or 1, 1),
            page_size=min(max(page_size, 1), 100),
            session=session,
        )

    if page is not None:
        data = await _list_missing_local(
            status=status,
            page=max(page, 1),
            page_size=min(max(page_size, 1), 200),
            session=session,
        )
        if terremoto_worker and terremoto_worker.last_stats:
            live_des = terremoto_worker.last_stats.get("desaparecidos", {})
            if status == MissingStatus.DESAPARECIDO:
                data["live_total"] = live_des.get("sin_contacto")
            elif status == MissingStatus.LOCALIZADO:
                data["live_total"] = live_des.get("localizado")
            elif status is None:
                data["live_total"] = live_des.get("total")
        return data

    effective_limit = limit or 120
    query = select(MissingPerson).order_by(MissingPerson.updated_at.desc()).limit(effective_limit)
    if status:
        query = query.where(MissingPerson.status == status)
    result = await session.execute(query)
    persons = result.scalars().all()
    return [_serialize_person(p) for p in persons]


@app.patch("/missing/{person_id}/status")
async def update_missing_status(person_id: int, payload: StatusUpdatePayload):
    from database import commit_with_retry

    async with async_session_factory() as session:
        person = await session.get(MissingPerson, person_id)
        if person is None:
            raise HTTPException(404, "Persona no encontrada")

        previous = person.status.value
        person.status = payload.nuevo_estado
        await session.flush()
        await commit_with_retry(session)

        broadcast_data = {
            "id": person.id,
            "external_id": person.external_id,
            "full_name": person.full_name,
            "nuevo_estado": payload.nuevo_estado.value,
            "estado_anterior": previous,
            "last_known_location": person.last_known_location,
            "photo_url": person.photo_url,
        }

    broadcast_data["nombre_completo"] = broadcast_data.get("full_name")
    broadcast_data["estado"] = payload.nuevo_estado.value.upper()
    delivered = await victim_room_manager.broadcast_status(person_id, broadcast_data)
    return {
        "id": person_id,
        "nuevo_estado": payload.nuevo_estado.value,
        "estado_anterior": previous,
        "broadcast_to": delivered,
    }


async def _resolve_person_by_external_id(external_id: str) -> MissingPerson:
    from database import commit_with_retry

    person_id: Optional[int] = None

    async with async_session_factory() as session:
        result = await session.execute(
            select(MissingPerson).where(MissingPerson.external_id == external_id)
        )
        person = result.scalar_one_or_none()
        if person:
            person_id = person.id

        needs_refresh = person is None or not person.reference_photo_path
        if needs_refresh:
            async with DesaparecidosIngestor() as ingestor:
                try:
                    record = await ingestor.fetch_persona(external_id)
                except Exception as exc:
                    if person_id is None:
                        raise HTTPException(404, "Persona no encontrada en la fuente oficial") from exc
                else:
                    person, _ = await ingestor.upsert_victim(
                        session,
                        record,
                        process_photo=True,
                    )
                    await session.flush()
                    await commit_with_retry(session)
                    person_id = person.id

    if person_id is None:
        raise HTTPException(404, "Perfil forense no encontrado")

    async with async_session_factory() as session:
        resolved = await session.get(MissingPerson, person_id)
        if resolved is None:
            raise HTTPException(404, "Perfil forense no encontrado")
        return resolved


@app.get("/victima/ext/{external_id}")
async def victima_resolve_external(external_id: str):
    person = await _resolve_person_by_external_id(external_id)
    return RedirectResponse(url=f"/victima/{person.id}", status_code=302)


@app.get("/victima/{person_id}", response_class=HTMLResponse)
async def victima_detail_page(person_id: int, request: Request):
    async with async_session_factory() as session:
        person = await session.get(MissingPerson, person_id)
        if person is None:
            raise HTTPException(404, "Perfil forense no encontrado")
        sync_forensic_fields(person)
        victim = person_to_forensic_dict(person)
    return templates.TemplateResponse(
        request=request,
        name="victim_detail.html",
        context={"request": request, "victim": victim},
    )


@app.get("/api/victima/{person_id}")
async def victima_detail_json(person_id: int):
    async with async_session_factory() as session:
        person = await session.get(MissingPerson, person_id)
        if person is None:
            raise HTTPException(404, "Perfil forense no encontrado")
        return person_to_forensic_dict(person)


@app.websocket("/ws/victima/{person_id}")
async def victima_status_ws(websocket: WebSocket, person_id: int):
    room = str(person_id)
    conn_id = await victim_room_manager.connect(websocket, room=room)
    await victim_room_manager.send_personal(
        conn_id,
        {
            "event": "connected",
            "data": {
                "message": f"Suscrito al perfil forense #{person_id}",
                "victim_id": person_id,
            },
            "timestamp": datetime.now().isoformat(),
        },
        room=room,
    )
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await victim_room_manager.disconnect(conn_id, room=room)
    except Exception:
        logger.exception("Error WS /ws/victima/%s", person_id)
        await victim_room_manager.disconnect(conn_id, room=room)


def _dashboard_worker_kwargs() -> dict[str, Any]:
    return {
        "scraper_stats": {
            "cycles": scraper.stats.cycles,
            "source_total": scraper.stats.source_total,
            "sin_contacto": scraper.stats.sin_contacto,
            "localizado": scraper.stats.localizado,
            "last_new": scraper.stats.last_new,
            "last_updated": scraper.stats.last_updated,
        }
        if scraper
        else {},
        "photo_worker": {
            "cycles": photo_worker.stats.cycles,
            "downloaded_session": photo_worker.stats.downloaded,
        }
        if photo_worker
        else {},
        "building_photo_worker": {
            "cycles": building_photo_worker.stats.cycles,
            "downloaded_session": building_photo_worker.stats.downloaded,
        }
        if building_photo_worker
        else {},
        "terremoto_cycles": terremoto_worker.cycles if terremoto_worker else 0,
        "cameras_total": len(camera_service.cameras),
        "cameras_online": sum(
            1 for runtime in camera_service.cameras.values() if runtime.status == "en_vivo"
        ),
    }


async def _fast_live_fallback() -> dict[str, Any]:
    dash = await collect_dashboard_stats(**_dashboard_worker_kwargs())
    local = dash["local"]
    cached_live = dash.get("live") or {}
    return {
        "fetched_at": dash["fetched_at"],
        "desaparecidos": cached_live.get("desaparecidos")
        or {
            "total": local["total"],
            "sin_contacto": local["desaparecido"],
            "localizado": local["localizado"],
        },
        "terremoto": cached_live.get("terremoto")
        or {
            "total_edificios": 0,
            "dano_parcial": 0,
            "dano_severo": 0,
            "dano_total": 0,
        },
        "fuentes": cached_live.get("fuentes", {}),
        "source": "local_cache",
    }


@app.websocket("/ws/status_updates")
async def status_updates_ws(websocket: WebSocket):
    conn_id = await status_updates_manager.connect(websocket)
    await status_updates_manager.send_personal(
        conn_id,
        {
            "event": "connected",
            "data": {"message": "Conectado al tablero de triaje", "operator_id": conn_id},
            "timestamp": datetime.now().isoformat(),
        },
    )
    live_payload = (
        terremoto_worker.last_stats
        if terremoto_worker and terremoto_worker.last_stats
        else get_live_stats_cache() or await _fast_live_fallback()
    )
    await status_updates_manager.send_personal(
        conn_id,
        {
            "event": "live_stats",
            "data": live_payload,
            "timestamp": datetime.now().isoformat(),
        },
    )
    dashboard = await collect_dashboard_stats(**_dashboard_worker_kwargs())
    await status_updates_manager.send_personal(
        conn_id,
        {
            "event": "dashboard_stats",
            "data": dashboard,
            "timestamp": datetime.now().isoformat(),
        },
    )
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await status_updates_manager.disconnect(conn_id)
    except Exception:
        logger.exception("Error en WebSocket /ws/status_updates")
        await status_updates_manager.disconnect(conn_id)


@app.get("/demo")
async def demo_frontend():
    demo = STATIC_DIR / "realtime_demo.html"
    if not demo.exists():
        raise HTTPException(404, "Demo no encontrada")
    return FileResponse(demo)


@app.websocket("/ws/missing_updates")
async def missing_updates_ws(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_json(
        {
            "event": "connected",
            "data": {"message": "Suscrito a actualizaciones de desaparecidos"},
            "timestamp": datetime.now().isoformat(),
        }
    )
    try:
        async for event in missing_updates_bus.subscribe():
            await websocket.send_json(event)
    except WebSocketDisconnect:
        logger.info("Cliente WebSocket desconectado")
    except Exception:
        logger.exception("Error en WebSocket /ws/missing_updates")
        await websocket.close()


@app.get("/source/stats")
async def source_stats():
    async with DesaparecidosIngestor() as ingestor:
        return await ingestor.get_source_stats()


@app.get("/api/stats/dashboard")
async def dashboard_stats():
    return await collect_dashboard_stats(**_dashboard_worker_kwargs())


@app.get("/api/stats/live")
async def live_unified_stats(background_tasks: BackgroundTasks):
    if terremoto_worker and terremoto_worker.last_stats:
        background_tasks.add_task(_refresh_live_stats_if_needed)
        return terremoto_worker.last_stats
    cached = get_live_stats_cache()
    if cached:
        background_tasks.add_task(_refresh_live_stats_if_needed)
        return cached
    background_tasks.add_task(_refresh_live_stats_if_needed)
    return await _fast_live_fallback()


async def _refresh_live_stats_if_needed() -> None:
    if not terremoto_worker:
        return
    try:
        await terremoto_worker.poll_and_broadcast(min_interval=15.0)
    except Exception:
        logger.exception("Error refrescando stats en vivo en segundo plano")


@app.get("/api/terremoto/buildings")
async def terremoto_buildings(
    limit: int = 50,
    damage_level: Optional[str] = None,
    search: Optional[str] = None,
    session: AsyncSession = Depends(get_session),
):
    async with TerremotoVenezuelaClient() as client:
        buildings = await client.fetch_buildings(
            limit=min(limit, 200),
            damage_level=damage_level,
            search=search,
        )
    enriched = [enrich_building(b) for b in buildings]
    local_reports = await list_building_reports(session, limit=200)
    if damage_level:
        local_reports = [b for b in local_reports if b.get("damage_level") == damage_level]
    if search:
        needle = search.lower()
        local_reports = [
            b
            for b in local_reports
            if needle in (b.get("name") or "").lower()
            or needle in (b.get("address") or "").lower()
            or needle in (b.get("city") or "").lower()
            or needle in (b.get("zone") or "").lower()
        ]
    merged: dict[str, dict[str, Any]] = {b["id"]: b for b in local_reports}
    for building in enriched:
        merged.setdefault(building["id"], building)
    combined = sorted(
        merged.values(),
        key=lambda item: item.get("last_updated_at") or "",
        reverse=True,
    )[: min(limit, 200)]
    return {
        "fuente": "https://terremotovenezuela.com/",
        "carpeta_fotos": str(BUILDING_PHOTOS_DIR.resolve()),
        "count": len(combined),
        "reportes_locales": len(local_reports),
        "buildings": combined,
    }


@app.post("/api/reports/persona", status_code=201)
async def report_missing_person(
    full_name: str = Form(...),
    last_known_location: str = Form(...),
    reporter_contact: str = Form(...),
    photo: UploadFile = File(...),
    cedula: Optional[str] = Form(None),
    age: Optional[str] = Form(None),
    gender: Optional[str] = Form(None),
    last_seen_date: Optional[str] = Form(None),
    distinguishing_marks: Optional[str] = Form(None),
    reporter_name: Optional[str] = Form(None),
    session: AsyncSession = Depends(get_session),
):
    result = await create_person_report(
        session,
        full_name=full_name,
        last_known_location=last_known_location,
        reporter_contact=reporter_contact,
        photo=photo,
        cedula=cedula,
        age=age,
        gender=gender,
        last_seen_date=last_seen_date,
        distinguishing_marks=distinguishing_marks,
        reporter_name=reporter_name,
    )
    await commit_with_retry(session)
    return result


@app.post("/api/reports/edificio", status_code=201)
async def report_damaged_building(
    name: str = Form(...),
    address: str = Form(...),
    city: str = Form(...),
    damage_level: str = Form(...),
    reporter_contact: str = Form(...),
    photo: UploadFile = File(...),
    zone: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    reporter_name: Optional[str] = Form(None),
    lat: Optional[str] = Form(None),
    lng: Optional[str] = Form(None),
    session: AsyncSession = Depends(get_session),
):
    result = await create_building_report(
        session,
        name=name,
        address=address,
        city=city,
        damage_level=damage_level,
        reporter_contact=reporter_contact,
        photo=photo,
        zone=zone,
        notes=notes,
        reporter_name=reporter_name,
        lat=lat,
        lng=lng,
    )
    await commit_with_retry(session)
    return result


@app.get("/api/reports/edificios")
async def list_local_building_reports(session: AsyncSession = Depends(get_session)):
    items = await list_building_reports(session, limit=500)
    return {"count": len(items), "buildings": items}


@app.get("/api/cameras")
async def list_cameras():
    return {
        "red": "SAR Venezuela — vigilancia 24h",
        "camaras": camera_service.list_cameras(),
        "config": "config/camaras_venezuela.json",
    }


@app.get("/api/cameras/detections")
async def camera_detections(camera_id: Optional[str] = None):
    items = camera_service.detections_24h(camera_id)
    return {"count": len(items), "ventana_horas": 24, "detecciones": items}


@app.get("/api/cameras/{camera_id}")
async def camera_detail(camera_id: str):
    status = camera_service.camera_status(camera_id)
    if not status:
        raise HTTPException(404, "Cámara no encontrada")
    return status


@app.get("/api/cameras/{camera_id}/relay.mjpg")
async def camera_relay_mjpeg(camera_id: str):
    """Reenvía el stream MJPEG original sin re-codificar (video fluido)."""
    if camera_id not in camera_service.cameras:
        raise HTTPException(404, "Cámara no encontrada")
    relay_url = camera_service.get_relay_url(camera_id)
    if not relay_url:
        raise HTTPException(503, "Sin URL MJPEG para reenvío directo")

    import httpx

    client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=60.0), follow_redirects=True)

    upstream_type = "multipart/x-mixed-replace; boundary=frame"

    async def relay():
        nonlocal upstream_type
        try:
            async with client.stream("GET", relay_url) as response:
                if response.status_code >= 400:
                    raise HTTPException(502, f"Fuente MJPEG respondió {response.status_code}")
                upstream_type = response.headers.get("content-type", upstream_type)
                async for chunk in response.aiter_bytes(16384):
                    yield chunk
        finally:
            await client.aclose()

    return StreamingResponse(
        relay(),
        media_type=upstream_type,
        headers={"Cache-Control": "no-cache, no-store", "Pragma": "no-cache"},
    )


@app.get("/api/cameras/{camera_id}/live.mjpg")
async def camera_live_mjpeg(camera_id: str):
    """Stream MJPEG generado desde captura continua (~15 fps)."""
    if camera_id not in camera_service.cameras:
        raise HTTPException(404, "Cámara no encontrada")

    async def generate():
        boundary = b"--frame"
        last_jpeg: Optional[bytes] = None
        while True:
            jpeg = camera_service.get_live_jpeg(camera_id, wait=True, timeout=1.0)
            if jpeg and jpeg != last_jpeg:
                last_jpeg = jpeg
                yield boundary + b"\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
            else:
                await asyncio.sleep(0.05)

    return StreamingResponse(
        generate(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-cache, no-store", "Pragma": "no-cache"},
    )


@app.post("/api/cameras/reload")
async def reload_cameras():
    await camera_service.stop_all()
    camera_service.load_configs()
    await camera_service.start_all()
    return {"status": "ok", "camaras": len(camera_service.cameras)}


@app.get("/api/terremoto/photos/stats")
async def building_photos_stats():
    stats = await get_building_photo_stats()
    if building_photo_worker:
        stats["worker"] = {
            "cycles": building_photo_worker.stats.cycles,
            "downloaded_session": building_photo_worker.stats.downloaded,
            "failed_session": building_photo_worker.stats.failed,
            "skipped_session": building_photo_worker.stats.skipped,
            "last_batch": building_photo_worker.stats.last_batch,
        }
    return stats


@app.get("/sync/status")
async def sync_status(session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(SyncLog).order_by(SyncLog.started_at.desc()).limit(5)
    )
    logs = result.scalars().all()
    return [
        {
            "id": log.id,
            "source": log.source,
            "records_fetched": log.records_fetched,
            "records_created": log.records_created,
            "records_updated": log.records_updated,
            "photos_processed": log.photos_processed,
            "completed": log.completed,
            "error_message": log.error_message,
            "started_at": log.started_at,
            "finished_at": log.finished_at,
        }
        for log in logs
    ]


@app.post("/sync")
async def trigger_sync(
    estado: Optional[str] = None,
    max_pages: Optional[int] = None,
    download_photos: bool = True,
    extract_embeddings: bool = False,
):
    global sync_task
    if sync_task and not sync_task.done():
        return {"status": "already_running"}

    async def _run():
        await run_sync(
            estado=estado,
            max_pages=max_pages,
            download_photos=download_photos,
            extract_embeddings=extract_embeddings,
        )

    sync_task = asyncio.create_task(_run())
    return {"status": "started", "estado": estado or "todos"}


@app.post("/sync/photos")
async def trigger_photo_download(limit: Optional[int] = None):
    global sync_task
    if sync_task and not sync_task.done():
        return {"status": "already_running"}

    async def _run():
        await run_photo_download(limit=limit)

    sync_task = asyncio.create_task(_run())
    return {"status": "started", "limit": limit}


@app.post("/victims", status_code=201)
async def register_missing_victim(
    payload: VictimCreate,
    session: AsyncSession = Depends(get_session),
):
    victim = MissingVictim(
        full_name=payload.full_name,
        age=payload.age,
        gender=payload.gender,
        height_cm=payload.height_cm,
        skin_tone=payload.skin_tone,
        hair_description=payload.hair_description,
        clothing_description=payload.clothing_description,
        distinguishing_marks=payload.distinguishing_marks,
        tattoo_descriptions=payload.tattoo_descriptions,
        last_known_location=payload.last_known_location,
        reporter_contact=payload.reporter_contact,
        status=MissingStatus.DESAPARECIDO,
    )
    session.add(victim)
    await session.flush()
    return {"id": victim.id, "status": victim.status.value}


@app.get("/victims")
async def list_victims(session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(MissingVictim).order_by(MissingVictim.created_at.desc()))
    victims = result.scalars().all()
    return [
        {
            "id": v.id,
            "external_id": v.external_id,
            "full_name": v.full_name,
            "age": v.age,
            "height_cm": v.height_cm,
            "status": v.status.value,
            "source_estado": v.source_estado,
            "last_known_location": v.last_known_location,
            "photo_url": v.photo_url,
            "has_photo": bool(v.reference_photo_path),
        }
        for v in victims
    ]


@app.post("/feeds", status_code=201)
async def register_rescue_feed(
    payload: FeedCreate,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
):
    feed = AuthorizedRescueFeed(
        name=payload.name,
        source_type=payload.source_type,
        rtsp_url=payload.rtsp_url,
        disaster_zone=payload.disaster_zone,
        authorized_by=payload.authorized_by,
        status=FeedStatus.ACTIVE,
    )
    session.add(feed)
    await session.flush()

    task = asyncio.create_task(_run_feed_pipeline(feed.id, feed.rtsp_url))
    active_feed_tasks[feed.id] = task

    return {"id": feed.id, "status": feed.status.value, "pipeline": "started"}


@app.post("/feeds/{feed_id}/stop")
async def stop_feed(feed_id: int):
    task = active_feed_tasks.pop(feed_id, None)
    if task is None:
        raise HTTPException(404, "Feed no activo")
    task.cancel()
    return {"feed_id": feed_id, "pipeline": "stopped"}


@app.get("/alerts", response_model=list[AlertResponse])
async def list_alerts(
    acknowledged: Optional[bool] = None,
    session: AsyncSession = Depends(get_session),
):
    query = select(RescueAlert).order_by(RescueAlert.created_at.desc())
    if acknowledged is not None:
        query = query.where(RescueAlert.acknowledged == acknowledged)
    result = await session.execute(query)
    alerts = result.scalars().all()
    return [
        AlertResponse(
            id=a.id,
            victim_id=a.victim_id,
            feed_id=a.feed_id,
            confidence=a.confidence,
            tattoo_similarity=a.tattoo_similarity,
            height_delta_cm=a.height_delta_cm,
            acknowledged=a.acknowledged,
            created_at=a.created_at,
        )
        for a in alerts
    ]


@app.patch("/alerts/{alert_id}/acknowledge")
async def acknowledge_alert(alert_id: int, session: AsyncSession = Depends(get_session)):
    alert = await session.get(RescueAlert, alert_id)
    if alert is None:
        raise HTTPException(404, "Alerta no encontrada")
    alert.acknowledged = True
    return {"id": alert.id, "acknowledged": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)