#!/usr/bin/env python3
"""
generate_embeddings.py — Populate FLOAT[384] embeddings on chapter/concept.

Uses sentence-transformers/all-MiniLM-L6-v2 (384-dim, max 256 tokens/input).
Each chapter's embedding is computed from `title\n\ncontent` so the opening
tokens carry the strongest topical signal (the title), which matters because
the model truncates to 256 tokens — the tail of a long chapter never
influences the vector.

Only rows where `embedding IS NULL` are processed, so the script is safe to
re-run after interruption.

Usage:
    .venv/bin/python3 scripts/generate_embeddings.py                # full corpus
    .venv/bin/python3 scripts/generate_embeddings.py --limit 10     # timing test
    .venv/bin/python3 scripts/generate_embeddings.py --batch 64
    .venv/bin/python3 scripts/generate_embeddings.py --device cpu
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Iterator

import duckdb

# Load db.open_catalog so we get VSS/FTS/DuckPGQ on the connection.
# Required because chapter_embedding / concept_embedding / etc. carry
# HNSW indexes — DuckDB refuses INSERT/UPDATE/DELETE on HNSW-indexed
# tables when the VSS extension isn't loaded:
#   _duckdb.Error: Cannot bind index 'chapter_embedding',
#   unknown index type 'HNSW'.
# This script writes embeddings, so it must use open_catalog.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = PROJECT_ROOT / "mcp-servers" / "kb-mcp"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))
from db import open_catalog  # noqa: E402  # pylint: disable=wrong-import-position

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = PROJECT_ROOT / "data" / "catalog.ddb"
MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
EMBED_DIM = 384
LOG = logging.getLogger("generate_embeddings")


# ----------------------------------------------------------------------------
# Device detection
# ----------------------------------------------------------------------------

def _pick_device(requested: str | None) -> str:
    """Choose a torch device string. MPS on Apple Silicon, CUDA if available, else CPU."""
    if requested:
        return requested
    try:
        import torch  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
    except ImportError:
        return "cpu"
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


# ----------------------------------------------------------------------------
# Corpus iteration
# ----------------------------------------------------------------------------

def _chapter_batches(
    conn: duckdb.DuckDBPyConnection,
    batch_size: int,
    limit: int | None,
) -> Iterator[list[tuple[int, str]]]:
    """Yield batches of (chapter_id, text) for rows with content and no embedding.

    Pulls from chapter LEFT JOIN chapter_embedding so the script is resumable
    after interruption and skips already-embedded rows.
    """
    sql = (
        "SELECT c.chapter_id, c.title, c.content "
        "  FROM chapter c "
        "  LEFT JOIN chapter_embedding e USING (chapter_id) "
        " WHERE e.chapter_id IS NULL AND c.content IS NOT NULL "
        " ORDER BY c.chapter_id"
    )
    if limit is not None:
        sql += f" LIMIT {int(limit)}"

    batch: list[tuple[int, str]] = []
    for chapter_id, title, content in conn.execute(sql).fetchall():
        # Prepend the title so the first ~256 tokens include it.
        text = f"{title}\n\n{content}" if title else content
        batch.append((chapter_id, text))
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def _concept_batches(
    conn: duckdb.DuckDBPyConnection,
    batch_size: int,
) -> Iterator[list[tuple[int, str]]]:
    """Yield batches of (concept_id, text) for concepts with no embedding."""
    rows = conn.execute(
        "SELECT c.concept_id, c.name, c.description "
        "  FROM concept c "
        "  LEFT JOIN concept_embedding e USING (concept_id) "
        " WHERE e.concept_id IS NULL "
        " ORDER BY c.concept_id"
    ).fetchall()
    batch: list[tuple[int, str]] = []
    for concept_id, name, description in rows:
        text = f"{name}\n\n{description}" if description else name
        batch.append((concept_id, text))
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


# ----------------------------------------------------------------------------
# Embedding driver
# ----------------------------------------------------------------------------

def _embed_and_write(
    conn: duckdb.DuckDBPyConnection,
    model,
    side_table: str,
    key_col: str,
    batches: Iterator[list[tuple[int, str]]],
    total_hint: int,
    log_every: int,
) -> int:
    """Embed each batch and INSERT the vectors into the side embedding table.

    Returns the number of rows inserted.
    """
    insert_sql = f"INSERT INTO {side_table} ({key_col}, embedding) VALUES (?, ?)"
    processed = 0
    start = time.time()

    for batch in batches:
        ids = [row[0] for row in batch]
        texts = [row[1] for row in batch]
        vectors = model.encode(
            texts,
            batch_size=len(texts),
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        conn.executemany(
            insert_sql,
            [(rid, vec.astype("float32").tolist()) for rid, vec in zip(ids, vectors)],
        )
        processed += len(batch)
        if processed % log_every < len(batch):
            elapsed = time.time() - start
            rate = processed / elapsed if elapsed else 0
            remaining = (total_hint - processed) / rate if rate and total_hint else float("inf")
            LOG.info(
                "%s: %d/%d embedded (%.1f rows/s, ~%.0fs remaining)",
                side_table, processed, total_hint, rate, remaining,
            )

    conn.commit()
    return processed


def _count_pending_chapters(conn: duckdb.DuckDBPyConnection) -> int:
    """Chapters with content but no row in chapter_embedding."""
    return conn.execute(
        "SELECT COUNT(*) FROM chapter c "
        "LEFT JOIN chapter_embedding e USING (chapter_id) "
        "WHERE e.chapter_id IS NULL AND c.content IS NOT NULL"
    ).fetchone()[0]


def _count_pending_concepts(conn: duckdb.DuckDBPyConnection) -> int:
    """Concepts with no row in concept_embedding."""
    return conn.execute(
        "SELECT COUNT(*) FROM concept c "
        "LEFT JOIN concept_embedding e USING (concept_id) "
        "WHERE e.concept_id IS NULL"
    ).fetchone()[0]


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main() -> int:
    """Embed chapter and concept rows with NULL embedding columns."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--batch", type=int, default=64)
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap chapters processed (for timing tests).")
    parser.add_argument("--device", type=str, default=None,
                        help="Torch device: cpu | mps | cuda (default: auto).")
    parser.add_argument("--log-every", type=int, default=1000)
    parser.add_argument("--skip-chapters", action="store_true")
    parser.add_argument("--skip-concepts", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    # Import here so --help is fast even without heavyweight deps loaded.
    from sentence_transformers import SentenceTransformer  # pylint: disable=import-outside-toplevel

    device = _pick_device(args.device)
    LOG.info("loading %s on device=%s", MODEL_NAME, device)
    model = SentenceTransformer(MODEL_NAME, device=device)
    LOG.info("model max_seq_length=%d, embedding dim=%d",
             model.max_seq_length, model.get_embedding_dimension())
    assert model.get_embedding_dimension() == EMBED_DIM

    conn = open_catalog(catalog_path=args.catalog, read_only=False)
    try:
        total_start = time.time()

        if not args.skip_chapters:
            pending = _count_pending_chapters(conn)
            LOG.info("chapter rows pending embedding: %d", pending)
            if pending:
                n = _embed_and_write(
                    conn, model, "chapter_embedding", "chapter_id",
                    _chapter_batches(conn, args.batch, args.limit),
                    total_hint=min(pending, args.limit) if args.limit else pending,
                    log_every=args.log_every,
                )
                LOG.info("chapters embedded: %d", n)

        if not args.skip_concepts:
            pending = _count_pending_concepts(conn)
            LOG.info("concept rows pending embedding: %d", pending)
            if pending:
                n = _embed_and_write(
                    conn, model, "concept_embedding", "concept_id",
                    _concept_batches(conn, args.batch),
                    total_hint=pending,
                    log_every=max(10, args.log_every // 10),
                )
                LOG.info("concepts embedded: %d", n)

        # Final accounting
        total_ch = conn.execute("SELECT COUNT(*) FROM chapter").fetchone()[0]
        done_ch = conn.execute("SELECT COUNT(*) FROM chapter_embedding").fetchone()[0]
        total_co = conn.execute("SELECT COUNT(*) FROM concept").fetchone()[0]
        done_co = conn.execute("SELECT COUNT(*) FROM concept_embedding").fetchone()[0]
        LOG.info(
            "done in %.1fs — chapter embeddings: %d/%d, concept embeddings: %d/%d",
            time.time() - total_start, done_ch, total_ch, done_co, total_co,
        )
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
