"""
Smoke tests for scripts/extract_entities.py.

Covers the LLM-agnostic pieces: prompt assembly, JSON parsing,
validation, and end-to-end `process_extraction_json` against a
realistic (HNSW-indexed) catalog. The LLM call itself is never
exercised — sub-agents produce the JSON in production; these tests
supply synthetic JSON to verify the coordinator behavior.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
MCP_DIR = PROJECT_ROOT / "mcp-servers" / "kb-mcp"
for _p in (SCRIPTS_DIR, MCP_DIR):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

import extract_entities as ee  # noqa: E402  # pylint: disable=wrong-import-position
from resolution import EntityResolver  # noqa: E402  # pylint: disable=wrong-import-position


# ----------------------------------------------------------------------------
# JSON parse + fence stripping
# ----------------------------------------------------------------------------

def test_parse_llm_json_strips_markdown_fences():
    fenced = '```json\n{"entities": [], "relations": []}\n```'
    parsed = ee.parse_llm_json(fenced)
    assert parsed == {"entities": [], "relations": []}


def test_parse_llm_json_handles_bare_json():
    parsed = ee.parse_llm_json('{"entities": [{"name": "x", "type": "Concept"}]}')
    assert parsed["entities"][0]["name"] == "x"


def test_parse_llm_json_raises_on_garbage():
    with pytest.raises(json.JSONDecodeError):
        ee.parse_llm_json("not json at all")


# ----------------------------------------------------------------------------
# _validate_extraction — the JSON → safe-tuple normalizer
# ----------------------------------------------------------------------------

def test_validate_extraction_drops_unknown_entity_types():
    raw = {
        "entities": [
            {"name": "Star Schema", "type": "Concept", "description": "..."},
            {"name": "Bogus", "type": "FrobnicatedWidget", "description": "..."},
        ],
        "relations": [],
    }
    entities, relations = ee._validate_extraction(raw)  # noqa: SLF001  # pylint: disable=protected-access
    assert len(entities) == 1
    assert entities[0]["name"] == "Star Schema"
    assert relations == []


def test_validate_extraction_drops_relations_with_unknown_endpoints():
    """A relation's from/to must reference one of the extracted entities.
    Orphans are dropped with a warning; no crash."""
    raw = {
        "entities": [
            {"name": "A", "type": "Concept", "description": ""},
            {"name": "B", "type": "Concept", "description": ""},
        ],
        "relations": [
            {"from": "A", "to": "B", "type": "EXTENDS", "confidence": 0.9},
            {"from": "A", "to": "Ghost", "type": "EXTENDS", "confidence": 0.9},
            {"from": "Ghost", "to": "B", "type": "EXTENDS", "confidence": 0.9},
        ],
    }
    _, relations = ee._validate_extraction(raw)  # pylint: disable=protected-access
    assert len(relations) == 1
    assert relations[0]["from"] == "A" and relations[0]["to"] == "B"


def test_validate_extraction_drops_invalid_relation_types():
    raw = {
        "entities": [
            {"name": "A", "type": "Concept", "description": ""},
            {"name": "B", "type": "Concept", "description": ""},
        ],
        "relations": [
            {"from": "A", "to": "B", "type": "HAS_FLAVOR_OF", "confidence": 1.0},
        ],
    }
    _, relations = ee._validate_extraction(raw)  # pylint: disable=protected-access
    assert relations == []


def test_validate_extraction_clamps_confidence():
    raw = {
        "entities": [
            {"name": "A", "type": "Concept", "description": ""},
            {"name": "B", "type": "Concept", "description": ""},
        ],
        "relations": [
            {"from": "A", "to": "B", "type": "EXTENDS", "confidence": 5.0},
            {"from": "A", "to": "B", "type": "REQUIRES", "confidence": -0.5},
            {"from": "A", "to": "B", "type": "CITES", "confidence": "not a number"},
        ],
    }
    _, relations = ee._validate_extraction(raw)  # pylint: disable=protected-access
    assert [r["confidence"] for r in relations] == [1.0, 0.0, 0.5]


def test_validate_extraction_drops_self_relations():
    raw = {
        "entities": [
            {"name": "A", "type": "Concept", "description": ""},
        ],
        "relations": [
            {"from": "A", "to": "A", "type": "EXTENDS", "confidence": 1.0},
        ],
    }
    _, relations = ee._validate_extraction(raw)  # pylint: disable=protected-access
    assert relations == []


def test_validate_extraction_drops_nameless_entities():
    raw = {
        "entities": [
            {"name": "", "type": "Concept"},
            {"name": "   ", "type": "Concept"},
            {"name": "Real", "type": "Concept"},
        ],
        "relations": [],
    }
    entities, _ = ee._validate_extraction(raw)  # pylint: disable=protected-access
    assert len(entities) == 1
    assert entities[0]["name"] == "Real"


# ----------------------------------------------------------------------------
# End-to-end process_extraction_json against the realistic substrate
# ----------------------------------------------------------------------------

def test_process_extraction_json_against_realistic(realistic_conn, embedder, seed_ids):
    """End-to-end: given a chapter and a synthetic LLM JSON, verify that
    concepts + concept_embedding rows are created, relations land on
    concept_relation with the right provenance, and re-running the same
    chapter clears prior edges before re-inserting (idempotency)."""
    chapter_id = seed_ids["chapter_id"]
    raw = {
        "entities": [
            {"name": "Star Schema", "type": "Concept",
             "description": "central fact table plus denormalized dimensions"},
            {"name": "Fact Table", "type": "Concept",
             "description": "table storing measurements at the grain of a business process"},
            {"name": "Inner Join", "type": "Technique",
             "description": "join that keeps only matched rows from both sides"},
        ],
        "relations": [
            {"from": "Star Schema", "to": "Fact Table", "type": "REQUIRES",
             "confidence": 0.95},
            {"from": "Inner Join", "to": "Fact Table", "type": "CITES",
             "confidence": 0.8},
        ],
    }

    resolver = EntityResolver(realistic_conn, model=embedder)
    summary = ee.process_extraction_json(realistic_conn, resolver, chapter_id, raw)

    assert summary.entities_extracted == 3
    assert summary.relations_written == 2

    # Concepts exist.
    by_name = {
        r[0]: r[1]
        for r in realistic_conn.execute(
            "SELECT name, concept_id FROM concept "
            "WHERE name IN ('Star Schema', 'Fact Table', 'Inner Join')"
        ).fetchall()
    }
    assert set(by_name.keys()) == {"Star Schema", "Fact Table", "Inner Join"}

    # Relations land with chapter provenance.
    rels = realistic_conn.execute(
        "SELECT from_concept_id, to_concept_id, relation_type, confidence "
        "FROM concept_relation WHERE source_type='chapter' AND source_id = ?",
        [chapter_id],
    ).fetchall()
    assert len(rels) == 2
    pairs = {(by_name[n1], n2) for n1, n2 in [
        ("Star Schema", "REQUIRES"), ("Inner Join", "CITES"),
    ]}
    observed = {(r[0], r[2]) for r in rels}
    assert pairs == observed

    # Idempotency: re-run with a different relation set; old edges are gone.
    raw2 = {
        "entities": raw["entities"],
        "relations": [
            {"from": "Star Schema", "to": "Inner Join", "type": "EXTENDS",
             "confidence": 0.7},
        ],
    }
    summary2 = ee.process_extraction_json(realistic_conn, resolver, chapter_id, raw2)
    assert summary2.prior_relations_cleared == 2
    post = realistic_conn.execute(
        "SELECT COUNT(*) FROM concept_relation "
        "WHERE source_type='chapter' AND source_id = ?",
        [chapter_id],
    ).fetchone()[0]
    assert post == 1


def test_process_extraction_empty_payload_is_harmless(realistic_conn, embedder, seed_ids):
    """Chapters that are pure front-matter should no-op cleanly, not crash."""
    resolver = EntityResolver(realistic_conn, model=embedder)
    summary = ee.process_extraction_json(
        realistic_conn, resolver, seed_ids["chapter_id"],
        {"entities": [], "relations": []},
    )
    assert summary.entities_extracted == 0
    assert summary.relations_written == 0


# ----------------------------------------------------------------------------
# Prompt assembly
# ----------------------------------------------------------------------------

def test_build_full_prompt_includes_system_and_chapter(realistic_conn, seed_ids):
    chapter = ee._load_chapter(realistic_conn, seed_ids["chapter_id"])  # pylint: disable=protected-access
    prompt = ee.build_full_prompt(chapter)
    # System prompt header
    assert "extract structured knowledge" in prompt.lower()
    # Chapter payload
    assert "__seed_chapter" in prompt
    assert "seed content" in prompt
    # Strict-JSON footer
    assert "Respond with JSON only" in prompt
