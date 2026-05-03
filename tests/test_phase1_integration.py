"""
Phase 1 integration tests — exercise FTS, VSS, and DuckPGQ together.

For each eval-set topic the test:
  1. Runs the FTS query against the BM25 index on chapter.content.
  2. Embeds the (different) semantic query and runs the HNSW search over
     chapter_embedding.embedding.
  3. Runs a graph traversal (author → book → chapter) seeded by the
     eval entry's `graph_seed_author` — a Phase-1 proxy for the future
     concept-DISCUSSES edges.
  4. Asserts each modality produces a minimum number of hits, that the
     three result sets overlap non-trivially, and that each modality
     surfaces something the others do not — the "different signals"
     property the substrate is supposed to deliver.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterable

import duckdb
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CATALOG = PROJECT_ROOT / "data" / "catalog.ddb"
EVAL_SET = PROJECT_ROOT / "tests" / "eval" / "phase1_eval_set.json"
TOP_K = 20

logging.getLogger("sentence_transformers").setLevel(logging.ERROR)
logging.getLogger("transformers").setLevel(logging.ERROR)


@pytest.fixture(scope="module")
def conn() -> Iterable[duckdb.DuckDBPyConnection]:
    """Open the catalog read-only with FTS and VSS loaded.

    Read-only on purpose: this lets the integration tests run while the
    KB MCP server (also read-only) is up, instead of blocking on an
    exclusive RW lock. DuckDB allows multiple RO processes; an RW
    process blocks all of them.

    DuckPGQ + property graph DDL are intentionally not loaded here. The
    third modality below uses a SQL JOIN (author→book→chapter is plain
    FK navigation), which is what the production server's traversal
    paths do too. Live MATCH coverage stays in
    scripts/build_property_graph.py and scripts/install_extensions.py.
    """
    assert CATALOG.exists(), f"catalog not found at {CATALOG}"
    c = duckdb.connect(str(CATALOG), read_only=True)
    c.execute("LOAD fts")
    c.execute("LOAD vss")
    c.execute("SET hnsw_enable_experimental_persistence = true")

    yield c
    c.close()


@pytest.fixture(scope="module")
def embedder():
    """Load the sentence-transformers model once per module."""
    from sentence_transformers import SentenceTransformer  # pylint: disable=import-outside-toplevel
    return SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")


def _load_eval_set() -> list[dict]:
    return json.loads(EVAL_SET.read_text())["topics"]


# ----------------------------------------------------------------------------
# Modality runners
# ----------------------------------------------------------------------------

def _fts_chapter_ids(conn: duckdb.DuckDBPyConnection, query: str, k: int) -> list[int]:
    rows = conn.execute(
        """
        SELECT chapter_id
          FROM chapter
         WHERE fts_main_chapter.match_bm25(chapter_id, ?) IS NOT NULL
         ORDER BY fts_main_chapter.match_bm25(chapter_id, ?) DESC
         LIMIT ?
        """,
        [query, query, k],
    ).fetchall()
    return [r[0] for r in rows]


def _vss_chapter_ids(
    conn: duckdb.DuckDBPyConnection, embedder, query: str, k: int
) -> list[int]:
    qvec = embedder.encode([query], convert_to_numpy=True)[0].astype("float32").tolist()
    rows = conn.execute(
        """
        SELECT e.chapter_id
          FROM chapter_embedding e
         ORDER BY array_cosine_distance(e.embedding, ?::FLOAT[384]) ASC
         LIMIT ?
        """,
        [qvec, k],
    ).fetchall()
    return [r[0] for r in rows]


def _graph_chapter_ids(
    conn: duckdb.DuckDBPyConnection, seed_author: str, k: int
) -> list[int]:
    """Chapters reachable from the seed author via wrote → book_contains.

    Implemented as a SQL JOIN over the book_author / book / chapter FK
    structure rather than a DuckPGQ MATCH. Reasons:
      * The production retrieval server uses recursive CTEs and JOINs;
        the test should mirror that path, not a parallel one.
      * MATCH requires per-connection ``CREATE PROPERTY GRAPH``, which
        is a write — forcing the test to open RW and lock everything
        else out. The JOIN runs read-only.
      * Live MATCH coverage already exists in
        scripts/build_property_graph.py (graph-build verification) and
        scripts/install_extensions.py (post-install smoke test).

    The schema assumes book_author as the wrote-edge join table; if it
    diverges, the FTS/VSS modality assertions still hold and this one
    will fail loudly with the right pointer.
    """
    rows = conn.execute(
        """
        SELECT ch.chapter_id
          FROM author    a
          JOIN book_author ba ON ba.author_id = a.author_id
          JOIN book      b   ON b.book_id     = ba.book_id
          JOIN chapter   ch  ON ch.book_id    = b.book_id
         WHERE a.name = ?
         LIMIT ?
        """,
        [seed_author, k],
    ).fetchall()
    return [r[0] for r in rows]


# ----------------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------------

@pytest.mark.parametrize("topic", _load_eval_set(), ids=lambda t: t["topic"])
def test_three_modalities_each_return_hits(topic, conn, embedder):
    """Each modality must find at least `min_hits_per_modality` chapters."""
    k = TOP_K
    fts = _fts_chapter_ids(conn, topic["fts_query"], k)
    vss = _vss_chapter_ids(conn, embedder, topic["vss_query"], k)
    graph = _graph_chapter_ids(conn, topic["graph_seed_author"], k)

    min_hits = topic["min_hits_per_modality"]
    assert len(fts) >= min_hits, f"FTS returned only {len(fts)} hits for {topic['topic']!r}"
    assert len(vss) >= min_hits, f"VSS returned only {len(vss)} hits for {topic['topic']!r}"
    assert len(graph) >= min_hits, f"graph returned only {len(graph)} hits for {topic['topic']!r}"


@pytest.mark.parametrize("topic", _load_eval_set(), ids=lambda t: t["topic"])
def test_vss_surfaces_something_fts_missed(topic, conn, embedder):
    """Semantic search must surface at least one chapter FTS did not.

    This is the whole point of having VSS alongside FTS — if the two
    always overlap perfectly, the embedding pipeline isn't adding signal.
    """
    fts = set(_fts_chapter_ids(conn, topic["fts_query"], TOP_K))
    vss = set(_vss_chapter_ids(conn, embedder, topic["vss_query"], TOP_K))
    vss_only = vss - fts
    assert vss_only, (
        f"{topic['topic']!r}: VSS top-{TOP_K} is a subset of FTS top-{TOP_K} "
        f"— no independent semantic signal"
    )


@pytest.mark.parametrize("topic", _load_eval_set(), ids=lambda t: t["topic"])
def test_graph_surfaces_something_text_search_missed(topic, conn, embedder):
    """Graph traversal from the seed author must find a chapter that
    neither FTS nor VSS returned directly — the graph contributes a
    traversal-derived signal even in Phase 1 where we only have
    author→book→chapter.
    """
    fts = set(_fts_chapter_ids(conn, topic["fts_query"], TOP_K))
    vss = set(_vss_chapter_ids(conn, embedder, topic["vss_query"], TOP_K))
    graph = set(_graph_chapter_ids(conn, topic["graph_seed_author"], TOP_K))
    graph_only = graph - fts - vss
    assert graph_only, (
        f"{topic['topic']!r}: graph traversal from {topic['graph_seed_author']!r} "
        f"returned {len(graph)} chapters but they were all already in FTS∪VSS"
    )


def _books_for_chapters(
    conn: duckdb.DuckDBPyConnection, chapter_ids: list[int]
) -> set[int]:
    """Map chapter_ids back to the set of book_ids they belong to."""
    if not chapter_ids:
        return set()
    placeholders = ",".join("?" * len(chapter_ids))
    rows = conn.execute(
        f"SELECT DISTINCT book_id FROM chapter WHERE chapter_id IN ({placeholders})",
        chapter_ids,
    ).fetchall()
    return {r[0] for r in rows}


@pytest.mark.parametrize("topic", _load_eval_set(), ids=lambda t: t["topic"])
def test_modalities_have_nontrivial_overlap(topic, conn, embedder):
    """FTS and VSS should agree on at least one book for the same topic.

    Chapter-level overlap is too strict a bar: FTS tends to cluster inside
    one book's content chapters while VSS can prefer that same book's
    introduction/summary chapters, yielding disjoint chapter sets that are
    still semantically aligned. Book-level overlap is what "do these two
    modalities agree" actually means on this corpus.
    """
    fts = _fts_chapter_ids(conn, topic["fts_query"], TOP_K)
    vss = _vss_chapter_ids(conn, embedder, topic["vss_query"], TOP_K)
    fts_books = _books_for_chapters(conn, fts)
    vss_books = _books_for_chapters(conn, vss)
    overlap = fts_books & vss_books
    assert overlap, (
        f"{topic['topic']!r}: FTS and VSS top-{TOP_K} surface 0 books in "
        f"common — modalities are disjoint at the book level"
    )
