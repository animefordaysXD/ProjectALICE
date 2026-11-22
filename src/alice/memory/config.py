"""Memory limits, the core-memory file, and the embedding model choice."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.config import (
    ConfigError,
    as_mapping,
    get_integer,
    get_string,
    reject_unknown,
    resolve_path,
)


@dataclass(frozen=True)
class EmbeddingConfig:
    model: str
    dimension: int


@dataclass(frozen=True)
class MemoryConfig:
    core_memory_file: Path
    hard_max_tokens: int = 2000
    task_ttl_hours: int = 48
    recall_budget_tokens: int = 1000
    recall_k: int = 5
    embedding: EmbeddingConfig | None = None


def parse_memory(raw: Any, data_dir: Path) -> MemoryConfig:
    mapping = as_mapping(raw or {}, "memory")
    reject_unknown(
        mapping,
        {
            "core_memory_file",
            "hard_max_tokens",
            "task_ttl_hours",
            "recall_budget_tokens",
            "recall_k",
            "embedding",
        },
        "memory",
    )
    core_file = resolve_path(
        Path(
            get_string(
                mapping, "core_memory_file", "memory", default="memory/core.json"
            )
        ),
        data_dir,
    )
    hard_max = get_integer(mapping, "hard_max_tokens", "memory", default=2000)
    ttl = get_integer(mapping, "task_ttl_hours", "memory", default=48)
    recall_budget = get_integer(mapping, "recall_budget_tokens", "memory", default=1000)
    recall_k = get_integer(mapping, "recall_k", "memory", default=5)
    if hard_max <= 0:
        raise ConfigError("memory.hard_max_tokens must be greater than zero")
    if ttl <= 0:
        raise ConfigError("memory.task_ttl_hours must be greater than zero")
    if recall_budget <= 0:
        raise ConfigError("memory.recall_budget_tokens must be greater than zero")
    if recall_k <= 0:
        raise ConfigError("memory.recall_k must be greater than zero")

    embedding = None
    if "embedding" in mapping and mapping["embedding"] is not None:
        emb = as_mapping(mapping["embedding"], "memory.embedding")
        reject_unknown(emb, {"model", "dimension"}, "memory.embedding")
        model = get_string(emb, "model", "memory.embedding")
        dimension = get_integer(emb, "dimension", "memory.embedding", default=0)
        if dimension <= 0:
            raise ConfigError("memory.embedding.dimension must be greater than zero")
        embedding = EmbeddingConfig(model=model, dimension=dimension)

    return MemoryConfig(
        core_memory_file=core_file,
        hard_max_tokens=hard_max,
        task_ttl_hours=ttl,
        recall_budget_tokens=recall_budget,
        recall_k=recall_k,
        embedding=embedding,
    )
