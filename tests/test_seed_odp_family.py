"""Tests for the Open Data Product family seed (scripts/seed_odp_family.py)."""
from __future__ import annotations

import numpy as np
import pytest

import seed_doc_sources
import seed_odp_family as odp
from resolution import EntityResolver


class FakeModel:
    """Stands in for sentence-transformers: a fixed 384-d vector per text."""

    def encode(self, texts, convert_to_numpy=True):
        out = []
        for t in texts:
            rng = np.random.default_rng(abs(hash(t)) % (2 ** 32))
            v = rng.random(384).astype("float32")
            out.append(v / np.linalg.norm(v))
        return np.array(out)


@pytest.fixture(scope="module")
def fam():
    return odp.load_family()


# ---------------------------------------------------------------------------
# The curated files
# ---------------------------------------------------------------------------

def test_family_file_is_consistent(fam):
    assert odp.validate_family(fam) == []
    assert set(fam.specs) == {"odps", "odpc", "odpg", "odpv", "odpr", "sdk", "odcs"}


def test_odpv_has_78_terms(fam):
    groups: dict[str, int] = {}
    for t in fam.terms + fam.relationships:
        groups[t.group] = groups.get(t.group, 0) + 1
    assert groups == {"core": 18, "value": 17, "governance": 19, "relationships": 24}


def test_family_map_tables_are_current(fam):
    md = odp.FAMILY_MAP_MD.read_text()
    assert odp.render_family_map(fam, md) == md


def test_family_map_has_a_section_per_odpv_group():
    headings = odp.markdown_headings(odp.FAMILY_MAP_MD.read_text())
    for h in odp.ODPV_GROUP_HEADINGS.values():
        assert h in headings


def test_source_seeds_are_valid_and_unique():
    for seed in odp.ODP_SOURCE_SEEDS:
        seed_doc_sources._validate(seed)  # pylint: disable=protected-access
    idents = [s["identifier"] for s in odp.ODP_SOURCE_SEEDS]
    assert len(idents) == len(set(idents))
    assert odp.FAMILY_MAP_ID in idents
    # The family map never outranks the specs it summarizes.
    fm = next(s for s in odp.ODP_SOURCE_SEEDS if s["identifier"] == odp.FAMILY_MAP_ID)
    assert fm["authority_score"] < odp.SPEC_AUTHORITY


# ---------------------------------------------------------------------------
# Naming and aliases
# ---------------------------------------------------------------------------

def _term(fam, tid):
    return next(t for t in fam.terms if t.id == tid)


def test_generic_one_word_terms_are_qualified(fam):
    assert odp.term_concept_name(_term(fam, "Domain"), fam.merge_single_word) == "Domain (ODPV)"
    assert odp.term_concept_name(_term(fam, "Risk"), fam.merge_single_word) == "Risk (ODPV)"
    assert odp.term_concept_name(_term(fam, "KPI"), fam.merge_single_word) == "KPI"
    assert odp.term_concept_name(_term(fam, "DataProduct"), fam.merge_single_word) == "Data Product"


def test_aliases_leave_out_loose_words(fam):
    for t in fam.terms:
        name = odp.term_concept_name(t, fam.merge_single_word)
        for a in odp.term_aliases(t, name):
            if " " not in a:
                # camelCase ids/field names, or an acronym id
                assert a == t.id and a.isupper() or any(
                    c.isupper() for c in a[1:]), (t.id, a)
    ds = _term(fam, "DataService")
    assert "API" not in odp.term_aliases(ds, "Data Service")
    kpi = _term(fam, "KPI")
    assert "key performance indicator" in odp.term_aliases(kpi, "KPI")
    assert "metric" not in odp.term_aliases(kpi, "KPI")


def test_relationship_lines_use_domain_and_range(fam):
    lines = odp.relationship_lines(_term(fam, "UseCase"), fam.relationships)
    assert any(line.startswith("UseCase requires ") for line in lines)


# ---------------------------------------------------------------------------
# Plan + apply on a schema-only catalog
# ---------------------------------------------------------------------------

def _load_family_map(conn) -> int:
    src = conn.execute(
        "INSERT INTO doc_source (name, source_type, mcp_server, identifier) "
        "VALUES ('ODP family map (myPub)', 'github_raw', 'github_raw', ?) "
        "RETURNING doc_source_id", [odp.FAMILY_MAP_ID]).fetchone()[0]
    snap = conn.execute("INSERT INTO doc_snapshot (doc_source_id, content) VALUES (?, 'x') "
                        "RETURNING snapshot_id", [src]).fetchone()[0]
    for i, h in enumerate(odp.markdown_headings(odp.FAMILY_MAP_MD.read_text())):
        conn.execute("INSERT INTO doc_section (snapshot_id, heading_level, heading_text, "
                     "ordinal, content) VALUES (?, 2, ?, ?, 'body')", [snap, h, i])
    return src


def _concept(conn, name, ctype="Concept") -> int:
    cid = conn.execute("INSERT INTO concept (name, concept_type) VALUES (?, ?) "
                       "RETURNING concept_id", [name, ctype]).fetchone()[0]
    conn.execute("INSERT INTO concept_embedding (concept_id, embedding) VALUES (?, ?)",
                 [cid, [0.0] * 384])
    return cid


def _section_id(conn, heading) -> int:
    return conn.execute("SELECT doc_section_id FROM doc_section WHERE heading_text = ?",
                        [heading]).fetchone()[0]


def test_map_requires_the_family_map(schema_only_conn, fam):
    resolver = EntityResolver(schema_only_conn, model=FakeModel())
    with pytest.raises(SystemExit, match="family map"):
        odp.plan_map(schema_only_conn, fam, resolver)


def test_plan_and_apply(schema_only_conn, fam):
    conn = schema_only_conn
    _load_family_map(conn)
    data_product = _concept(conn, "Data Product")
    ddd_domain = _concept(conn, "Domain")
    scrum_owner = _concept(conn, "Product Owner")
    resolver = EntityResolver(conn, model=FakeModel())

    plan = odp.plan_map(conn, fam, resolver)
    assert plan.concepts["odpv:DataProduct"].concept_id == data_product
    assert plan.concepts["odpv:Domain"].concept_id is None          # not the DDD Domain
    assert plan.concepts["odps"].concept_id is None
    # "concept:Data Mesh" isn't in this catalog, so its edge is skipped;
    # "concept:Data Contract" is the ODPV term planned here.
    assert any("Data Mesh" in s for s in plan.skipped)
    assert "concept:Data Contract" not in plan.concepts
    # "concept:Knowledge Graph" must not resolve through our own aliases.
    assert any("Knowledge Graph" in s for s in plan.skipped)

    summary = odp.apply_map(conn, plan, resolver)
    assert summary.concepts_created == len(plan.concepts) - 1   # all but Data Product
    assert summary.edges_added > 100

    def cid(name):
        return conn.execute("SELECT concept_id FROM concept WHERE name = ?", [name]).fetchone()[0]

    odps_id, odpc_id = cid("Open Data Product Specification"), cid("Open Data Product Catalogs")
    row = conn.execute(
        "SELECT confidence, source_type, source_id FROM concept_relation "
        "WHERE from_concept_id = ? AND to_concept_id = ? AND relation_type = 'REQUIRES'",
        [odpc_id, odps_id]).fetchone()
    assert row == (1.0, "doc_section", _section_id(conn, "ODPC: Open Data Product Catalogs"))

    # ODPS IMPLEMENTS the books' Data Product concept; ODPS REQUIRES the ODPV term too.
    rels = {r[0] for r in conn.execute(
        "SELECT relation_type FROM concept_relation WHERE from_concept_id = ? "
        "AND to_concept_id = ?", [odps_id, data_product]).fetchall()}
    assert rels == {"IMPLEMENTS", "REQUIRES"}

    # Unrelated same-name concepts are left alone.
    for other in (ddd_domain, scrum_owner):
        assert conn.execute("SELECT count(*) FROM concept_relation WHERE from_concept_id = ? "
                            "OR to_concept_id = ?", [other, other]).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM concept_alias WHERE concept_id = ?",
                            [other]).fetchone()[0] == 0
    # "product owner" names nothing else here as an alias, but the Scrum
    # concept already has that name, so the ODPV Owner doesn't take it.
    owner = cid("Owner (ODPV)")
    aliases = {r[0] for r in conn.execute(
        "SELECT alias FROM concept_alias WHERE concept_id = ?", [owner]).fetchall()}
    assert "product owner" not in {a.lower() for a in aliases}
    assert "accountable owner" in aliases

    # Spec aliases resolve.
    assert resolver.resolve_lookup_only("ODPS") == odps_id
    # Descriptions carry ODPV relationships.
    desc = conn.execute("SELECT description FROM concept WHERE name = 'Use Case'").fetchone()[0]
    assert "UseCase requires" in desc
    # Linked to the family map source.
    assert conn.execute("SELECT count(*) FROM concept_doc_link WHERE notes = ?",
                        [odp.LINK_NOTE]).fetchone()[0] >= len(plan.concepts) - 1

    # Re-running changes nothing.
    again = odp.apply_map(conn, odp.plan_map(conn, fam, resolver), resolver)
    assert again.concepts_created == 0
    assert again.edges_added == 0
    assert again.aliases_added == 0
    assert again.doc_links_added == 0
