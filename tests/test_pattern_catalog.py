"""Tests for pattern_catalog.py — Phase 11 Pattern + Anti-Pattern Catalog."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import duckdb
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FILE = PROJECT_ROOT / "schemas" / "catalog.sql"
KB_MCP = PROJECT_ROOT / "mcp-servers" / "kb-mcp"
if str(KB_MCP) not in sys.path:
    sys.path.insert(0, str(KB_MCP))

import pattern_catalog as pc  # noqa: E402


@pytest.fixture
def catalog(tmp_path):
    conn = duckdb.connect(str(tmp_path / "catalog.ddb"))
    conn.execute(SCHEMA_FILE.read_text())
    yield conn
    conn.close()


def _seed_concept(conn, name, concept_type="Pattern", description=""):
    return conn.execute(
        "INSERT INTO concept (name, concept_type, description) "
        "VALUES (?, ?, ?) RETURNING concept_id",
        [name, concept_type, description],
    ).fetchone()[0]


_REL = [0]


def _add_rel(conn, src, tgt, rel_type, source_id=None):
    _REL[0] += 1
    sid = source_id if source_id is not None else _REL[0]
    conn.execute(
        "INSERT INTO concept_relation (from_concept_id, to_concept_id, "
        "relation_type, source_type, source_id, confidence) "
        "VALUES (?, ?, ?, 'chapter', ?, 0.9)",
        [src, tgt, rel_type, sid],
    )


def _seed_book_chapter(conn, book_title, chapter_title):
    bid = conn.execute(
        "INSERT INTO book (title, source_path) VALUES (?, ?) RETURNING book_id",
        [book_title, f"/tmp/{book_title.replace(' ', '_')}.epub"],
    ).fetchone()[0]
    cid = conn.execute(
        "INSERT INTO chapter (book_id, title, chapter_num) "
        "VALUES (?, ?, 1) RETURNING chapter_id", [bid, chapter_title],
    ).fetchone()[0]
    return cid


def _resolver(name_to_id):
    r = MagicMock()
    r.resolve_lookup_only.side_effect = lambda n: name_to_id.get(n.strip().lower())
    return r


@pytest.fixture
def resilience_corpus(catalog):
    """Three resilience patterns, two implementing the same target."""
    resilience = _seed_concept(catalog, "Resilience", concept_type="Concept")
    fault_tolerance = _seed_concept(catalog, "Fault Tolerance", concept_type="Concept")

    cb = _seed_concept(catalog, "Circuit Breaker", concept_type="Pattern",
                        description="Halts requests when a downstream is failing.")
    bh = _seed_concept(catalog, "Bulkhead", concept_type="Pattern",
                        description="Isolates resources to prevent cascading failure.")
    retry = _seed_concept(catalog, "Retry With Backoff", concept_type="Pattern",
                           description="Retries failed operations with exponential delay.")

    # Anti-patterns
    naive_retry = _seed_concept(catalog, "Naive Tight-Loop Retry",
                                 concept_type="Pattern",
                                 description="Retries immediately and overwhelms the downstream.")

    # Wire IMPLEMENTS — cb and bh both implement Fault Tolerance (family)
    _add_rel(catalog, cb, fault_tolerance, "IMPLEMENTS")
    _add_rel(catalog, bh, fault_tolerance, "IMPLEMENTS")
    _add_rel(catalog, retry, resilience, "IMPLEMENTS")

    # Make the patterns reachable from the domain seed
    _add_rel(catalog, resilience, cb, "REQUIRES")
    _add_rel(catalog, resilience, bh, "REQUIRES")
    _add_rel(catalog, resilience, retry, "REQUIRES")

    # CONTRASTS_WITH — naive_retry contrasts with the good retry
    _add_rel(catalog, retry, naive_retry, "CONTRASTS_WITH")

    # Chapter coverage so chapter_count > 0
    ch = _seed_book_chapter(catalog, "Release It", "Stability Patterns")
    for p in (cb, bh, retry):
        _add_rel(catalog, p, p, "CITES", source_id=ch)

    return {
        "resilience": resilience, "fault_tolerance": fault_tolerance,
        "cb": cb, "bh": bh, "retry": retry,
        "naive_retry": naive_retry,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_slugify_handles_punctuation_and_spaces():
    assert pc._slugify("Circuit Breaker") == "circuit-breaker"
    assert pc._slugify("Retry With Backoff!") == "retry-with-backoff"


def test_short_summary_caps_length():
    long_text = "word " * 100
    out = pc._short_summary(long_text, limit=50)
    assert len(out) <= 51
    assert out.endswith("…")


def test_short_summary_handles_empty():
    assert pc._short_summary(None) == "_no description_"
    assert pc._short_summary("") == "_no description_"


# ---------------------------------------------------------------------------
# Decomposer
# ---------------------------------------------------------------------------


def test_decomposer_unknown_domain_returns_empty(catalog, resilience_corpus):
    res = _resolver({})
    d = pc.PatternDiscoveryDecomposer().decompose(catalog, res, "ghost")
    assert d.domain_concept_id == -1
    assert d.patterns == []
    assert any("not found" in n for n in d.notes)


def test_decomposer_finds_patterns_in_domain(catalog, resilience_corpus):
    res = _resolver({"resilience": resilience_corpus["resilience"]})
    d = pc.PatternDiscoveryDecomposer().decompose(catalog, res, "Resilience")
    assert d.domain_concept_id == resilience_corpus["resilience"]
    pat_ids = {p.concept_id for p in d.patterns}
    assert resilience_corpus["cb"] in pat_ids
    assert resilience_corpus["bh"] in pat_ids
    assert resilience_corpus["retry"] in pat_ids


def test_decomposer_filters_to_pattern_typed(catalog, resilience_corpus):
    """Concept-typed seed should not appear in patterns."""
    res = _resolver({"resilience": resilience_corpus["resilience"]})
    d = pc.PatternDiscoveryDecomposer().decompose(catalog, res, "Resilience")
    pat_ids = {p.concept_id for p in d.patterns}
    assert resilience_corpus["resilience"] not in pat_ids
    assert resilience_corpus["fault_tolerance"] not in pat_ids


def test_decomposer_groups_into_families_by_implements(catalog, resilience_corpus):
    """cb and bh both IMPLEMENT fault_tolerance ⇒ same family."""
    res = _resolver({"resilience": resilience_corpus["resilience"]})
    d = pc.PatternDiscoveryDecomposer().decompose(catalog, res, "Resilience")
    # Find the family containing fault_tolerance as canonical target
    ft_family = next(
        (f for f in d.families
         if f.canonical_target_id == resilience_corpus["fault_tolerance"]),
        None,
    )
    assert ft_family is not None
    member_ids = {p.concept_id for p in ft_family.patterns}
    assert resilience_corpus["cb"] in member_ids
    assert resilience_corpus["bh"] in member_ids


def test_decomposer_finds_anti_patterns_via_contrasts(catalog, resilience_corpus):
    res = _resolver({"resilience": resilience_corpus["resilience"]})
    d = pc.PatternDiscoveryDecomposer().decompose(catalog, res, "Resilience")
    anti_ids = {ap.concept_id for ap in d.anti_patterns}
    assert resilience_corpus["naive_retry"] in anti_ids


def test_decomposer_max_patterns_caps_count(catalog):
    seed = _seed_concept(catalog, "Big Domain", concept_type="Concept")
    target = _seed_concept(catalog, "Common Target", concept_type="Concept")
    for i in range(50):
        p = _seed_concept(catalog, f"Pattern {i}", concept_type="Pattern")
        _add_rel(catalog, p, target, "IMPLEMENTS")
        _add_rel(catalog, seed, p, "REQUIRES")
    res = _resolver({"big domain": seed})
    d = pc.PatternDiscoveryDecomposer().decompose(
        catalog, res, "Big Domain", max_patterns=10,
    )
    assert len(d.patterns) <= 10


def test_decomposer_handles_domain_with_no_patterns(catalog):
    seed = _seed_concept(catalog, "Lonely Domain", concept_type="Concept")
    res = _resolver({"lonely domain": seed})
    d = pc.PatternDiscoveryDecomposer().decompose(catalog, res, "Lonely Domain")
    assert d.patterns == []
    assert any("no Pattern-typed" in n for n in d.notes)


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def test_render_catalog_lists_every_family_and_pattern(catalog, resilience_corpus):
    res = _resolver({"resilience": resilience_corpus["resilience"]})
    d = pc.PatternDiscoveryDecomposer().decompose(catalog, res, "Resilience")
    text = pc._render_catalog(d)
    assert text.startswith("# Resilience")
    for p in d.patterns:
        assert p.name in text


def test_render_pattern_includes_when_to_use(catalog, resilience_corpus):
    res = _resolver({"resilience": resilience_corpus["resilience"]})
    d = pc.PatternDiscoveryDecomposer().decompose(catalog, res, "Resilience")
    cb_pattern = next(p for p in d.patterns
                       if p.concept_id == resilience_corpus["cb"])
    text = pc._render_pattern(catalog, cb_pattern)
    assert "## When to use" in text
    assert "Fault Tolerance" in text  # implements target


def test_render_pattern_includes_when_not_to_use(catalog, resilience_corpus):
    res = _resolver({"resilience": resilience_corpus["resilience"]})
    d = pc.PatternDiscoveryDecomposer().decompose(catalog, res, "Resilience")
    retry_pattern = next(p for p in d.patterns
                          if p.concept_id == resilience_corpus["retry"])
    text = pc._render_pattern(catalog, retry_pattern)
    assert "## When NOT to use" in text


def test_render_anti_patterns_lists_contrasted(catalog, resilience_corpus):
    res = _resolver({"resilience": resilience_corpus["resilience"]})
    d = pc.PatternDiscoveryDecomposer().decompose(catalog, res, "Resilience")
    text = pc._render_anti_patterns(d)
    assert "Naive Tight-Loop Retry" in text


def test_render_anti_patterns_handles_empty():
    d = pc._Decomposition(
        domain_concept_id=1, domain_name="x", domain_concept_type=None,
        families=[], patterns=[], anti_patterns=[],
    )
    text = pc._render_anti_patterns(d)
    assert "No CONTRASTS_WITH" in text


# ---------------------------------------------------------------------------
# Planner + Validator
# ---------------------------------------------------------------------------


def test_planner_produces_one_unit_per_pattern(catalog, resilience_corpus):
    res = _resolver({"resilience": resilience_corpus["resilience"]})
    d = pc.PatternDiscoveryDecomposer().decompose(catalog, res, "Resilience")
    plan = pc.PatternCatalogPlanner().plan(catalog, d)
    assert len(plan.units) == len(d.patterns)
    fnames = {f.filename for f in plan.files}
    assert "_catalog.md" in fnames
    assert "_anti_patterns.md" in fnames
    # One file per pattern
    n_pattern_files = sum(1 for f in plan.files
                           if f.filename.startswith("patterns/"))
    assert n_pattern_files == len(d.patterns)


def test_validator_passes_for_valid_plan(catalog, resilience_corpus):
    res = _resolver({"resilience": resilience_corpus["resilience"]})
    d = pc.PatternDiscoveryDecomposer().decompose(catalog, res, "Resilience")
    plan = pc.PatternCatalogPlanner().plan(catalog, d)
    issues = pc.PatternCatalogValidator().validate(catalog, plan)
    errors = [i for i in issues if i.severity == "error"]
    assert errors == []


def test_validator_flags_phantom_concept_id(catalog, resilience_corpus):
    res = _resolver({"resilience": resilience_corpus["resilience"]})
    d = pc.PatternDiscoveryDecomposer().decompose(catalog, res, "Resilience")
    plan = pc.PatternCatalogPlanner().plan(catalog, d)
    plan.units[0].metadata["concept_id"] = 999_999
    issues = pc.PatternCatalogValidator().validate(catalog, plan)
    assert any("999999" in i.message for i in issues if i.severity == "error")


def test_validator_unresolved_domain_errors(catalog):
    from generator import GenPlan
    plan = GenPlan(generator_type="pattern_catalog", package_name="x", domain="x",
                    package_metadata={"domain_concept_id": -1})
    issues = pc.PatternCatalogValidator().validate(catalog, plan)
    assert any(i.severity == "error" and "domain" in i.message for i in issues)


def test_validator_warns_on_empty_catalog(catalog):
    from generator import GenPlan
    plan = GenPlan(
        generator_type="pattern_catalog", package_name="x", domain="x",
        package_metadata={"domain_concept_id": 1, "n_patterns": 0},
    )
    issues = pc.PatternCatalogValidator().validate(catalog, plan)
    assert all(i.severity != "error" for i in issues)
    assert any("no patterns" in i.message for i in issues)


# ---------------------------------------------------------------------------
# End-to-end
# ---------------------------------------------------------------------------


def test_run_deterministic_writes_catalog_and_per_pattern(
    catalog, resilience_corpus, tmp_path,
):
    res = _resolver({"resilience": resilience_corpus["resilience"]})
    g = pc.make_pattern_catalog_generator()
    pid, report, issues = g.run_deterministic(
        catalog, res, "Resilience", output_root=str(tmp_path),
    )
    assert pid > 0
    errors = [i for i in issues if i.severity == "error"]
    assert errors == []
    pkg_dir = tmp_path / "resilience"
    assert (pkg_dir / "_catalog.md").exists()
    assert (pkg_dir / "_anti_patterns.md").exists()
    assert (pkg_dir / "patterns" / "circuit-breaker.md").exists()


def test_run_deterministic_idempotent(catalog, resilience_corpus, tmp_path):
    res = _resolver({"resilience": resilience_corpus["resilience"]})
    g = pc.make_pattern_catalog_generator()
    pid1, _, _ = g.run_deterministic(catalog, res, "Resilience",
                                       output_root=str(tmp_path))
    pid2, _, _ = g.run_deterministic(catalog, res, "Resilience",
                                       output_root=str(tmp_path))
    assert pid1 == pid2


def test_run_deterministic_unknown_domain_returns_negative(
    catalog, resilience_corpus, tmp_path,
):
    res = _resolver({})
    g = pc.make_pattern_catalog_generator()
    pid, _, issues = g.run_deterministic(
        catalog, res, "ghost", output_root=str(tmp_path),
    )
    assert pid == -1
    assert any(i.severity == "error" for i in issues)
