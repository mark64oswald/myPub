"""Tests for skills_eval.py — Phase 5.5 routing eval.

Covers:
  * Query synthesis (name-only, concept-exclusive, concept-shared)
  * Cosine + metric aggregation (R@1, R@3, MRR)
  * score_routing under perfect / partial / empty inputs
  * run_routing_eval end-to-end with extra_queries
  * report_to_dict serialization
  * End-to-end smoke: materialize_package output → run_routing_eval
    on the same in-memory catalog.

The deterministic embedder is a hash-bucket bag-of-words: each token
maps to a fixed dimension by hashlib, then increments that slot. Cosine
similarity on these vectors equals "fraction of shared tokens, weighted
by frequency" — close enough to a real embedder to exercise the ranking
logic.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import duckdb
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FILE = PROJECT_ROOT / "schemas" / "catalog.sql"
KB_MCP = PROJECT_ROOT / "mcp-servers" / "kb-mcp"
if str(KB_MCP) not in sys.path:
    sys.path.insert(0, str(KB_MCP))

import skills_eval as se  # noqa: E402
import skills_factory as sf  # noqa: E402


# ---------------------------------------------------------------------------
# Deterministic embedder
# ---------------------------------------------------------------------------


def _hash_token(tok: str, dim: int) -> int:
    h = hashlib.sha1(tok.encode("utf-8")).digest()
    return int.from_bytes(h[:4], "big") % dim


def make_fake_embed(dim: int = 128):
    def embed(texts):
        out = []
        for s in texts:
            v = [0.0] * dim
            for tok in s.lower().split():
                v[_hash_token(tok, dim)] += 1.0
            out.append(v)
        return out
    return embed


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def catalog(tmp_path):
    conn = duckdb.connect(str(tmp_path / "catalog.ddb"))
    conn.execute(SCHEMA_FILE.read_text())
    yield conn
    conn.close()


def _seed_concept(conn, name):
    return conn.execute(
        "INSERT INTO concept (name, concept_type) VALUES (?, 'concept') "
        "RETURNING concept_id",
        [name],
    ).fetchone()[0]


def _seed_book_chapter(conn, book_title, chapter_title, source_path=None):
    bid = conn.execute(
        "INSERT INTO book (title, source_path) VALUES (?, ?) RETURNING book_id",
        [book_title, source_path or f"/tmp/{book_title}.epub"],
    ).fetchone()[0]
    cid = conn.execute(
        "INSERT INTO chapter (book_id, title, chapter_num) "
        "VALUES (?, ?, 1) RETURNING chapter_id",
        [bid, chapter_title],
    ).fetchone()[0]
    return cid


def _link_concept_to_chapter(conn, concept_id, chapter_id, other_concept_id=None):
    """Concepts attach to chapters through concept_relation rows. We
    create a self-referential relation if no second concept is given."""
    other = other_concept_id if other_concept_id is not None else concept_id
    conn.execute(
        """
        INSERT INTO concept_relation
            (from_concept_id, to_concept_id, relation_type,
             source_type, source_id, confidence)
        VALUES (?, ?, 'CITES', 'chapter', ?, 0.9)
        """,
        [concept_id, other, chapter_id],
    )


@pytest.fixture
def two_skill_package(catalog):
    """Package with 2 skills, each backed by chapters carrying distinct
    concept relations.

    Skill A — "Circuit Breaker"
        * exclusive concepts: Circuit Breaker, Half Open
        * description that mentions its concepts strongly
    Skill B — "Bulkhead Isolation"
        * exclusive concepts: Bulkhead Isolation, Resource Pool
        * description that mentions its concepts strongly
    Shared: Resilience  (both skills' chapters relate to it)
    """
    pid = catalog.execute(
        "INSERT INTO skill_package (name, domain) VALUES "
        "('resilience-pkg', 'resilience') RETURNING package_id"
    ).fetchone()[0]

    cb = _seed_concept(catalog, "Circuit Breaker")
    ho = _seed_concept(catalog, "Half Open")
    bh = _seed_concept(catalog, "Bulkhead Isolation")
    rp = _seed_concept(catalog, "Resource Pool")
    res = _seed_concept(catalog, "Resilience")

    cb_chap = _seed_book_chapter(
        catalog, "Release It", "Circuit Breaker chapter", "/tmp/release-it-cb.epub")
    bh_chap = _seed_book_chapter(
        catalog, "Release It", "Bulkhead chapter", "/tmp/release-it-bh.epub")

    _link_concept_to_chapter(catalog, cb, cb_chap, ho)
    _link_concept_to_chapter(catalog, ho, cb_chap)
    _link_concept_to_chapter(catalog, res, cb_chap)  # shared
    _link_concept_to_chapter(catalog, bh, bh_chap, rp)
    _link_concept_to_chapter(catalog, rp, bh_chap)
    _link_concept_to_chapter(catalog, res, bh_chap)  # shared

    skill_cb = catalog.execute(
        """
        INSERT INTO skill (package_id, name, description, content_markdown,
                            strategy, source_currency)
        VALUES (?, 'Circuit Breaker',
                'When user asks about circuit breaker patterns, half open, or trip thresholds',
                '# Circuit Breaker\n\n...',
                'recent_doc_anchored', 'current')
        RETURNING skill_id
        """, [pid],
    ).fetchone()[0]
    skill_bh = catalog.execute(
        """
        INSERT INTO skill (package_id, name, description, content_markdown,
                            strategy, source_currency)
        VALUES (?, 'Bulkhead Isolation',
                'When user asks about bulkhead isolation, resource pool sizing, or thread isolation',
                '# Bulkhead\n\n...',
                'recent_doc_anchored', 'current')
        RETURNING skill_id
        """, [pid],
    ).fetchone()[0]

    catalog.execute(
        "INSERT INTO skill_source (skill_id, source_type, source_id, score, weight) "
        "VALUES (?, 'chapter', ?, 0.9, 1.0)",
        [skill_cb, cb_chap],
    )
    catalog.execute(
        "INSERT INTO skill_source (skill_id, source_type, source_id, score, weight) "
        "VALUES (?, 'chapter', ?, 0.9, 1.0)",
        [skill_bh, bh_chap],
    )
    return {
        "package_id": pid,
        "skill_cb": skill_cb,
        "skill_bh": skill_bh,
        "concepts": {
            "cb": cb, "ho": ho, "bh": bh, "rp": rp, "res": res,
        },
        "chapters": {"cb": cb_chap, "bh": bh_chap},
    }


# ---------------------------------------------------------------------------
# Cosine + aggregate
# ---------------------------------------------------------------------------


def test_cosine_orthogonal_is_zero():
    assert se._cosine([1.0, 0.0], [0.0, 1.0]) == 0.0


def test_cosine_identical_is_one():
    assert se._cosine([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)


def test_cosine_zero_vector_returns_zero():
    assert se._cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_aggregate_metrics_empty_returns_zero():
    m = se._aggregate_metrics([])
    assert m.n_queries == 0 and m.recall_at_1 == 0.0 and m.mrr == 0.0


def test_aggregate_metrics_perfect_returns_ones():
    qrs = [
        se.QueryResult(
            query=se.EvalQuery(skill_id=1, query="x", source="name"),
            rank=1, top_skill_id=1, top_similarity=1.0, correct_similarity=1.0,
        ),
        se.QueryResult(
            query=se.EvalQuery(skill_id=2, query="y", source="name"),
            rank=1, top_skill_id=2, top_similarity=1.0, correct_similarity=1.0,
        ),
    ]
    m = se._aggregate_metrics(qrs)
    assert m.recall_at_1 == 1.0
    assert m.recall_at_3 == 1.0
    assert m.mrr == 1.0


def test_aggregate_metrics_mixed_ranks():
    qrs = [
        se.QueryResult(
            query=se.EvalQuery(skill_id=1, query="a", source="name"),
            rank=1, top_skill_id=1, top_similarity=0.9, correct_similarity=0.9,
        ),
        se.QueryResult(
            query=se.EvalQuery(skill_id=2, query="b", source="name"),
            rank=2, top_skill_id=99, top_similarity=0.95, correct_similarity=0.6,
        ),
        se.QueryResult(
            query=se.EvalQuery(skill_id=3, query="c", source="name"),
            rank=4, top_skill_id=99, top_similarity=0.95, correct_similarity=0.3,
        ),
    ]
    m = se._aggregate_metrics(qrs)
    # 1 of 3 ranked first
    assert m.recall_at_1 == pytest.approx(1 / 3)
    # 2 of 3 in top-3
    assert m.recall_at_3 == pytest.approx(2 / 3)
    # MRR = (1/1 + 1/2 + 1/4) / 3
    assert m.mrr == pytest.approx((1.0 + 0.5 + 0.25) / 3)


# ---------------------------------------------------------------------------
# Query synthesis
# ---------------------------------------------------------------------------


def test_synthesize_queries_emits_name_query_per_skill(catalog, two_skill_package):
    queries, _ = se.synthesize_queries(
        catalog, two_skill_package["package_id"], queries_per_skill=1)
    assert len(queries) == 2
    names = {q.query for q in queries}
    assert names == {"Circuit Breaker", "Bulkhead Isolation"}
    assert all(q.source == "name" for q in queries)


def test_synthesize_queries_prefers_exclusive_concepts(catalog, two_skill_package):
    # 2 = 1 name query + 1 concept query. cb's exclusive set is {CB, HO};
    # CB matches the skill name and is skipped, leaving HO. bh's exclusive
    # set is {BH, RP}; BH is skipped likewise, leaving RP. Neither budget
    # should reach the shared concept (Resilience).
    queries, _ = se.synthesize_queries(
        catalog, two_skill_package["package_id"], queries_per_skill=2)
    cb_id = two_skill_package["skill_cb"]
    bh_id = two_skill_package["skill_bh"]

    cb_queries = [q for q in queries if q.skill_id == cb_id]
    bh_queries = [q for q in queries if q.skill_id == bh_id]

    cb_concept_queries = [q for q in cb_queries if q.source != "name"]
    bh_concept_queries = [q for q in bh_queries if q.source != "name"]

    # All concept queries should be exclusive (Resilience is shared, so
    # it should not show up at budget=2 when 2 exclusive concepts exist).
    assert all(q.source == "concept_exclusive" for q in cb_concept_queries)
    assert all(q.source == "concept_exclusive" for q in bh_concept_queries)
    cb_terms = {q.query for q in cb_concept_queries}
    bh_terms = {q.query for q in bh_concept_queries}
    assert "Resilience" not in cb_terms and "Resilience" not in bh_terms


def test_synthesize_queries_skips_name_duplicates(catalog, two_skill_package):
    """If a concept happens to share the skill's name, don't emit it twice
    (the name query already covers it)."""
    queries, _ = se.synthesize_queries(
        catalog, two_skill_package["package_id"], queries_per_skill=5)
    # "Circuit Breaker" appears as both skill name and a concept name; it
    # should appear exactly once across all queries for that skill.
    cb_id = two_skill_package["skill_cb"]
    cb_queries = [q for q in queries if q.skill_id == cb_id]
    cb_breaker_count = sum(1 for q in cb_queries if q.query == "Circuit Breaker")
    assert cb_breaker_count == 1


def test_synthesize_queries_emits_note_when_concepts_run_short(catalog):
    """A skill with no concept-bearing chapters yields only the name
    query and produces a note."""
    pid = catalog.execute(
        "INSERT INTO skill_package (name) VALUES ('lonely') RETURNING package_id"
    ).fetchone()[0]
    catalog.execute(
        "INSERT INTO skill (package_id, name, description) "
        "VALUES (?, 'Solo', 'Triggers on solo')",
        [pid],
    )
    queries, notes = se.synthesize_queries(catalog, pid, queries_per_skill=4)
    assert len(queries) == 1  # just the name query
    assert any("yielded 0/3" in n for n in notes)


def test_synthesize_queries_empty_package_returns_note(catalog):
    pid = catalog.execute(
        "INSERT INTO skill_package (name) VALUES ('empty') RETURNING package_id"
    ).fetchone()[0]
    queries, notes = se.synthesize_queries(catalog, pid)
    assert queries == []
    assert any("no skills" in n for n in notes)


# ---------------------------------------------------------------------------
# score_routing
# ---------------------------------------------------------------------------


def test_score_routing_perfect_match(catalog, two_skill_package):
    """When each query is the skill's own name and descriptions mention
    that name, the correct skill should rank #1 every time."""
    queries = [
        se.EvalQuery(
            skill_id=two_skill_package["skill_cb"],
            query="circuit breaker patterns",
            source="user",
        ),
        se.EvalQuery(
            skill_id=two_skill_package["skill_bh"],
            query="bulkhead isolation thread",
            source="user",
        ),
    ]
    embed = make_fake_embed()
    report = se.score_routing(
        catalog, two_skill_package["package_id"], queries, embed_fn=embed)
    assert report.overall.recall_at_1 == 1.0
    assert report.overall.mrr == 1.0
    assert report.n_skills == 2
    assert len(report.per_query) == 2


def test_score_routing_handles_missing_package(catalog):
    embed = make_fake_embed()
    report = se.score_routing(catalog, 99999, [], embed_fn=embed)
    assert report.n_skills == 0
    assert any("not found" in n for n in report.notes)


def test_score_routing_handles_no_queries(catalog, two_skill_package):
    embed = make_fake_embed()
    report = se.score_routing(
        catalog, two_skill_package["package_id"], [], embed_fn=embed)
    assert report.overall.n_queries == 0
    assert any("no queries" in n for n in report.notes)


def test_score_routing_falls_back_when_description_empty(catalog):
    """An empty description should fall back to the skill name and emit
    a note. The eval still runs cleanly."""
    pid = catalog.execute(
        "INSERT INTO skill_package (name) VALUES ('p') RETURNING package_id"
    ).fetchone()[0]
    sid_a = catalog.execute(
        "INSERT INTO skill (package_id, name, description) "
        "VALUES (?, 'Foo', '') RETURNING skill_id",
        [pid],
    ).fetchone()[0]
    sid_b = catalog.execute(
        "INSERT INTO skill (package_id, name, description) "
        "VALUES (?, 'Bar', 'When user asks about bar things') RETURNING skill_id",
        [pid],
    ).fetchone()[0]
    queries = [
        se.EvalQuery(skill_id=sid_a, query="foo lookup", source="user"),
        se.EvalQuery(skill_id=sid_b, query="bar things lookup", source="user"),
    ]
    embed = make_fake_embed()
    report = se.score_routing(catalog, pid, queries, embed_fn=embed)
    assert any("descriptions empty" in n for n in report.notes)
    assert report.overall.n_queries == 2


def test_score_routing_per_skill_breakdown(catalog, two_skill_package):
    queries = [
        se.EvalQuery(
            skill_id=two_skill_package["skill_cb"], query="circuit breaker", source="user"),
        se.EvalQuery(
            skill_id=two_skill_package["skill_cb"], query="half open", source="user"),
        se.EvalQuery(
            skill_id=two_skill_package["skill_bh"], query="bulkhead resource", source="user"),
    ]
    report = se.score_routing(
        catalog, two_skill_package["package_id"], queries,
        embed_fn=make_fake_embed())
    assert two_skill_package["skill_cb"] in report.per_skill
    assert two_skill_package["skill_bh"] in report.per_skill
    assert report.per_skill[two_skill_package["skill_cb"]].n_queries == 2
    assert report.per_skill[two_skill_package["skill_bh"]].n_queries == 1


# ---------------------------------------------------------------------------
# run_routing_eval
# ---------------------------------------------------------------------------


def test_run_routing_eval_full_loop(catalog, two_skill_package):
    """End-to-end: synthesize + score + return report."""
    extra = [
        se.EvalQuery(
            skill_id=two_skill_package["skill_cb"],
            query="circuit breaker trip threshold",
            source="user",
        ),
    ]
    report = se.run_routing_eval(
        catalog, two_skill_package["package_id"],
        queries_per_skill=3,
        embed_fn=make_fake_embed(),
        extra_queries=extra,
    )
    # 2 skills * 3 synth + 1 extra = 7 queries
    assert report.overall.n_queries == 7
    # Descriptions are deliberately tuned to match concepts; on a
    # 2-skill package with reasonable token overlap, R@1 should be
    # solidly above chance (which would be 0.5).
    assert report.overall.recall_at_1 >= 0.7


def test_run_routing_eval_appends_synth_notes_first(catalog):
    pid = catalog.execute(
        "INSERT INTO skill_package (name) VALUES ('p') RETURNING package_id"
    ).fetchone()[0]
    catalog.execute(
        "INSERT INTO skill (package_id, name, description) "
        "VALUES (?, 'Lonely', '')",
        [pid],
    )
    report = se.run_routing_eval(
        catalog, pid, queries_per_skill=3, embed_fn=make_fake_embed())
    # Expect both a synth note (concept budget short) and a score note
    # (description empty).
    assert any("yielded" in n for n in report.notes)
    assert any("descriptions empty" in n for n in report.notes)


# ---------------------------------------------------------------------------
# report_to_dict
# ---------------------------------------------------------------------------


def test_report_to_dict_serialization(catalog, two_skill_package):
    report = se.run_routing_eval(
        catalog, two_skill_package["package_id"],
        queries_per_skill=2, embed_fn=make_fake_embed())
    d = se.report_to_dict(report)
    assert d["package_id"] == two_skill_package["package_id"]
    assert d["package_name"] == "resilience-pkg"
    assert "overall" in d and "recall_at_1" in d["overall"]
    assert isinstance(d["per_skill"], dict)
    # JSON requires string keys; we coerce skill_id to str on serialization.
    assert all(isinstance(k, str) for k in d["per_skill"])
    assert isinstance(d["per_query"], list)
    assert all("rank" in row for row in d["per_query"])


# ---------------------------------------------------------------------------
# End-to-end smoke (B): materialize → eval
# ---------------------------------------------------------------------------


def test_e2e_materialize_then_eval(catalog, two_skill_package, tmp_path):
    """B-flavored smoke: drive materialize_package on the seeded package,
    then run the eval on the same catalog. Asserts:
      * SKILL.md files written (sanity-check the materialization step)
      * eval runs without errors
      * eval returns at least one query per skill
      * R@1 exceeds chance (>0.5 for a 2-skill package)
    """
    materialize_report = sf.materialize_package(
        catalog,
        package_id=two_skill_package["package_id"],
        output_root=str(tmp_path),
        overwrite=True,
    )
    assert len(materialize_report.skill_md_paths) == 2
    for p in materialize_report.skill_md_paths:
        text = Path(p).read_text()
        assert text.startswith("---")
        assert "name:" in text and "description:" in text

    eval_report = se.run_routing_eval(
        catalog, two_skill_package["package_id"],
        queries_per_skill=3, embed_fn=make_fake_embed(),
    )
    assert eval_report.n_skills == 2
    assert eval_report.overall.n_queries >= 4
    # 2-skill package, descriptions deliberately mention exclusive concepts,
    # so we expect R@1 well above 0.5.
    assert eval_report.overall.recall_at_1 > 0.5
    # Every skill should have its own queries scored.
    for sid in (two_skill_package["skill_cb"], two_skill_package["skill_bh"]):
        assert sid in eval_report.per_skill
        assert eval_report.per_skill[sid].n_queries > 0
