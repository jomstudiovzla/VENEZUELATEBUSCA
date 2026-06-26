"""Autenticación de verificadores de centros de acopio."""

from __future__ import annotations

from fastapi import HTTPException

VERIFIER_ROLE = "VERIFICADOR_ACOPIO"


def is_acopio_verifier(role: str | None) -> bool:
    return (role or "").strip().upper() == VERIFIER_ROLE


def require_acopio_verifier(role: str | None) -> None:
    if not is_acopio_verifier(role):
        raise HTTPException(
            403,
            "Acceso denegado: se requiere rol VERIFICADOR_ACOPIO para moderar centros de acopio",
        )