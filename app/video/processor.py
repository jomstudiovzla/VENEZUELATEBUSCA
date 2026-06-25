"""Utilidades de video para fuentes autorizadas (sin interceptación de red)."""

from __future__ import annotations

from pathlib import Path

from app.services.ai_processor import AIProcessor


class VideoProcessor:
    def __init__(self) -> None:
        self.ai = AIProcessor()

    def analyze_evidence(self, video_path: Path, evidence_id: str) -> dict:
        return self.ai.process_video_file(
            video_path,
            source_type="crowdsourced_evidence",
            source_id=evidence_id,
        )

    def analyze_drone_clip(self, video_path: Path, drone_id: str) -> dict:
        return self.ai.process_video_file(
            video_path,
            source_type="drone_telemetry",
            source_id=drone_id,
        )