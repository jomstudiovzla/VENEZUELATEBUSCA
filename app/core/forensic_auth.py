"""Autenticación de roles forenses — candado post-mortem DVI."""

from __future__ import annotations

from fastapi import Header, HTTPException

FORENSIC_ROLE = "ADMIN_FORENSE"


def is_forensic_admin(role: str | None) -> bool:
    return (role or "").strip().upper() == FORENSIC_ROLE


def require_forensic_admin(role: str | None) -> None:
    if not is_forensic_admin(role):
        raise HTTPException(
            403,
            "Acceso denegado: se requiere rol ADMIN_FORENSE y confirmación post-mortem oficial",
        )


async def forensic_role_header(
    x_forensic_role: str | None = Header(None, alias="X-Forensic-Role"),
) -> str | None:
    return x_forensic_role