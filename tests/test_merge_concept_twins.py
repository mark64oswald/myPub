"""merge_concept_twins: find acronym / parenthetical / spelling twins; merge safely."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import merge_concept_twins as mct  # noqa: E402


def test_name_rules():
    assert mct.initials("Retrieval-Augmented Generation") == "RAG"
    assert mct.initials("Command Query Responsibility Segregation") == "CQRS"
    assert mct.initials("Kafka") is None
    assert mct.acronym_key("RAG") == "RAG" and mct.acronym_key("LLMs") == "LLM"
    assert mct.acronym_key("Kafka") is None and mct.acronym_key("Python") is None
    rows = [(1, "RAG"), (2, "Retrieval-Augmented Generation"),
            (3, "Retrieval Augmented Generation (RAG)"), (4, "Microservices"),
            (5, "Micro-services"), (6, "Kafka"), (7, "Real Application Gateway")]
    pairs = {(a, b, r) for a, b, r in mct.candidate_pairs(rows)}
    assert (1, 2, "acronym") in pairs and (1, 7, "acronym") in pairs
    assert (1, 3, "parenthetical") in pairs
    assert (4, 5, "spelling") in pairs and (2, 3, "spelling") not in pairs


@pytest.fixture
def world(tmp_path):
    conn = duckdb.connect(str(tmp_path / "c.ddb"))
    conn.execute((ROOT / "schemas" / "catalog.sql").read_text())

    def concept(name, vec=None, desc=""):
        cid = conn.execute("INSERT INTO concept (name, description) VALUES (?, ?) "
                           "RETURNING concept_id", [name, desc]).fetchone()[0]
        if vec is not None:
            conn.execute("INSERT INTO concept_embedding (concept_id, embedding) VALUES (?, ?)",
                         [cid, (vec + [0.0] * 384)[:384]])
        return cid

    ids = {
        "rag": concept("RAG", [1.0, 0.1]),
        "long": concept("Retrieval-Augmented Generation", [1.0, 0.2]),
        "gateway": concept("Real Application Gateway", [0.0, 1.0]),   # acronym clash
        "llm": concept("LLM", [0.9, 0.5]),
        "vec": concept("Vector Database", [0.5, 0.5]),
    }
    rel = lambda a, b, sid: conn.execute(  # noqa: E731
        "INSERT INTO concept_relation (from_concept_id, to_concept_id, relation_type, "
        "source_type, source_id) VALUES (?, ?, 'REQUIRES', 'chapter', ?)", [a, b, sid])
    rel(ids["long"], ids["llm"], 1)
    rel(ids["long"], ids["vec"], 2)
    rel(ids["long"], ids["vec"], 3)
    rel(ids["rag"], ids["llm"], 1)       # duplicates long's edge after the merge
    rel(ids["rag"], ids["long"], 4)      # becomes a self-loop
    rel(ids["vec"], ids["rag"], 5)
    p = conn.execute("INSERT INTO procedure (name) VALUES ('p') RETURNING procedure_id").fetchone()[0]
    conn.execute("INSERT INTO procedure_concept VALUES (?, ?), (?, ?)", [p, ids["rag"], p, ids["long"]])
    return conn, ids


def test_find_groups_needs_evidence(world):
    conn, ids = world
    groups = mct.find_groups(conn)
    assert len(groups) == 1
    g = groups[0]
    assert g["canonical"]["concept_id"] == ids["long"]          # better covered
    assert [t["concept_id"] for t in g["twins"]] == [ids["rag"]]  # not the gateway


def test_merge_moves_children_and_leaves_an_alias(world, tmp_path):
    conn, ids = world
    plan = tmp_path / "twins.json"
    plan.write_text(json.dumps(mct.find_groups(conn)))
    conn.close()
    assert mct.main(["merge", "--plan", str(plan), "--catalog", str(tmp_path / "c.ddb"),
                     "--write"]) == 0
    conn = duckdb.connect(str(tmp_path / "c.ddb"))
    rag, long = ids["rag"], ids["long"]
    assert conn.execute("SELECT COUNT(*) FROM concept WHERE concept_id = ?", [rag]).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM concept_relation WHERE ? IN (from_concept_id, to_concept_id)",
                        [rag]).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM concept_relation WHERE from_concept_id = to_concept_id"
                        ).fetchone()[0] == 0
    # long→llm (dup dropped), long→vec ×2, vec→long (moved)
    assert conn.execute("SELECT COUNT(*) FROM concept_relation").fetchone()[0] == 4
    assert conn.execute("SELECT concept_id FROM procedure_concept").fetchall() == [(long,)]
    assert conn.execute("SELECT concept_id FROM concept_alias WHERE alias = 'RAG'").fetchone()[0] == long
    trail = json.loads(plan.with_suffix(".merged.json").read_text())
    assert trail["merged_into"] == {str(rag): long} and len(trail["embeddings"]) == 1


def test_mismatched_parenthetical_is_not_a_twin():
    assert not mct._parenthetical_consistent("Resource Description Framework Schema", "RDF")
    assert mct._parenthetical_consistent("Retrieval Augmented Generation", "RAG")
    rows = [(1, "RDF"), (2, "Resource Description Framework Schema (RDF)")]
    assert mct.candidate_pairs(rows) == []


def test_whole_word_description_mention():
    assert mct._mentions("RDF", "Built on RDF triples.")
    assert not mct._mentions("RDF", "RDFS adds classes.")


def test_acronym_keeps_only_its_closest_expansion():
    names = {1: "SQL", 2: "Structured Query Language", 3: "Single Query Language",
             4: "SQL (Structured Query Language)"}
    confirmed = [(1, 2, "acronym", {"cosine": 0.9}), (1, 3, "acronym", {"cosine": 0.7}),
                 (1, 4, "parenthetical", {"cosine": 0.95})]
    kept = mct._one_expansion_per_acronym(confirmed, names)
    assert [(a, b) for a, b, *_ in kept] == [(1, 2), (1, 4)]


def test_family_parenthetical_and_plural_acronym_guards():
    rows = [(1, "RDF"), (2, "RDF(S)"), (3, "RDFS"), (4, "RDFs"), (5, "RNN"), (6, "RNNs")]
    pairs = {(a, b, r) for a, b, r in mct.candidate_pairs(rows)}
    assert not any({a, b} == {1, 2} for a, b, _ in pairs)   # RDF(S) isn't RDF
    assert (3, 4, "spelling") not in pairs                   # RDFS isn't RDFs
    assert (1, 4, "acronym-variant") in pairs and (5, 6, "acronym-variant") in pairs


def test_acronym_variants_and_expansion_spellings_stay_together():
    names = {1: "RNN", 2: "RNNs", 3: "Recurrent Neural Network",
             4: "Recurrent Neural Networks", 5: "Random Neuron Net"}
    confirmed = [(1, 3, "acronym", {"cosine": 0.90}), (2, 4, "acronym", {"cosine": 0.85}),
                 (1, 5, "acronym", {"cosine": 0.62}), (1, 2, "acronym-variant", {"cosine": 0.95})]
    kept = {(a, b) for a, b, *_ in mct._one_expansion_per_acronym(confirmed, names)}
    assert kept == {(1, 3), (2, 4), (1, 2)}


def test_expansion_key_ignores_filler_and_punctuation():
    assert mct.expansion_key("Extract, Transform, and Load") == mct.expansion_key("Extract-Transform-Load")
    rows = [(1, "ETL (Extract, Transform, Load)"), (2, "Extract Transform Load")]
    assert (1, 2, "parenthetical") in set(mct.candidate_pairs(rows))
