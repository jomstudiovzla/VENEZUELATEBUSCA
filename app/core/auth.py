"""Autenticación piramidal — JWT + roles jerárquicos."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import jwt
from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_session
from app.models.models import User, UserRole

ROLE_HIERARCHY: dict[str, int] = {
    UserRole.SUPER_ADMIN.value: 100,
    UserRole.ADMIN.value: 80,
    UserRole.VERIFICADOR_ACOPIO.value: 60,
    UserRole.PARAMEDICO.value: 50,
    UserRole.VOLUNTARIO.value: 30,
    UserRole.FAMILIAR.value: 10,
}

ROLE_CAN_CREATE: dict[str, set[str]] = {
    UserRole.SUPER_ADMIN.value: {
        UserRole.ADMIN.value,
        UserRole.VERIFICADOR_ACOPIO.value,
        UserRole.PARAMEDICO.value,
        UserRole.VOLUNTARIO.value,
        UserRole.FAMILIAR.value,
    },
    UserRole.ADMIN.value: {
        UserRole.VERIFICADOR_ACOPIO.value,
        UserRole.PARAMEDICO.value,
        UserRole.VOLUNTARIO.value,
        UserRole.FAMILIAR.value,
    },
    UserRole.VERIFICADOR_ACOPIO.value: {UserRole.VOLUNTARIO.value},
    UserRole.PARAMEDICO.value: {UserRole.VOLUNTARIO.value},
}


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000)
    return f"{salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, digest = stored.split("$", 1)
    except ValueError:
        return False
    check = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000).hex()
    return secrets.compare_digest(check, digest)


def create_token(user: User) -> str:
    payload = {
        "sub": user.id,
        "username": user.username,
        "role": user.role,
        "name": user.display_name,
        "exp": datetime.now(timezone.utc) + timedelta(hours=settings.jwt_expire_hours),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_token(token: str) -> dict[str, Any]:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise HTTPException(401, "Token inválido o expirado") from exc


async def get_current_user(
    authorization: str | None = Header(None),
    session: AsyncSession = Depends(get_session),
) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Se requiere autenticación Bearer")
    token = authorization.split(" ", 1)[1].strip()
    data = decode_token(token)
    user = await session.get(User, data["sub"])
    if not user or not user.is_active:
        raise HTTPException(401, "Usuario inactivo o no encontrado")
    return user


def require_role(user: User, *roles: str) -> None:
    if user.role not in roles and user.role != UserRole.SUPER_ADMIN.value:
        raise HTTPException(403, f"Rol {user.role} no autorizado para esta acción")


def can_create_role(creator: User, new_role: str) -> bool:
    if creator.role == UserRole.SUPER_ADMIN.value:
        return True
    allowed = ROLE_CAN_CREATE.get(creator.role, set())
    return new_role in allowed