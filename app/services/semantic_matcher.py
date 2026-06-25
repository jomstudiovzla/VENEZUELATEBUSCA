"""Matching semántico de texto — MissingReport ↔ Survivor."""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.models import MissingReport, ReportStatus, Survivor

logger = logging.getLogger(__name__)

STOPWORDS = {
    "de", "la", "el", "en", "y", "a", "un", "una", "los", "las", "con", "por", "su", "se", "que",
    "del", "al", "es", "fue", "tiene", "tenia", "muy", "mas", "más", "sin", "para",
}


@dataclass
class MatchCandidate:
    report_id: str
    survivor_id: str
    score: float
    report_name: str
    survivor_name: str
    matched_tokens: list[str]


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"[^a-z0-9áéíóúñ\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _tokens(text: str) -> set[str]:
    return {t for t in _normalize(text).split() if len(t) > 2 and t not in STOPWORDS}


def _survivor_text(survivor: Survivor) -> str:
    parts = [survivor.name, survivor.estado_medico]
    traits = survivor.caracteristicas_fisicas or {}
    if isinstance(traits, dict):
        for key in ("ropa", "cicatrices", "tatuajes", "estatura", "descripcion", "notas", "pelo", "color"):
            val = traits.get(key)
            if val:
                parts.append(str(val))
    return " ".join(parts)


def _report_text(report: MissingReport) -> str:
    parts = [report.missing_person_name, report.description]
    if report.physical_traits:
        for v in report.physical_traits.values():
            if v:
                parts.append(str(v))
    if report.last_seen_location:
        parts.append(report.last_seen_location)
    return " ".join(parts)


def compute_similarity(report: MissingReport, survivor: Survivor) -> tuple[float, list[str]]:
    ta = _tokens(_report_text(report))
    tb = _tokens(_survivor_text(survivor))
    if not ta or not tb:
        return 0.0, []

    intersection = ta & tb
    union = ta | tb
    jaccard = len(intersection) / len(union)

    name_a = _normalize(report.missing_person_name)
    name_b = _normalize(survivor.name)
    name_bonus = 0.0
    if name_b != "desconocido":
        na, nb = set(name_a.split()), set(name_b.split())
        if na & nb:
            name_bonus = 0.25

    trait_keywords = {"tatuaje", "tatuajes", "cicatriz", "camisa", "pantalon", "estatura", "pelo", "ojos"}
    trait_hits = len(intersection & trait_keywords)
    trait_bonus = min(0.2, trait_hits * 0.08)

    score = min(1.0, jaccard * 0.65 + name_bonus + trait_bonus)
    return round(score, 3), sorted(intersection)[:12]


async def run_matching_cycle(session: AsyncSession) -> list[MatchCandidate]:
    reports = (
        await session.execute(
            select(MissingReport).where(MissingReport.status.in_([ReportStatus.ACTIVO, ReportStatus.POSIBLE_MATCH]))
        )
    ).scalars().all()
    survivors = (
        await session.execute(select(Survivor).where(Survivor.matched_report_id.is_(None)))
    ).scalars().all()

    if not reports or not survivors:
        return []

    matches: list[MatchCandidate] = []
    threshold = settings.match_threshold

    for report in reports:
        best: Optional[MatchCandidate] = None
        for survivor in survivors:
            score, tokens = compute_similarity(report, survivor)
            if score < threshold:
                continue
            candidate = MatchCandidate(
                report_id=report.id,
                survivor_id=survivor.id,
                score=score,
                report_name=report.missing_person_name,
                survivor_name=survivor.name,
                matched_tokens=tokens,
            )
            if best is None or candidate.score > best.score:
                best = candidate

        if best:
            report.status = ReportStatus.POSIBLE_MATCH
            report.matched_survivor_id = best.survivor_id
            report.match_score = best.score
            survivor = next(s for s in survivors if s.id == best.survivor_id)
            survivor.matched_report_id = best.report_id
            survivor.match_score = best.score
            matches.append(best)

    if matches:
        await session.flush()
        logger.info("Matching semántico | %d posibles coincidencias", len(matches))

    return matches