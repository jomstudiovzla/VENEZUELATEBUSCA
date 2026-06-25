"""Extracción de biometría blanda: embeddings de tatuajes vía CBIR (SIFT + CNN)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np
import torch
import torch.nn as nn
import torchvision.transforms as T
from PIL import Image

logger = logging.getLogger(__name__)

EMBEDDING_DIM = 256


class TattooCNNEncoder(nn.Module):
    """Encoder CNN ligero para refinar embeddings de regiones de tatuaje."""

    def __init__(self, output_dim: int = EMBEDDING_DIM):
        super().__init__()
        self.features = nn.Sequential(
            nn.Conv2d(3, 32, 3, padding=1),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2),
            nn.Conv2d(32, 64, 3, padding=1),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2),
            nn.Conv2d(64, 128, 3, padding=1),
            nn.ReLU(inplace=True),
            nn.AdaptiveAvgPool2d((4, 4)),
        )
        self.head = nn.Sequential(
            nn.Flatten(),
            nn.Linear(128 * 4 * 4, output_dim),
            nn.ReLU(inplace=True),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.head(self.features(x))


@dataclass
class TattooRegion:
    bbox: tuple[int, int, int, int]
    sift_keypoints: int
    embedding: np.ndarray


class TattooAnalyzer:
    """Aísla marcas corporales y genera vectores matemáticos para matching forense."""

    def __init__(self, device: Optional[str] = None):
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.sift = cv2.SIFT_create(nfeatures=500)
        self.cnn = TattooCNNEncoder().to(self.device)
        self.cnn.eval()
        self.transform = T.Compose(
            [
                T.Resize((128, 128)),
                T.ToTensor(),
                T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
            ]
        )

    def _skin_mask(self, image: np.ndarray) -> np.ndarray:
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        lower = np.array([0, 20, 70], dtype=np.uint8)
        upper = np.array([25, 255, 255], dtype=np.uint8)
        mask1 = cv2.inRange(hsv, lower, upper)
        lower2 = np.array([160, 20, 70], dtype=np.uint8)
        upper2 = np.array([180, 255, 255], dtype=np.uint8)
        mask2 = cv2.inRange(hsv, lower2, upper2)
        skin = cv2.bitwise_or(mask1, mask2)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        return cv2.morphologyEx(skin, cv2.MORPH_CLOSE, kernel)

    def _find_tattoo_candidates(self, image: np.ndarray) -> list[tuple[int, int, int, int]]:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        skin = self._skin_mask(image)
        masked = cv2.bitwise_and(gray, gray, mask=skin)

        edges = cv2.Canny(masked, 40, 120)
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        candidates: list[tuple[int, int, int, int]] = []
        h, w = image.shape[:2]
        for contour in contours:
            x, y, cw, ch = cv2.boundingRect(contour)
            area = cw * ch
            if area < 400 or area > (h * w * 0.35):
                continue
            if ch / max(cw, 1) > 6 or cw / max(ch, 1) > 6:
                continue
            candidates.append((x, y, x + cw, y + ch))

        return candidates[:5]

    def _sift_descriptor(self, region: np.ndarray) -> Optional[np.ndarray]:
        gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
        keypoints, descriptors = self.sift.detectAndCompute(gray, None)
        if descriptors is None or len(descriptors) == 0:
            return None
        return descriptors.mean(axis=0).astype(np.float32)

    def _cnn_embedding(self, region: np.ndarray) -> np.ndarray:
        pil = Image.fromarray(cv2.cvtColor(region, cv2.COLOR_BGR2RGB))
        tensor = self.transform(pil).unsqueeze(0).to(self.device)
        with torch.no_grad():
            vec = self.cnn(tensor).cpu().numpy().flatten()
        return vec.astype(np.float32)

    def _fuse_embeddings(
        self, sift_vec: Optional[np.ndarray], cnn_vec: np.ndarray
    ) -> np.ndarray:
        if sift_vec is None:
            fused = np.zeros(EMBEDDING_DIM + 128, dtype=np.float32)
            fused[128:] = cnn_vec
        else:
            sift_padded = np.zeros(128, dtype=np.float32)
            sift_padded[: min(128, len(sift_vec))] = sift_vec[:128]
            fused = np.concatenate([sift_padded, cnn_vec])

        norm = np.linalg.norm(fused)
        return fused / norm if norm > 1e-8 else fused

    def extract_from_crop(self, body_crop: np.ndarray) -> list[TattooRegion]:
        regions = self._find_tattoo_candidates(body_crop)
        results: list[TattooRegion] = []

        for x1, y1, x2, y2 in regions:
            patch = body_crop[y1:y2, x1:x2]
            if patch.size == 0:
                continue

            sift_desc = self._sift_descriptor(patch)
            cnn_emb = self._cnn_embedding(patch)
            embedding = self._fuse_embeddings(sift_desc, cnn_emb)

            results.append(
                TattooRegion(
                    bbox=(x1, y1, x2, y2),
                    sift_keypoints=0 if sift_desc is None else 128,
                    embedding=embedding,
                )
            )

        return results

    @staticmethod
    def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
        denom = np.linalg.norm(a) * np.linalg.norm(b)
        if denom < 1e-8:
            return 0.0
        return float(np.dot(a, b) / denom)

    def best_match(
        self,
        query_embeddings: list[np.ndarray],
        reference_embeddings: list[np.ndarray],
    ) -> tuple[float, int]:
        if not query_embeddings or not reference_embeddings:
            return 0.0, -1

        best_score = -1.0
        best_idx = -1
        for idx, ref in enumerate(reference_embeddings):
            ref_vec = np.array(ref, dtype=np.float32)
            for query in query_embeddings:
                score = self.cosine_similarity(query, ref_vec)
                if score > best_score:
                    best_score = score
                    best_idx = idx

        return best_score, best_idx