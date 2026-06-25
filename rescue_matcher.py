"""Motor de matching SAR: cruza biometría blanda contra desaparecidos registrados."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import MissingPerson, MissingStatus, MissingVictim, RescueAlert, settings
from height_estimator import HeightEstimate
from tattoo_analyzer import TattooAnalyzer, TattooRegion
from victim_detector import Detection

logger = logging.getLogger(__name__)


@dataclass
class MatchResult:
    victim_id: int
    victim_name: str
    confidence: float
    tattoo_similarity: float
    height_delta_cm: float
    estimated_height_cm: float
    registered_height_cm: Optional[float]


class RescueMatcher:
    """Compara embeddings de tatuajes y estatura contra la base de desaparecidos."""

    def __init__(self, tattoo_analyzer: Optional[TattooAnalyzer] = None):
        self.tattoo_analyzer = tattoo_analyzer or TattooAnalyzer()

    async def fetch_missing_victims(self, session: AsyncSession) -> list[MissingVictim]:
        result = await session.execute(
            select(MissingVictim).where(MissingVictim.status == MissingStatus.DESAPARECIDO)
        )
        return list(result.scalars().all())

    def _height_score(
        self,
        estimated_cm: float,
        registered_cm: Optional[float],
        tolerance: float,
    ) -> tuple[float, float]:
        if registered_cm is None or estimated_cm <= 0:
            return 0.5, 0.0
        delta = abs(estimated_cm - registered_cm)
        if delta <= tolerance:
            score = 1.0 - (delta / tolerance) * 0.5
        else:
            score = max(0.0, 0.3 - (delta - tolerance) / (tolerance * 3))
        return score, delta

    def _combined_confidence(
        self,
        tattoo_similarity: float,
        height_score: float,
        detection_confidence: float,
    ) -> float:
        return float(
            np.clip(
                0.55 * tattoo_similarity + 0.30 * height_score + 0.15 * detection_confidence,
                0.0,
                1.0,
            )
        )

    async def match_detection(
        self,
        session: AsyncSession,
        detection: Detection,
        tattoo_regions: list[TattooRegion],
        height_estimate: HeightEstimate,
        feed_id: int,
        frame_snapshot_path: Optional[str] = None,
        alert_threshold: float = 0.65,
    ) -> Optional[MatchResult]:
        victims = await self.fetch_missing_victims(session)
        if not victims:
            return None

        query_embeddings = [r.embedding for r in tattoo_regions]
        best_match: Optional[MatchResult] = None

        for victim in victims:
            ref_embeddings = []
            if victim.tattoo_embeddings:
                ref_embeddings = [np.array(e, dtype=np.float32) for e in victim.tattoo_embeddings]

            tattoo_sim, _ = self.tattoo_analyzer.best_match(query_embeddings, ref_embeddings)
            height_score, height_delta = self._height_score(
                height_estimate.height_cm,
                victim.height_cm,
                settings.height_tolerance_cm,
            )

            if not ref_embeddings:
                tattoo_sim = 0.4 if victim.distinguishing_marks else 0.0

            confidence = self._combined_confidence(
                tattoo_sim, height_score, detection.confidence
            )

            if best_match is None or confidence > best_match.confidence:
                best_match = MatchResult(
                    victim_id=victim.id,
                    victim_name=victim.full_name,
                    confidence=confidence,
                    tattoo_similarity=tattoo_sim,
                    height_delta_cm=height_delta,
                    estimated_height_cm=height_estimate.height_cm,
                    registered_height_cm=victim.height_cm,
                )

        if best_match is None or best_match.confidence < alert_threshold:
            return None

        alert = RescueAlert(
            victim_id=best_match.victim_id,
            feed_id=feed_id,
            confidence=best_match.confidence,
            tattoo_similarity=best_match.tattoo_similarity,
            height_delta_cm=best_match.height_delta_cm,
            frame_snapshot_path=frame_snapshot_path,
            bbox={
                "x1": detection.bbox[0],
                "y1": detection.bbox[1],
                "x2": detection.bbox[2],
                "y2": detection.bbox[3],
            },
            acknowledged=False,
            created_at=datetime.now(timezone.utc),
        )
        session.add(alert)

        victim_record = await session.get(MissingVictim, best_match.victim_id)
        if victim_record:
            victim_record.status = MissingStatus.LOCALIZADO

        logger.critical(
            "ALERTA DE RESCATE | víctima=%s | confianza=%.2f | feed=%d",
            best_match.victim_name,
            best_match.confidence,
            feed_id,
        )

        return best_match