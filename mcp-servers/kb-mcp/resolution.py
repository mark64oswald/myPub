"""
resolution.py — Entity resolution for concept extraction.

Per architecture doc §5.2, incoming concept candidates pass through three stages:

    1. Exact name match   (case-insensitive on concept.name)
    2. Alias match        (case-insensitive lookup in concept_alias)
    3. Embedding match    (cosine similarity against concept_embedding)

Embedding-stage thresholds (cosine similarity, not distance):

    ≥ 0.90  → auto-match to nearest existing concept
    0.75–0.89 → borderline: provisionally create a new concept with
                pending_review=True and enqueue to concept_resolution_queue
    < 0.75 → genuinely new: create a new concept cleanly

Every path returns a `ResolveResult` carrying the concept_id, whether it's
newly created, and a type tag suitable for logging/analytics.

Everything that creates a concept also writes to concept_embedding, so
future resolutions will see the new node.

The caller owns the DuckDB connection. This module commits nothing; the
caller decides transaction boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import duckdb

DEFAULT_EMBEDDING_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_EMBED_DIM = 384
DEFAULT_HIGH_THRESHOLD = 0.90      # similarity — above here is auto-match
DEFAULT_LOW_THRESHOLD = 0.75       # similarity — below here is a new concept

RESOLUTION_TYPES = {"exact", "alias", "embedding_high", "borderline", "new"}


@dataclass
class ResolveResult:
    """Outcome of resolving one candidate concept.

    `similarity` is populated for embedding-stage outcomes (embedding_high,
    borderline, and for `new` when there was a nearest-match below the
    borderline threshold). `nearest_concept_id` is populated when there was
    *some* neighbor — useful for audit/debug even on new-concept outcomes.
    """

    concept_id: int
    is_new: bool
    resolution_type: str
    similarity: Optional[float] = None
    nearest_concept_id: Optional[int] = None


class EntityResolver:
    """Resolve a candidate concept name+context to an existing or new concept.

    The resolver holds a sentence-transformers model for embedding the
    candidate; callers can inject a pre-loaded model (recommended when
    resolving many candidates in a loop) or let the class lazy-load on
    first use.
    """

    def __init__(
        self,
        conn: duckdb.DuckDBPyConnection,
        model=None,
        *,
        high_threshold: float = DEFAULT_HIGH_THRESHOLD,
        low_threshold: float = DEFAULT_LOW_THRESHOLD,
        embed_dim: int = DEFAULT_EMBED_DIM,
        model_name: str = DEFAULT_EMBEDDING_MODEL_NAME,
    ) -> None:
        if not 0 < low_threshold < high_threshold <= 1.0:
            raise ValueError(
                f"thresholds must satisfy 0 < low ({low_threshold}) < "
                f"high ({high_threshold}) ≤ 1.0"
            )
        self.conn = conn
        self._model = model
        self._model_name = model_name
        self.high_threshold = high_threshold
        self.low_threshold = low_threshold
        self.embed_dim = embed_dim

    # ------------------------------------------------------------------
    # Lazy model loading
    # ------------------------------------------------------------------

    @property
    def model(self):
        """Return the sentence-transformers model, loading it on first use."""
        if self._model is None:
            # pylint: disable=import-outside-toplevel
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self._model_name)
        return self._model

    def _embed(self, name: str, context: str = "") -> list[float]:
        """Embed the candidate as name + context and return a float32 list."""
        text = f"{name}\n\n{context}" if context else name
        vec = self.model.encode([text], convert_to_numpy=True)[0]
        return vec.astype("float32").tolist()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve(
        self,
        candidate_name: str,
        candidate_context: str = "",
        concept_type: Optional[str] = None,
        *,
        source_type: Optional[str] = None,
        source_id: Optional[int] = None,
    ) -> ResolveResult:
        """Resolve one candidate to a concept_id.

        `concept_type` scopes exact-match and embedding-match to concepts of
        the same type (polysemy handling per §5.2). Aliases are type-agnostic
        by design — an alias like "CDC" can map to one canonical concept
        regardless of type.

        `source_type` and `source_id` carry provenance for queued borderline
        cases; they're stored on concept_resolution_queue so a reviewer can
        see where the candidate came from.
        """
        if not candidate_name or not candidate_name.strip():
            raise ValueError("candidate_name must be non-empty")
        candidate_name = candidate_name.strip()

        # Stage 1: exact match
        exact = self._exact_match(candidate_name, concept_type)
        if exact is not None:
            return ResolveResult(
                concept_id=exact, is_new=False, resolution_type="exact"
            )

        # Stage 2: alias match
        alias = self._alias_match(candidate_name)
        if alias is not None:
            return ResolveResult(
                concept_id=alias, is_new=False, resolution_type="alias"
            )

        # Stage 3: embedding similarity
        qvec = self._embed(candidate_name, candidate_context)
        nearest = self._nearest_by_embedding(qvec, concept_type)

        if nearest is not None:
            neighbor_id, similarity = nearest
            if similarity >= self.high_threshold:
                return ResolveResult(
                    concept_id=neighbor_id,
                    is_new=False,
                    resolution_type="embedding_high",
                    similarity=similarity,
                    nearest_concept_id=neighbor_id,
                )
            if similarity >= self.low_threshold:
                new_id = self._create_concept(
                    candidate_name,
                    candidate_context,
                    concept_type,
                    qvec,
                    pending_review=True,
                )
                self._enqueue_resolution(
                    candidate_name,
                    candidate_context,
                    nearest_concept_id=neighbor_id,
                    similarity=similarity,
                    source_type=source_type,
                    source_id=source_id,
                )
                return ResolveResult(
                    concept_id=new_id,
                    is_new=True,
                    resolution_type="borderline",
                    similarity=similarity,
                    nearest_concept_id=neighbor_id,
                )

        # Fall through: genuinely new concept.
        new_id = self._create_concept(
            candidate_name, candidate_context, concept_type, qvec, pending_review=False
        )
        return ResolveResult(
            concept_id=new_id,
            is_new=True,
            resolution_type="new",
            similarity=nearest[1] if nearest else None,
            nearest_concept_id=nearest[0] if nearest else None,
        )

    # ------------------------------------------------------------------
    # Stage implementations
    # ------------------------------------------------------------------

    def _exact_match(
        self, candidate_name: str, concept_type: Optional[str]
    ) -> Optional[int]:
        """Case-insensitive lookup on concept.name, optionally scoped by type."""
        if concept_type is None:
            row = self.conn.execute(
                "SELECT concept_id FROM concept "
                "WHERE lower(name) = lower(?) LIMIT 1",
                [candidate_name],
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT concept_id FROM concept "
                "WHERE lower(name) = lower(?) AND concept_type = ? LIMIT 1",
                [candidate_name, concept_type],
            ).fetchone()
        return row[0] if row else None

    def _alias_match(self, candidate_name: str) -> Optional[int]:
        """Case-insensitive lookup on concept_alias.alias."""
        row = self.conn.execute(
            "SELECT concept_id FROM concept_alias "
            "WHERE lower(alias) = lower(?) LIMIT 1",
            [candidate_name],
        ).fetchone()
        return row[0] if row else None

    def _nearest_by_embedding(
        self, qvec: list[float], concept_type: Optional[str]
    ) -> Optional[tuple[int, float]]:
        """Find the nearest existing concept by cosine similarity.

        Returns (concept_id, similarity) or None if there are no embedded
        concepts to compare against. Similarity = 1 - cosine_distance.
        """
        count = self.conn.execute(
            "SELECT COUNT(*) FROM concept_embedding"
        ).fetchone()[0]
        if count == 0:
            return None

        if concept_type is None:
            row = self.conn.execute(
                f"""
                SELECT e.concept_id,
                       array_cosine_distance(e.embedding, ?::FLOAT[{self.embed_dim}]) AS d
                  FROM concept_embedding e
                 ORDER BY d ASC
                 LIMIT 1
                """,
                [qvec],
            ).fetchone()
        else:
            row = self.conn.execute(
                f"""
                SELECT e.concept_id,
                       array_cosine_distance(e.embedding, ?::FLOAT[{self.embed_dim}]) AS d
                  FROM concept_embedding e
                  JOIN concept c USING (concept_id)
                 WHERE c.concept_type = ?
                 ORDER BY d ASC
                 LIMIT 1
                """,
                [qvec, concept_type],
            ).fetchone()

        if row is None:
            return None
        concept_id, distance = row
        similarity = 1.0 - float(distance)
        return concept_id, similarity

    # ------------------------------------------------------------------
    # Writers
    # ------------------------------------------------------------------

    def _create_concept(
        self,
        name: str,
        description: str,
        concept_type: Optional[str],
        embedding: list[float],
        *,
        pending_review: bool,
    ) -> int:
        """Insert a new concept row and its embedding; return the concept_id."""
        row = self.conn.execute(
            """
            INSERT INTO concept (name, concept_type, description, pending_review)
            VALUES (?, ?, ?, ?)
            RETURNING concept_id
            """,
            [name, concept_type, description or None, pending_review],
        ).fetchone()
        concept_id = row[0]
        self.conn.execute(
            "INSERT INTO concept_embedding (concept_id, embedding) VALUES (?, ?)",
            [concept_id, embedding],
        )
        return concept_id

    def _enqueue_resolution(
        self,
        candidate_name: str,
        candidate_context: str,
        *,
        nearest_concept_id: int,
        similarity: float,
        source_type: Optional[str],
        source_id: Optional[int],
    ) -> int:
        """Push a borderline candidate onto concept_resolution_queue."""
        row = self.conn.execute(
            """
            INSERT INTO concept_resolution_queue (
                candidate_name, candidate_context,
                source_type, source_id,
                nearest_concept_id, similarity_score,
                resolution_action
            )
            VALUES (?, ?, ?, ?, ?, ?, 'pending')
            RETURNING queue_id
            """,
            [
                candidate_name,
                candidate_context or None,
                source_type,
                source_id,
                nearest_concept_id,
                similarity,
            ],
        ).fetchone()
        return row[0]

    # ------------------------------------------------------------------
    # Alias registration (used by resolve() and by seed_aliases.py)
    # ------------------------------------------------------------------

    def register_alias(
        self, concept_id: int, alias: str, alias_type: Optional[str] = None
    ) -> Optional[int]:
        """Register an alias for an existing concept.

        Returns the new alias_id, or None if the (concept_id, alias) pair
        already existed (the UNIQUE constraint catches it silently).
        """
        row = self.conn.execute(
            "SELECT alias_id FROM concept_alias "
            "WHERE concept_id = ? AND lower(alias) = lower(?)",
            [concept_id, alias],
        ).fetchone()
        if row is not None:
            return None
        new_id = self.conn.execute(
            """
            INSERT INTO concept_alias (concept_id, alias, alias_type)
            VALUES (?, ?, ?)
            RETURNING alias_id
            """,
            [concept_id, alias, alias_type],
        ).fetchone()[0]
        return new_id
