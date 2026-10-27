"""Library Landscape: a large doc corpus must not win every job by size."""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp-servers" / "kb-mcp"))

import library_landscape as ll  # noqa: E402


def _cand(name: str, sid: int) -> ll._Candidate:
    return ll._Candidate(name=name, doc_source_id=sid, concept_id=None, description=None,
                         n_doc_sections=0, n_chapters=0, overlap_score=0,
                         neighborhood_size=0, has_current_docs=True)


def test_focused_candidate_beats_huge_corpus_on_its_own_job(monkeypatch):
    conn = duckdb.connect()
    conn.execute("CREATE TABLE concept (concept_id BIGINT, name VARCHAR)")
    conn.execute("INSERT INTO concept SELECT i, 'c' || i FROM range(0, 1000) t(i)")
    # 'big' touches 600 concepts, including 4 of the 5 PDF-job concepts;
    # 'pdf' is a 20-concept library that covers 3 of them. Raw rarity
    # scoring ranks 'big' first (4.2 vs 2.7).
    neighborhoods = {1: set(range(600)), 2: {0, 1, 2} | set(range(901, 918))}
    dec = ll.LandscapeDecomposer()
    monkeypatch.setattr(dec, "_doc_source_concepts", lambda _c, sid: set(neighborhoods[sid]))
    job = ll._Job(name="extract pdf text", concept_cluster={0, 1, 2, 3, 900})
    matrix = dec._score_matrix(conn, [_cand("big", 1), _cand("pdf", 2)], [job])
    by = {m.candidate_name: m for m in matrix}
    assert by["big"].coverage == 4 and by["pdf"].coverage == 3
    assert by["pdf"].score > by["big"].score
