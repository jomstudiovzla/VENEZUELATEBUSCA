"""Puente Firebase Realtime Database — proyecto Esperanzavzla."""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)
_firebase_app = None
_init_attempted = False


def firebase_enabled() -> bool:
    return bool(settings.firebase_project_id and settings.firebase_database_url)


def _messaging_sender_id() -> str:
    if settings.firebase_messaging_sender_id:
        return settings.firebase_messaging_sender_id
    app_id = (settings.firebase_app_id or "").strip()
    parts = app_id.split(":")
    return parts[1] if len(parts) > 1 else ""


def firebase_client_config() -> dict[str, str]:
    pid = settings.firebase_project_id
    return {
        "project_id": pid,
        "database_url": settings.firebase_database_url,
        "auth_domain": f"{pid.lower()}.firebaseapp.com" if pid else "",
        "api_key": settings.firebase_api_key,
        "app_id": settings.firebase_app_id,
        "messaging_sender_id": _messaging_sender_id(),
    }


def _rest_base() -> str:
    return settings.firebase_database_url.rstrip("/")


async def _rest_push(path: str, data: dict[str, Any]) -> Optional[str]:
    url = f"{_rest_base()}/{path.strip('/')}.json"
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.post(url, json=data)
            if r.status_code in (200, 201):
                body = r.json()
                return body.get("name") if isinstance(body, dict) else None
            logger.warning("Firebase REST %s → HTTP %s: %s", path, r.status_code, r.text[:200])
    except Exception:
        logger.exception("Firebase REST push falló: %s", path)
    return None


async def _rest_set(path: str, data: dict[str, Any]) -> bool:
    url = f"{_rest_base()}/{path.strip('/')}.json"
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.put(url, json=data)
            return r.status_code in (200, 201)
    except Exception:
        logger.exception("Firebase REST set falló: %s", path)
        return False


def _init_firebase_admin():
    global _firebase_app, _init_attempted
    if _firebase_app is not None:
        return _firebase_app
    if _init_attempted:
        return None
    _init_attempted = True
    if not firebase_enabled():
        return None
    if not settings.firebase_credentials_path:
        logger.info(
            "Firebase %s: modo REST (sin service account). "
            "Coloca FIREBASE_CREDENTIALS_PATH para escritura admin.",
            settings.firebase_project_id,
        )
        return None
    try:
        import firebase_admin
        from firebase_admin import credentials

        cred = credentials.Certificate(settings.firebase_credentials_path)
        _firebase_app = firebase_admin.initialize_app(cred, {
            "databaseURL": settings.firebase_database_url,
            "projectId": settings.firebase_project_id,
        })
        logger.info("Firebase Admin conectado: %s", settings.firebase_project_id)
        return _firebase_app
    except Exception:
        logger.exception("No se pudo inicializar Firebase Admin")
        return None


async def _admin_push(path: str, data: dict[str, Any]) -> Optional[str]:
    app = _init_firebase_admin()
    if not app:
        return None
    try:
        from firebase_admin import db
        ref = db.reference(f"/{path.strip('/')}").push(data)
        return ref.key
    except Exception:
        logger.exception("Firebase Admin push falló: %s", path)
        return None


async def _admin_set(path: str, data: dict[str, Any]) -> bool:
    app = _init_firebase_admin()
    if not app:
        return False
    try:
        from firebase_admin import db
        db.reference(f"/{path.strip('/')}").set(data)
        return True
    except Exception:
        logger.exception("Firebase Admin set falló: %s", path)
        return False


async def push_testimony_to_firebase(testimony: dict[str, Any]) -> bool:
    if not firebase_enabled():
        return False
    key = await _admin_push("testimonios", testimony)
    if key:
        return True
    key = await _rest_push("testimonios", testimony)
    return bool(key)


async def push_victim_update_to_firebase(victim: dict[str, Any]) -> bool:
    if not firebase_enabled():
        return False
    vid = victim.get("id")
    if vid is None:
        return False
    if await _admin_set(f"victimas/{vid}", victim):
        return True
    return await _rest_set(f"victimas/{vid}", victim)


async def push_family_search(query: dict[str, Any]) -> Optional[str]:
    if not firebase_enabled():
        return None
    key = await _admin_push("busquedas_familiares", query)
    if key:
        return key
    return await _rest_push("busquedas_familiares", query)


async def fetch_firebase_node(path: str) -> Any:
    if not firebase_enabled():
        return None
    url = f"{_rest_base()}/{path.strip('/')}.json"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url)
            if r.status_code == 200:
                return r.json()
    except Exception:
        logger.debug("Firebase REST read falló: %s", path)
    return None


async def firebase_health() -> dict[str, Any]:
    if not firebase_enabled():
        return {"enabled": False, "project_id": "", "reachable": False}
    reachable = False
    detail = ""
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(f"{_rest_base()}/.json")
            reachable = r.status_code in (200, 401, 403, 404)
            detail = f"HTTP {r.status_code}"
    except Exception as exc:
        detail = str(exc)
    return {
        "enabled": True,
        "project_id": settings.firebase_project_id,
        "database_url": settings.firebase_database_url,
        "reachable": reachable,
        "detail": detail,
        "admin_sdk": bool(settings.firebase_credentials_path),
        "client": firebase_client_config(),
    }