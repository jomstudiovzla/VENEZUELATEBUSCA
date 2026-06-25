"""Procesamiento YOLOv7-OA solo sobre fuentes autorizadas y voluntarias."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

import cv2
import numpy as np

from app.core.config import settings
from app.core.paths import AI_SNAPSHOTS

logger = logging.getLogger(__name__)

ALLOWED_SOURCES = frozenset({"crowdsourced_evidence", "drone_telemetry", "voluntary_cameras"})


class AIProcessor:
    """Detecta víctimas bajo escombros en videos/flujos consensuados."""

    def __init__(self) -> None:
        self._detector = None

    def _get_detector(self):
        if self._detector is None:
            from victim_detector import YOLOv7OADetector

            self._detector = YOLOv7OADetector(weights_path=settings.yolov7_weights)
        return self._detector

    def process_frame(
        self,
        frame: np.ndarray,
        *,
        source_type: str,
        source_id: str,
    ) -> dict[str, Any]:
        if source_type not in ALLOWED_SOURCES:
            raise ValueError(f"Fuente no autorizada para IA: {source_type}")

        detector = self._get_detector()
        detections = detector.detect(frame)
        snapshots: list[str] = []

        for idx, det in enumerate(detections[:5]):
            snap_name = f"{source_type}_{source_id}_{idx}.jpg"
            snap_path = AI_SNAPSHOTS / snap_name
            cv2.imwrite(str(snap_path), det.crop)
            snapshots.append(f"/uploads/ai_snapshots/{snap_name}")

        return {
            "source_type": source_type,
            "source_id": source_id,
            "person_count": len(detections),
            "confidence_max": max((d.confidence for d in detections), default=0.0),
            "snapshots": snapshots,
            "detections": [
                {
                    "bbox": list(det.bbox),
                    "confidence": round(det.confidence, 3),
                    "label": det.label,
                }
                for det in detections
            ],
        }

    def process_video_file(
        self,
        video_path: Path,
        *,
        source_type: str,
        source_id: str,
        sample_every_n: int = 30,
        max_frames: int = 120,
    ) -> dict[str, Any]:
        if source_type not in ALLOWED_SOURCES:
            raise ValueError(f"Fuente no autorizada para IA: {source_type}")

        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            raise RuntimeError(f"No se pudo abrir el video: {video_path}")

        total_persons = 0
        max_conf = 0.0
        all_snapshots: list[str] = []
        frames_read = 0
        processed = 0

        try:
            while processed < max_frames:
                ok, frame = cap.read()
                if not ok:
                    break
                frames_read += 1
                if frames_read % sample_every_n != 0:
                    continue
                processed += 1
                result = self.process_frame(frame, source_type=source_type, source_id=source_id)
                total_persons += result["person_count"]
                max_conf = max(max_conf, result["confidence_max"])
                all_snapshots.extend(result["snapshots"])
        finally:
            cap.release()

        return {
            "source_type": source_type,
            "source_id": source_id,
            "frames_sampled": processed,
            "total_detections": total_persons,
            "confidence_max": round(max_conf, 3),
            "snapshots": all_snapshots[:10],
        }

    def process_voluntary_stream_frame(
        self,
        frame: np.ndarray,
        camera_id: str,
    ) -> dict[str, Any]:
        return self.process_frame(frame, source_type="voluntary_cameras", source_id=camera_id)