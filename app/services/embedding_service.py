from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

import numpy as np

from app.core.config import ROOT_DIR, settings

QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："


@dataclass(frozen=True)
class EmbeddingResult:
    vector: np.ndarray
    content_hash: str


class EmbeddingService:
    """GPU 优先的本地中文向量化服务。"""

    def __init__(self, model_name: str | None = None) -> None:
        self.model_name = model_name or settings.embedding_model
        self._model: Any | None = None

    def _load_model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(
                self.model_name,
                cache_folder=str(ROOT_DIR / "models"),
                device=self.device,
                local_files_only=True,
            )
        return self._model

    @property
    def device(self) -> str:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"

    def encode_documents(self, texts: list[str]) -> list[EmbeddingResult]:
        if not texts:
            return []
        model = self._load_model()
        vectors = model.encode(
            texts,
            batch_size=settings.embedding_batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return [
            EmbeddingResult(
                vector=np.asarray(vector, dtype=np.float32),
                content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            )
            for text, vector in zip(texts, vectors, strict=True)
        ]

    def encode_query(self, query: str) -> np.ndarray:
        result = self.encode_documents([f"{QUERY_PREFIX}{query}"])[0]
        return result.vector

    @staticmethod
    def to_bytes(vector: np.ndarray) -> bytes:
        return np.asarray(vector, dtype=np.float32).tobytes()

    @staticmethod
    def from_bytes(value: bytes) -> np.ndarray:
        return np.frombuffer(value, dtype=np.float32)


class FakeEmbeddingService(EmbeddingService):
    """测试使用的确定性向量器，不依赖模型权重。"""

    def __init__(self, dimension: int = 8) -> None:
        super().__init__(model_name="fake-embedding")
        self.dimension = dimension

    def encode_documents(self, texts: list[str]) -> list[EmbeddingResult]:
        results = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            vector = np.frombuffer(digest[: self.dimension * 4], dtype=np.uint8).astype(np.float32)
            vector = vector[: self.dimension]
            norm = float(np.linalg.norm(vector)) or 1.0
            results.append(
                EmbeddingResult(
                    vector=vector / norm,
                    content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                )
            )
        return results

    def encode_query(self, query: str) -> np.ndarray:
        return self.encode_documents([query])[0].vector
