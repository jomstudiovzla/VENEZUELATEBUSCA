"""Red de Esperanza — motor logístico humanitario (sin videovigilancia ni biometría)."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select

from app.api.routes import router
from app.core.config import settings
from app.core.database import async_session_factory, init_db
from app.core.connection_manager import dashboard_ws
from app.models.models import (
    Inventory,
    InventoryStatus,
    MissingReport,
    Mission,
    MissionStatus,
    Shelter,
    ShelterType,
)
from app.services.semantic_matcher import run_matching_cycle

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("red_esperanza")

ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / "app" / "frontend"
STATIC = FRONTEND / "static"
STATIC.mkdir(parents=True, exist_ok=True)

_matcher_task: asyncio.Task | None = None


async def _seed_if_empty() -> None:
    async with async_session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(Shelter)) or 0
        if count > 0:
            return

        shelters = [
            Shelter(
                name="Refugio Parque Central",
                shelter_type=ShelterType.REFUGIO,
                address="Av. Urdaneta, Caracas",
                city="Caracas",
                contact_phone="0212-555-0101",
                max_capacity=350,
                current_occupancy=218,
                lat=10.5069,
                lng=-66.9153,
            ),
            Shelter(
                name="Hospital Vargas — Urgencias",
                shelter_type=ShelterType.HOSPITAL,
                address="Av. La Marina, La Guaira",
                city="La Guaira",
                contact_phone="0212-555-0202",
                max_capacity=120,
                current_occupancy=94,
                lat=10.5995,
                lng=-66.9346,
            ),
            Shelter(
                name="Centro de Acopio Valencia Norte",
                shelter_type=ShelterType.ACOPIO,
                address="Av. Bolívar Norte, Valencia",
                city="Valencia",
                contact_phone="0241-555-0303",
                max_capacity=500,
                current_occupancy=0,
                lat=10.1620,
                lng=-68.0077,
            ),
        ]
        session.add_all(shelters)
        await session.flush()

        inv = [
            Inventory(shelter_id=shelters[0].id, item_name="Agua potable", quantity=80, unit="litros", status=InventoryStatus.NECESITADO),
            Inventory(shelter_id=shelters[0].id, item_name="Medicinas básicas", quantity=45, unit="kits", status=InventoryStatus.NECESITADO),
            Inventory(shelter_id=shelters[1].id, item_name="Sangre O+", quantity=12, unit="bolsas", status=InventoryStatus.NECESITADO),
            Inventory(shelter_id=shelters[2].id, item_name="Cobijas", quantity=800, unit="unidades", status=InventoryStatus.EXCEDENTE),
            Inventory(shelter_id=shelters[2].id, item_name="Alimentos no perecederos", quantity=1200, unit="kg", status=InventoryStatus.EXCEDENTE),
        ]
        session.add_all(inv)

        missions = [
            Mission(
                title="Mover escombros — Calle Real de Sabana Grande",
                description="Se requieren 8 voluntarios con palas y cascos.",
                mission_type="rescate",
                address="Calle Real, Sabana Grande, Caracas",
                lat=10.4934,
                lng=-66.8742,
                priority=3,
                status=MissionStatus.ABIERTA,
            ),
            Mission(
                title="Llevar 100 litros de agua al Refugio Parque Central",
                description="Camión disponible en acopio Valencia; ruta segura confirmada.",
                mission_type="logistica",
                address="Av. Urdaneta, Caracas",
                lat=10.5069,
                lng=-66.9153,
                shelter_id=shelters[0].id,
                priority=3,
                status=MissionStatus.ABIERTA,
            ),
        ]
        session.add_all(missions)

        reports = [
            MissingReport(
                seeker_name="María González",
                seeker_contact="0414-555-1001",
                missing_person_name="Carlos Rodríguez",
                description="Hombre de 42 años, camisa azul, pantalón jean oscuro, tatuaje de águila en el brazo derecho.",
                last_seen_location="Sabana Grande, Caracas",
                physical_traits={"ropa": "camisa azul jean oscuro", "tatuajes": "águila brazo derecho", "estatura": "1.75m"},
            ),
            MissingReport(
                seeker_name="Ana Pérez",
                seeker_contact="0424-555-2002",
                missing_person_name="Desconocido — anciana",
                description="Mujer mayor, pelo blanco, cicatriz en la frente, vestido floral verde.",
                last_seen_location="La Guaira",
                physical_traits={"ropa": "vestido floral verde", "cicatrices": "frente", "pelo": "blanco"},
            ),
        ]
        session.add_all(reports)
        await session.commit()
        logger.info("Datos iniciales de demostración cargados")


async def _matcher_loop() -> None:
    while True:
        try:
            async with async_session_factory() as session:
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
        except Exception:
            logger.exception("Error en ciclo de matching semántico")
        await asyncio.sleep(settings.matcher_interval_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _matcher_task
    await init_db()
    await _seed_if_empty()
    _matcher_task = asyncio.create_task(_matcher_loop())
    logger.info("Red de Esperanza activa | matching cada %ds", settings.matcher_interval_seconds)
    yield
    if _matcher_task:
        _matcher_task.cancel()


app = FastAPI(
    title="Red de Esperanza",
    description="Sistema Centralizado de Rescate, Acopio y Refugios — Venezuela 2026",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(router)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/")
async def tablero():
    return FileResponse(FRONTEND / "index.html")


@app.get("/manifest.json")
async def manifest():
    return FileResponse(FRONTEND / "manifest.json")