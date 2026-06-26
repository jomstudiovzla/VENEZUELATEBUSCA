"""Red de Esperanza — motor logístico humanitario (sin videovigilancia ni biometría)."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select

from app.api.acopio_endpoint import router as acopio_router
from app.api.auth_endpoint import router as auth_router
from app.api.config_endpoint import router as config_router

from app.api.firebase_endpoint import router as firebase_router
from app.api.map_endpoint import router as map_router
from app.api.routes import router
from app.api.search_endpoint import router as search_router
from app.api.volunteer_endpoint import router as volunteer_router
from app.core.config import settings
from app.core.database import async_session_factory, init_db
from app.core.victims_database import init_victims_db
from app.services.victims_sync import sync_victims_incremental
from app.core.connection_manager import dashboard_ws, start_redis_listener
from app.models.models import (
    Inventory,
    InventoryStatus,
    MissingReport,
    Mission,
    MissionStatus,
    Shelter,
    ShelterType,
    VerificationStatus,
)
from app.core.auth import hash_password
from app.models.models import User, UserRole
from app.services.firebase_bridge import firebase_health
from app.services.punto_apoyo_sync import sync_all_map_points
from app.services.semantic_matcher import run_matching_cycle

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("red_esperanza")

ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / "app" / "frontend"
STATIC = FRONTEND / "static"
REFERENCE_PHOTOS = ROOT / "reference_photos"
STATIC.mkdir(parents=True, exist_ok=True)

_matcher_task: asyncio.Task | None = None
_victims_sync_task: asyncio.Task | None = None
_map_sync_task: asyncio.Task | None = None
MOBILE = ROOT / "app" / "mobile"
FAMILIAR = ROOT / "app" / "familiar"

_NATIONAL_ACOPIO: list[dict] = [
    {
        "name": "Centro de Acopio La Candelaria",
        "city": "Caracas", "state": "Distrito Capital",
        "address": "Av. México, La Candelaria, Caracas",
        "contact_phone": "0212-555-1001", "lat": 10.5061, "lng": -66.9036,
        "description": "Recepción de donaciones, clasificación y despacho a refugios del capital.",
        "services_offered": ["alimentos", "agua", "ropa", "medicinas", "higiene", "cobijas", "logística"],
        "inventory": [
            ("Agua potable", 2400, "litros", InventoryStatus.EXCEDENTE),
            ("Alimentos no perecederos", 1800, "kg", InventoryStatus.EXCEDENTE),
            ("Kits de higiene", 420, "unidades", InventoryStatus.EXCEDENTE),
            ("Medicinas básicas", 90, "kits", InventoryStatus.NECESITADO),
        ],
    },
    {
        "name": "Centro de Acopio Valencia Norte",
        "city": "Valencia", "state": "Carabobo",
        "address": "Av. Bolívar Norte, Valencia",
        "contact_phone": "0241-555-0303", "lat": 10.1620, "lng": -68.0077,
        "description": "Hub logístico del centro del país. Camiones disponibles para rutas a refugios.",
        "services_offered": ["alimentos", "agua", "ropa", "cobijas", "transporte", "voluntariado"],
        "inventory": [
            ("Cobijas", 800, "unidades", InventoryStatus.EXCEDENTE),
            ("Alimentos no perecederos", 1200, "kg", InventoryStatus.EXCEDENTE),
            ("Agua potable", 600, "litros", InventoryStatus.NECESITADO),
        ],
    },
    {
        "name": "Centro de Acopio Zulia Norte",
        "city": "Maracaibo", "state": "Zulia",
        "address": "Av. 5 de Julio, Maracaibo",
        "contact_phone": "0261-555-2001", "lat": 10.6668, "lng": -71.6125,
        "description": "Acopio regional occidente. Recibe donaciones marítimas y terrestres.",
        "services_offered": ["alimentos", "agua", "ropa", "medicinas", "higiene", "pañales"],
        "inventory": [
            ("Agua potable", 900, "litros", InventoryStatus.EXCEDENTE),
            ("Pañales adulto", 200, "unidades", InventoryStatus.NECESITADO),
            ("Ropa de cama", 350, "juegos", InventoryStatus.EXCEDENTE),
        ],
    },
    {
        "name": "Centro de Acopio Lara Central",
        "city": "Barquisimeto", "state": "Lara",
        "address": "Av. Vargas, Barquisimeto",
        "contact_phone": "0251-555-3001", "lat": 10.0647, "lng": -69.3570,
        "description": "Distribución a municipios afectados de Lara y Portuguesa.",
        "services_offered": ["alimentos", "agua", "ropa", "cobijas", "herramientas"],
        "inventory": [
            ("Alimentos no perecederos", 950, "kg", InventoryStatus.EXCEDENTE),
            ("Palas y picos", 45, "unidades", InventoryStatus.NECESITADO),
            ("Cobijas", 280, "unidades", InventoryStatus.EXCEDENTE),
        ],
    },
    {
        "name": "Centro de Acopio Aragua",
        "city": "Maracay", "state": "Aragua",
        "address": "Av. Las Delicias, Maracay",
        "contact_phone": "0243-555-4001", "lat": 10.2469, "lng": -67.5958,
        "description": "Punto de acopio para el estado Aragua y La Victoria.",
        "services_offered": ["alimentos", "agua", "medicinas", "higiene", "voluntariado"],
        "inventory": [
            ("Medicinas básicas", 120, "kits", InventoryStatus.NECESITADO),
            ("Agua potable", 400, "litros", InventoryStatus.NECESITADO),
            ("Kits de higiene", 180, "unidades", InventoryStatus.EXCEDENTE),
        ],
    },
    {
        "name": "Centro de Acopio Bolívar",
        "city": "Ciudad Guayana", "state": "Bolívar",
        "address": "Av. Paseo Caroní, Puerto Ordaz",
        "contact_phone": "0286-555-5001", "lat": 8.2890, "lng": -62.7300,
        "description": "Acopio sur-oriente. Coordinación con rutas fluviales.",
        "services_offered": ["alimentos", "agua", "ropa", "cobijas", "transporte fluvial"],
        "inventory": [
            ("Alimentos no perecederos", 700, "kg", InventoryStatus.EXCEDENTE),
            ("Lanchas de carga", 2, "unidades", InventoryStatus.EXCEDENTE),
            ("Agua potable", 200, "litros", InventoryStatus.NECESITADO),
        ],
    },
    {
        "name": "Centro de Acopio Monagas",
        "city": "Maturín", "state": "Monagas",
        "address": "Av. Alirio Ugarte Pelayo, Maturín",
        "contact_phone": "0291-555-6001", "lat": 9.7457, "lng": -63.1832,
        "description": "Recepción de donaciones para oriente venezolano.",
        "services_offered": ["alimentos", "agua", "ropa", "higiene"],
        "inventory": [
            ("Alimentos no perecederos", 500, "kg", InventoryStatus.EXCEDENTE),
            ("Kits de higiene", 90, "unidades", InventoryStatus.NECESITADO),
        ],
    },
    {
        "name": "Centro de Acopio Anzoátegui",
        "city": "Barcelona", "state": "Anzoátegui",
        "address": "Av. 5 de Julio, Barcelona",
        "contact_phone": "0281-555-7001", "lat": 10.1362, "lng": -64.6862,
        "description": "Acopio costa oriental. Enlace con Puerto La Cruz.",
        "services_offered": ["alimentos", "agua", "ropa", "medicinas", "cobijas"],
        "inventory": [
            ("Agua potable", 350, "litros", InventoryStatus.NECESITADO),
            ("Cobijas", 150, "unidades", InventoryStatus.EXCEDENTE),
            ("Medicinas básicas", 60, "kits", InventoryStatus.NECESITADO),
        ],
    },
    {
        "name": "Centro de Acopio Táchira",
        "city": "San Cristóbal", "state": "Táchira",
        "address": "Av. Carabobo, San Cristóbal",
        "contact_phone": "0276-555-8001", "lat": 7.7669, "lng": -72.2250,
        "description": "Acopio fronterizo occidente. Atención a desplazados.",
        "services_offered": ["alimentos", "agua", "ropa", "higiene", "refugio temporal"],
        "inventory": [
            ("Alimentos no perecederos", 420, "kg", InventoryStatus.EXCEDENTE),
            ("Colchonetas", 80, "unidades", InventoryStatus.NECESITADO),
        ],
    },
    {
        "name": "Centro de Acopio Los Andes",
        "city": "Mérida", "state": "Mérida",
        "address": "Av. Las Américas, Mérida",
        "contact_phone": "0274-555-9001", "lat": 8.5897, "lng": -71.1561,
        "description": "Acopio de montaña. Rutas a municipios de altura.",
        "services_offered": ["alimentos", "agua", "ropa", "cobijas", "abrigos"],
        "inventory": [
            ("Abrigos y chaquetas", 220, "unidades", InventoryStatus.EXCEDENTE),
            ("Agua potable", 180, "litros", InventoryStatus.NECESITADO),
            ("Alimentos no perecederos", 310, "kg", InventoryStatus.EXCEDENTE),
        ],
    },
    {
        "name": "Centro de Acopio Sucre",
        "city": "Cumaná", "state": "Sucre",
        "address": "Av. Bermúdez, Cumaná",
        "contact_phone": "0293-555-1101", "lat": 10.4530, "lng": -64.1826,
        "description": "Acopio región nor-oriental y costa de Sucre.",
        "services_offered": ["alimentos", "agua", "ropa", "higiene"],
        "inventory": [
            ("Alimentos no perecederos", 380, "kg", InventoryStatus.EXCEDENTE),
            ("Agua potable", 250, "litros", InventoryStatus.NECESITADO),
        ],
    },
    {
        "name": "Centro de Acopio Falcón",
        "city": "Coro", "state": "Falcón",
        "address": "Av. Miranda, Coro",
        "contact_phone": "0268-555-1201", "lat": 11.4045, "lng": -69.6737,
        "description": "Acopio península y media guajira.",
        "services_offered": ["alimentos", "agua", "ropa", "medicinas"],
        "inventory": [
            ("Agua potable", 300, "litros", InventoryStatus.NECESITADO),
            ("Medicinas básicas", 40, "kits", InventoryStatus.NECESITADO),
            ("Ropa", 190, "kg", InventoryStatus.EXCEDENTE),
        ],
    },
    {
        "name": "Centro de Acopio Paraguaná",
        "city": "Punto Fijo", "state": "Falcón",
        "address": "Av. Joséfa Camejo, Punto Fijo",
        "contact_phone": "0269-555-1301", "lat": 11.6916, "lng": -70.1996,
        "description": "Acopio península de Paraguaná. Donaciones industriales y comunitarias.",
        "services_offered": ["alimentos", "agua", "ropa", "higiene", "logística"],
        "inventory": [
            ("Alimentos no perecederos", 620, "kg", InventoryStatus.EXCEDENTE),
            ("Kits de higiene", 140, "unidades", InventoryStatus.EXCEDENTE),
        ],
    },
    {
        "name": "Centro de Acopio Portuguesa",
        "city": "Guanare", "state": "Portuguesa",
        "address": "Av. Unda, Guanare",
        "contact_phone": "0257-555-1401", "lat": 9.0436, "lng": -69.7489,
        "description": "Distribución a llanos occidentales.",
        "services_offered": ["alimentos", "agua", "ropa", "cobijas"],
        "inventory": [
            ("Alimentos no perecederos", 290, "kg", InventoryStatus.EXCEDENTE),
            ("Cobijas", 95, "unidades", InventoryStatus.NECESITADO),
        ],
    },
    {
        "name": "Centro de Acopio Costa Oriental",
        "city": "Puerto La Cruz", "state": "Anzoátegui",
        "address": "Av. Municipal, Puerto La Cruz",
        "contact_phone": "0281-555-1501", "lat": 10.2138, "lng": -64.6322,
        "description": "Acopio costero. Recepción de donaciones marítimas.",
        "services_offered": ["alimentos", "agua", "ropa", "medicinas", "transporte"],
        "inventory": [
            ("Agua potable", 480, "litros", InventoryStatus.EXCEDENTE),
            ("Medicinas básicas", 55, "kits", InventoryStatus.NECESITADO),
            ("Alimentos no perecederos", 410, "kg", InventoryStatus.EXCEDENTE),
        ],
    },
]


async def _ensure_national_acopio() -> None:
    """Garantiza centros de acopio registrados a nivel nacional."""
    async with async_session_factory() as session:
        existing = {
            s.name: s
            for s in (await session.execute(
                select(Shelter).where(Shelter.shelter_type == ShelterType.ACOPIO)
            )).scalars().all()
        }
        created = 0
        for spec in _NATIONAL_ACOPIO:
            shelter = existing.get(spec["name"])
            if shelter:
                shelter.state = spec["state"]
                shelter.description = spec["description"]
                shelter.services_offered = spec["services_offered"]
                shelter.address = spec["address"]
                shelter.city = spec["city"]
                shelter.contact_phone = spec["contact_phone"]
                shelter.lat = spec["lat"]
                shelter.lng = spec["lng"]
                shelter.is_official = True
                shelter.verification_status = VerificationStatus.VERIFICADO.value
                continue
            shelter = Shelter(
                name=spec["name"],
                shelter_type=ShelterType.ACOPIO,
                address=spec["address"],
                city=spec["city"],
                state=spec["state"],
                description=spec["description"],
                services_offered=spec["services_offered"],
                contact_phone=spec["contact_phone"],
                max_capacity=500,
                current_occupancy=0,
                lat=spec["lat"],
                lng=spec["lng"],
                is_official=True,
                verification_status=VerificationStatus.VERIFICADO.value,
                verified_by="Red de Esperanza",
                verified_at=datetime.now(timezone.utc),
            )
            session.add(shelter)
            await session.flush()
            existing[spec["name"]] = shelter
            for item_name, qty, unit, status in spec.get("inventory", []):
                session.add(Inventory(
                    shelter_id=shelter.id,
                    item_name=item_name,
                    quantity=qty,
                    unit=unit,
                    status=status,
                ))
            created += 1
        if created:
            logger.info("Centros de acopio nacional: %d nuevos registrados", created)
        await session.commit()


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
                state="Distrito Capital",
                description="Refugio temporal con atención básica y registro de ingresos.",
                services_offered=["alojamiento", "alimentos", "agua", "registro"],
                contact_phone="0212-555-0101",
                max_capacity=350,
                current_occupancy=218,
                lat=10.5069,
                lng=-66.9153,
                is_official=True,
                verification_status=VerificationStatus.VERIFICADO.value,
            ),
            Shelter(
                name="Hospital Vargas — Urgencias",
                shelter_type=ShelterType.HOSPITAL,
                address="Av. La Marina, La Guaira",
                city="La Guaira",
                state="La Guaira",
                description="Urgencias y triaje médico para lesionados del terremoto.",
                services_offered=["urgencias", "cirugía", "trauma", "laboratorio", "rayos X"],
                contact_phone="0212-555-0202",
                max_capacity=120,
                current_occupancy=94,
                lat=10.5995,
                lng=-66.9346,
                is_official=True,
                verification_status=VerificationStatus.VERIFICADO.value,
            ),
            Shelter(
                name="Centro de Acopio Valencia Norte",
                shelter_type=ShelterType.ACOPIO,
                address="Av. Bolívar Norte, Valencia",
                city="Valencia",
                state="Carabobo",
                description="Hub logístico del centro del país.",
                services_offered=["alimentos", "agua", "ropa", "cobijas", "transporte"],
                contact_phone="0241-555-0303",
                max_capacity=500,
                current_occupancy=0,
                lat=10.1620,
                lng=-68.0077,
                is_official=True,
                verification_status=VerificationStatus.VERIFICADO.value,
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


async def _seed_super_admin() -> None:
    async with async_session_factory() as session:
        exists = await session.scalar(
            select(User).where(User.username == settings.super_admin_username)
        )
        if exists:
            return
        session.add(
            User(
                username=settings.super_admin_username,
                password_hash=hash_password(settings.super_admin_password),
                display_name="Administrador JOM",
                role=UserRole.SUPER_ADMIN.value,
            )
        )
        await session.commit()
        logger.info("Super admin creado: %s", settings.super_admin_username)


async def _map_sync_loop() -> None:
    while True:
        try:
            await sync_all_map_points()
        except Exception:
            logger.exception("Error sincronizando mapa Punto de Apoyo")
        await asyncio.sleep(settings.punto_apoyo_sync_seconds)


async def _victims_sync_loop() -> None:
    while True:
        try:
            stats = await sync_victims_incremental()
            if stats["created"] or stats["updated"]:
                await dashboard_ws.broadcast("victims_sync_cycle", stats)
        except Exception:
            logger.exception("Error en sincronización de víctimas")
        await asyncio.sleep(settings.victims_sync_interval_seconds)


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
    global _matcher_task, _victims_sync_task, _map_sync_task
    await init_db()
    await init_victims_db()
    await _seed_if_empty()
    await _ensure_national_acopio()
    await _seed_super_admin()
    try:
        await sync_all_map_points()
    except Exception:
        logger.exception("Sync inicial de mapa falló")
    fb = await firebase_health()
    if fb.get("enabled"):
        logger.info("Firebase %s → %s (%s)", fb["project_id"], fb["database_url"], fb.get("detail", ""))
    _victims_sync_task = asyncio.create_task(_victims_sync_loop())
    _matcher_task = asyncio.create_task(_matcher_loop())
    _map_sync_task = asyncio.create_task(_map_sync_loop())
    await start_redis_listener()
    logger.info(
        "Red de Esperanza activa | víctimas %ds | mapa %ds | live %.2fs",
        settings.victims_sync_interval_seconds,
        settings.punto_apoyo_sync_seconds,
        settings.live_sync_interval_seconds,
    )
    yield
    for task in (_victims_sync_task, _matcher_task, _map_sync_task):
        if task:
            task.cancel()


app = FastAPI(
    title="Red de Esperanza",
    description="Sistema Centralizado de Rescate, Acopio y Refugios — Venezuela 2026",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(router)
app.include_router(config_router)

app.include_router(acopio_router)
app.include_router(auth_router)
app.include_router(firebase_router)
app.include_router(map_router)
app.include_router(volunteer_router)
app.include_router(search_router)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
if REFERENCE_PHOTOS.is_dir():
    app.mount("/reference_photos", StaticFiles(directory=str(REFERENCE_PHOTOS)), name="reference_photos")


@app.get("/")
async def tablero():
    return FileResponse(MOBILE / "index.html")


@app.get("/web")
async def tablero_web():
    return FileResponse(FRONTEND / "index.html")


@app.get("/mobile")
@app.get("/mobile/")
async def mobile_app():
    return FileResponse(MOBILE / "index.html")


@app.get("/familiar")
@app.get("/familiar/")
async def familiar_portal():
    return FileResponse(FAMILIAR / "index.html")


@app.get("/familiar/firebase.bundle.js")
async def familiar_firebase_bundle():
    return FileResponse(FAMILIAR / "firebase.bundle.js", media_type="application/javascript")


@app.get("/victim_detail.html")
async def victim_detail_page():
    return FileResponse(FRONTEND / "victim_detail.html")


@app.get("/manifest.json")
async def manifest():
    return FileResponse(FRONTEND / "manifest.json")


@app.get("/mobile/manifest.json")
async def mobile_manifest():
    return FileResponse(MOBILE / "manifest.json")


@app.get("/mobile/install.html")
async def mobile_install_page():
    return FileResponse(MOBILE / "install.html")


@app.get("/mobile/sw.js")
async def mobile_service_worker():
    return FileResponse(MOBILE / "sw.js", media_type="application/javascript")


app.mount("/mobile/icons", StaticFiles(directory=str(MOBILE / "icons")), name="mobile_icons")
app.mount("/mobile/js", StaticFiles(directory=str(MOBILE / "js")), name="mobile_js")