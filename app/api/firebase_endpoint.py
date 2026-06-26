"""Configuración Firebase para portal familiar (Esperanzavzla)."""

from __future__ import annotations

from fastapi import APIRouter

from app.services.firebase_bridge import firebase_client_config, firebase_health, fetch_firebase_node

router = APIRouter(tags=["firebase"])


@router.get("/api/firebase/config")
async def firebase_config():
    """Config pública para el SDK web del portal familiar."""
    cfg = firebase_client_config()
    return {
        **cfg,
        "storage_bucket": f"{cfg['project_id'].lower()}.appspot.com" if cfg["project_id"] else "",
        "sync_interval_ms": 250,
    }


@router.get("/api/firebase/status")
async def firebase_status():
    return await firebase_health()


@router.get("/api/firebase/victimas")
async def firebase_victimas():
    data = await fetch_firebase_node("victimas")
    if not data or not isinstance(data, dict):
        return {"items": [], "count": 0}
    items = list(data.values()) if isinstance(data, dict) else []
    return {"items": items, "count": len(items)}


@router.get("/api/firebase/testimonios")
async def firebase_testimonios():
    data = await fetch_firebase_node("testimonios")
    if not data or not isinstance(data, dict):
        return {"items": [], "count": 0}
    items = sorted(
        data.values(),
        key=lambda x: x.get("created_at", "") if isinstance(x, dict) else "",
        reverse=True,
    )[:100]
    return {"items": items, "count": len(items)}