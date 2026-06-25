"""Ojo de Dios — Centro de Comando SAR-DVI (sin workers de descarga automática)."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.core.config import settings
from app.core.database import init_db
from app.core.paths import AI_SNAPSHOTS, EVIDENCE_VIDEOS, MEDICAL_PHOTOS, REFERENCE_PHOTOS, ROOT

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ojo_de_dios")

FRONTEND_DIR = ROOT / "app" / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info(
        "Centro de Comando SAR-DVI iniciado | modo=crowdsourcing | workers_tiempo_real=%s",
        settings.enable_realtime_workers,
    )
    yield
    logger.info("Centro de Comando detenido")


app = FastAPI(
    title="Ojo de Dios — SAR/DVI",
    description="Centro de Comando unificado: crowdsourcing autorizado, triaje médico y DVI",
    version="2.0.0",
    lifespan=lifespan,
)

app.include_router(router)

app.mount("/uploads/medical_triage", StaticFiles(directory=str(MEDICAL_PHOTOS)), name="medical_photos")
app.mount("/uploads/crowdsourced_evidence", StaticFiles(directory=str(EVIDENCE_VIDEOS)), name="evidence_videos")
app.mount("/uploads/ai_snapshots", StaticFiles(directory=str(AI_SNAPSHOTS)), name="ai_snapshots")
if REFERENCE_PHOTOS.exists():
    app.mount("/photos", StaticFiles(directory=str(REFERENCE_PHOTOS)), name="photos")


@app.get("/")
async def root():
    index = FRONTEND_DIR / "index.html"
    if index.exists():
        return FileResponse(index)
    return RedirectResponse("/docs")


@app.get("/command-center")
async def command_center():
    return FileResponse(FRONTEND_DIR / "index.html")