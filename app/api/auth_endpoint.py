"""Login piramidal y registro de usuarios por rol."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import (
    ROLE_CAN_CREATE,
    can_create_role,
    create_token,
    get_current_user,
    hash_password,
    require_role,
    verify_password,
)
from app.core.database import get_session
from app.models.models import User, UserRole

router = APIRouter(tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(..., description="ID de usuario, ej: JOM")
    password: str


class RegisterUserRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=64)
    password: str = Field(..., min_length=4)
    display_name: str = Field(..., min_length=2)
    role: str
    phone: Optional[str] = None


def _user_card(u: User) -> dict:
    return {
        "id": u.id,
        "username": u.username,
        "display_name": u.display_name,
        "role": u.role,
        "phone": u.phone,
        "parent_id": u.parent_id,
    }


@router.post("/api/auth/login")
async def login(payload: LoginRequest, session: AsyncSession = Depends(get_session)):
    username = payload.username.strip()
    user = await session.scalar(select(User).where(User.username == username))
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(401, "Usuario o contraseña incorrectos")
    if not user.is_active:
        raise HTTPException(403, "Cuenta desactivada")
    token = create_token(user)
    return {
        "token": token,
        "user": _user_card(user),
        "permissions": {
            "can_register": list(ROLE_CAN_CREATE.get(user.role, set())),
            "is_super_admin": user.role == UserRole.SUPER_ADMIN.value,
        },
    }


@router.get("/api/auth/me")
async def me(user: User = Depends(get_current_user)):
    return {
        "user": _user_card(user),
        "permissions": {
            "can_register": list(ROLE_CAN_CREATE.get(user.role, set())),
            "is_super_admin": user.role == UserRole.SUPER_ADMIN.value,
        },
    }


@router.post("/api/auth/register", status_code=201)
async def register_user(
    payload: RegisterUserRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    require_role(
        user,
        UserRole.SUPER_ADMIN.value,
        UserRole.ADMIN.value,
        UserRole.VERIFICADOR_ACOPIO.value,
        UserRole.PARAMEDICO.value,
    )
    if not can_create_role(user, payload.role):
        raise HTTPException(403, f"Tu rol no puede crear usuarios con rol {payload.role}")
    exists = await session.scalar(select(User).where(User.username == payload.username.strip()))
    if exists:
        raise HTTPException(409, "El usuario ya existe")
    new_user = User(
        username=payload.username.strip(),
        password_hash=hash_password(payload.password),
        display_name=payload.display_name.strip(),
        role=payload.role,
        parent_id=user.id,
        phone=payload.phone,
    )
    session.add(new_user)
    await session.flush()
    return _user_card(new_user)