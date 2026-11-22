"""Local multilingual embeddings via model2vec (no GPU, lazy load)."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Sequence

from ..contracts.memory import Embedder
from .config import EmbeddingConfig

logger = logging.getLogger(__name__)


class Model2VecEmbedder(Embedder):
    """Static multilingual embeddings, loaded on first use."""

    def __init__(
        self,
        model_name: str,
        dimension: int,
        *,
        cache_dir: Path | str | None = None,
    ) -> None:
        self.model_name = model_name
        self.dimension = dimension
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._model = None

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        model = self._load()
        vectors = model.encode(list(texts), normalize=True, show_progress_bar=False)
        if vectors.shape[1] != self.dimension:
            raise RuntimeError(
                f"embedding model returns {vectors.shape[1]} dims, config says {self.dimension}"
            )
        return vectors.tolist()

    def _load(self):
        if self._model is not None:
            return self._model
        if self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            os.environ.setdefault("HF_HOME", str(self.cache_dir))
        try:
            from model2vec import StaticModel
        except ImportError as exc:
            raise RuntimeError(
                "model2vec is not installed; run `pip install model2vec`"
            ) from exc
        logger.info("loading embedding model %s", self.model_name)
        self._model = StaticModel.from_pretrained(
            self.model_name, force_download=False
        )
        actual = getattr(self._model, "dim", None)
        if actual is not None and int(actual) != self.dimension:
            raise RuntimeError(
                f"embedding model dimension is {actual}, config says {self.dimension}"
            )
        return self._model


def build_embedder(config, cache_dir: Path | str) -> Embedder | None:
    """Build from `memory.embedding`; None means FTS-only recall."""
    embedding: EmbeddingConfig | None = getattr(config, "embedding", None)
    if embedding is None:
        return None
    return Model2VecEmbedder(
        embedding.model, embedding.dimension, cache_dir=cache_dir
    )
