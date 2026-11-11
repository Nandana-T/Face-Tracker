"""In-memory cosine-similarity index over registered embeddings (controlled, authoritative DB).

Why not pgvector / a vector database: the dataset is small (hundreds to a few thousand visitors x
<= a handful of embeddings of 128 floats), an exact numpy dot product over it takes microseconds, and
the authoritative copy is PostgreSQL - the index is rebuilt from it at start-up and updated
incrementally. It never compares vectors from different model versions (separate partitions).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Match:
    visitor_id: str
    similarity: float


@dataclass(frozen=True)
class MatchResult:
    best: Match | None
    second: Match | None  # best candidate of a DIFFERENT visitor

    @property
    def margin(self) -> float:
        if self.best is None:
            return 0.0
        return self.best.similarity - (self.second.similarity if self.second else -1.0)


class EmbeddingIndex:
    """Partitioned by ``model_version``; rows are (visitor_id, unit vector)."""

    def __init__(self) -> None:
        self._vectors: dict[str, np.ndarray] = {}
        self._owners: dict[str, list[str]] = {}
        self._ids: dict[str, set[str]] = {}  # embedding ids already present (idempotent add)

    def __len__(self) -> int:
        return sum(len(o) for o in self._owners.values())

    def visitor_count(self, model_version: str) -> int:
        return len(set(self._owners.get(model_version, [])))

    def add(self, model_version: str, visitor_id: str, vector: np.ndarray, key: str) -> bool:
        """Add one embedding; returns False if ``key`` was already indexed."""
        ids = self._ids.setdefault(model_version, set())
        if key in ids:
            return False
        v = np.asarray(vector, dtype=np.float32).reshape(1, -1)
        current = self._vectors.get(model_version)
        if current is not None and current.shape[1] != v.shape[1]:
            raise ValueError("embedding dimension differs from the index partition")
        self._vectors[model_version] = v if current is None else np.vstack([current, v])
        self._owners.setdefault(model_version, []).append(visitor_id)
        ids.add(key)
        return True

    def remove_visitor(self, visitor_id: str) -> int:
        removed = 0
        for version, owners in list(self._owners.items()):
            keep = [i for i, o in enumerate(owners) if o != visitor_id]
            removed += len(owners) - len(keep)
            if len(keep) != len(owners):
                self._owners[version] = [owners[i] for i in keep]
                vectors = self._vectors[version]
                self._vectors[version] = vectors[keep] if keep else vectors[:0]
        return removed

    def clear(self) -> None:
        self._vectors.clear()
        self._owners.clear()
        self._ids.clear()

    def search(self, model_version: str, vector: np.ndarray) -> MatchResult:
        """Best match and the best match belonging to a different visitor (for the ambiguity margin)."""
        matrix = self._vectors.get(model_version)
        owners = self._owners.get(model_version)
        if matrix is None or not owners:
            return MatchResult(None, None)
        sims = matrix @ np.asarray(vector, dtype=np.float32).reshape(-1)
        best_per_visitor: dict[str, float] = {}
        for owner, s in zip(owners, sims.tolist(), strict=True):
            if s > best_per_visitor.get(owner, -2.0):
                best_per_visitor[owner] = s
        ranked = sorted(best_per_visitor.items(), key=lambda kv: kv[1], reverse=True)
        best = Match(ranked[0][0], ranked[0][1])
        second = Match(ranked[1][0], ranked[1][1]) if len(ranked) > 1 else None
        return MatchResult(best, second)
