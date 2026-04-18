"""
Phase 2 end-to-end integration test.

Exercises the full Phase 2 pipeline in miniature against the realistic
substrate fixture:

  1. Index a tiny programmatically-built ePub into the catalog.
  2. Feed synthetic LLM JSON through extract_entities.process_extraction_json
     so concepts, embeddings, and concept_relation rows land on disk.
  3. Verify the resolver's borderline path lands a second-chapter
     extraction in concept_resolution_queue with a provisional concept.
  4. Resolve the queue item via resolve_concept.do_merge and confirm
     edges migrate cleanly, the provisional is deleted, and the alias
     registers.

This is the end-to-end workflow every /kb-* command downstream depends
on. If it breaks, something in Phase 2's substrate moved.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import duckdb
import pytest
from ebooklib import epub

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FILE = PROJECT_ROOT / "schemas" / "catalog.sql"
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
MCP_DIR = PROJECT_ROOT / "mcp-servers" / "kb-mcp"
for _p in (SCRIPTS_DIR, MCP_DIR):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

import extract_entities as ee  # noqa: E402  # pylint: disable=wrong-import-position
import index_books as ib  # noqa: E402  # pylint: disable=wrong-import-position
import resolve_concept as rc  # noqa: E402  # pylint: disable=wrong-import-position
from resolution import EntityResolver  # noqa: E402  # pylint: disable=wrong-import-position


# ----------------------------------------------------------------------------
# Fixture: a catalog that looks like production mid-Phase-2 — v2 schema,
# VSS loaded, HNSW indexes on both embedding side tables. Built locally
# here (instead of using conftest.realistic_conn) so we own the seeding
# from a clean slate.
# ----------------------------------------------------------------------------

@pytest.fixture
def integration_conn():
    conn = duckdb.connect(":memory:")
    conn.execute(SCHEMA_FILE.read_text())
    conn.execute("LOAD vss")
    conn.execute("SET hnsw_enable_experimental_persistence = true")
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def embedder():
    from sentence_transformers import SentenceTransformer  # pylint: disable=import-outside-toplevel
    return SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")


def _make_test_epub(dest: Path, *, title: str) -> Path:
    """Minimal ePub with two distinct content chapters."""
    book = epub.EpubBook()
    book.set_identifier("int-test")
    book.set_title(title)
    book.set_language("en")
    book.add_author("Integration Author")
    c1 = epub.EpubHtml(title="Fundamentals", file_name="ch1.xhtml", lang="en")
    c1.content = (
        "<h1>Fundamentals</h1>"
        "<p>This chapter covers the fundamentals of dimensional modeling. "
        + ("Fact and dimension tables are central. " * 20) + "</p>"
    )
    c2 = epub.EpubHtml(title="Advanced Topics", file_name="ch2.xhtml", lang="en")
    c2.content = (
        "<h1>Advanced Topics</h1>"
        "<p>Building on the fundamentals, this chapter dives deeper. "
        + ("Snowflake schemas and bridge tables extend the model. " * 20) + "</p>"
    )
    for c in (c1, c2):
        book.add_item(c)
    book.toc = [c1, c2]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", c1, c2]
    epub.write_epub(str(dest), book, {})
    return dest


# ----------------------------------------------------------------------------
# End-to-end test
# ----------------------------------------------------------------------------

def test_phase2_index_extract_resolve_roundtrip(integration_conn, embedder, tmp_path):
    """index → extract (ch1) → extract (ch2, triggers borderline queue) →
    resolve-merge. Every step asserted."""
    # STEP 1 — Index the tiny ePub.
    src = tmp_path / "Integration-1234567890.epub"
    _make_test_epub(src, title="Integration Book")
    assert ib.index_book(integration_conn, src)

    # Chapters loaded with content + content_hash; book has file hash.
    book_row = integration_conn.execute(
        "SELECT book_id, title, content_hash, status "
        "FROM book WHERE source_path = ?", [str(src)]
    ).fetchone()
    assert book_row[1] == "Integration Book"
    assert book_row[2] and len(book_row[2]) == 64
    assert book_row[3] == "active"
    book_id = book_row[0]

    chapters = integration_conn.execute(
        "SELECT chapter_id, title, content_hash FROM chapter "
        "WHERE book_id = ? AND content IS NOT NULL ORDER BY chapter_id",
        [book_id],
    ).fetchall()
    assert len(chapters) >= 2
    ch_fund = next(c for c in chapters if c[1] == "Fundamentals")
    ch_adv = next(c for c in chapters if c[1] == "Advanced Topics")

    # STEP 2 — Extract ch1 via synthetic LLM output.
    resolver = EntityResolver(
        integration_conn, model=embedder,
        # Tighter thresholds so the borderline probe in step 3 lands in the band.
        high_threshold=0.98, low_threshold=0.50,
    )
    raw_ch1 = {
        "entities": [
            {"name": "Star Schema", "type": "Concept",
             "description": "central fact table joined to denormalized dimension tables"},
            {"name": "Fact Table", "type": "Concept",
             "description": "table storing measurements of a business process"},
        ],
        "relations": [
            {"from": "Star Schema", "to": "Fact Table", "type": "REQUIRES",
             "confidence": 0.9},
        ],
    }
    summary1 = ee.process_extraction_json(integration_conn, resolver, ch_fund[0], raw_ch1)
    assert summary1.entities_extracted == 2
    assert summary1.relations_written == 1
    # Both concepts created on this run.
    assert summary1.entities_by_resolution.get("new") == 2

    star_id = integration_conn.execute(
        "SELECT concept_id FROM concept WHERE name = 'Star Schema'"
    ).fetchone()[0]

    # STEP 3 — Extract ch2 with a second concept closely related to 'Star Schema'.
    # With low_threshold=0.50 the resolver should land 'Snowflake Schema' in
    # the borderline band and queue it with a provisional concept + queue row.
    raw_ch2 = {
        "entities": [
            {"name": "Snowflake Schema", "type": "Concept",
             "description": "dimensional model with normalized dimensions — related "
                            "to but distinct from star schema"},
        ],
        "relations": [],
    }
    ee.process_extraction_json(integration_conn, resolver, ch_adv[0], raw_ch2)

    queue = integration_conn.execute(
        "SELECT queue_id, candidate_name, provisional_concept_id, "
        "       nearest_concept_id, resolution_action "
        "FROM concept_resolution_queue"
    ).fetchall()
    assert len(queue) == 1
    qid, cand_name, prov_id, nearest_id, action = queue[0]
    assert cand_name == "Snowflake Schema"
    assert nearest_id == star_id
    assert action == "pending"
    # The provisional concept exists with pending_review=TRUE.
    prov_flag = integration_conn.execute(
        "SELECT pending_review FROM concept WHERE concept_id = ?", [prov_id]
    ).fetchone()[0]
    assert prov_flag is True

    # STEP 4 — Build the HNSW index that production has, so the resolve path
    # hits the same "DML on HNSW-indexed table" behavior as live.
    integration_conn.execute(
        "CREATE INDEX concept_embedding_hnsw ON concept_embedding "
        "USING HNSW (embedding) WITH (metric = 'cosine')"
    )
    # do_merge with --register-alias should delete the provisional, rewrite
    # nothing (no edges on provisional in this scenario), register alias.
    rc.do_merge(integration_conn, qid, register_alias=True)
    # Provisional gone, alias on nearest.
    assert integration_conn.execute(
        "SELECT COUNT(*) FROM concept WHERE concept_id = ?", [prov_id]
    ).fetchone()[0] == 0
    assert integration_conn.execute(
        "SELECT COUNT(*) FROM concept_embedding WHERE concept_id = ?", [prov_id]
    ).fetchone()[0] == 0
    alias_row = integration_conn.execute(
        "SELECT alias, alias_type FROM concept_alias WHERE concept_id = ?",
        [star_id],
    ).fetchone()
    assert alias_row == ("Snowflake Schema", "synonym")
    # Queue is stamped.
    assert integration_conn.execute(
        "SELECT resolution_action FROM concept_resolution_queue WHERE queue_id = ?",
        [qid],
    ).fetchone()[0] == "alias"

    # Sanity: the retained concept graph is coherent.
    # star_id still has its 'Fact Table' REQUIRES edge from chapter 1.
    relcount = integration_conn.execute(
        "SELECT COUNT(*) FROM concept_relation "
        "WHERE from_concept_id = ? AND relation_type = 'REQUIRES'",
        [star_id],
    ).fetchone()[0]
    assert relcount == 1
