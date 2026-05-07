"""Tests for project_bootstrap.py + refactoring_playbook.py — Phase 15."""
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

import project_bootstrap as pb  # noqa: E402
import refactoring_playbook as rp  # noqa: E402


@pytest.fixture
def catalog(tmp_path):
    conn = duckdb.connect(str(tmp_path / "catalog.ddb"))
    conn.execute(SCHEMA_FILE.read_text())
    yield conn
    conn.close()


def _seed_concept(conn, name, concept_type="Concept", description=""):
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


def _seed_procedure(conn, name, steps_json='[{"n":1,"action":"a"}]'):
    return conn.execute(
        "INSERT INTO procedure (name, steps) VALUES (?, ?) RETURNING procedure_id",
        [name, steps_json],
    ).fetchone()[0]


def _link_proc(conn, pid, cid):
    conn.execute(
        "INSERT INTO procedure_concept (procedure_id, concept_id) VALUES (?, ?)",
        [pid, cid],
    )


def _resolver(name_to_id):
    r = MagicMock()
    r.resolve_lookup_only.side_effect = lambda n: name_to_id.get(n.strip().lower())
    return r


# ---------------------------------------------------------------------------
# Project Bootstrap
# ---------------------------------------------------------------------------


@pytest.fixture
def bootstrap_corpus(catalog):
    cqrs = _seed_concept(catalog, "CQRS", "Pattern", "Command/query split.")
    kafka = _seed_concept(catalog, "Apache Kafka", "Tool", "Event streaming platform.")
    hl7 = _seed_concept(catalog, "HL7", "Concept", "Healthcare messaging standard.")
    p1 = _seed_procedure(catalog, "Configure Kafka producer")
    _link_proc(catalog, p1, kafka)
    return {"cqrs": cqrs, "kafka": kafka, "hl7": hl7, "p1": p1}


def test_bootstrap_resolves_named_elements(catalog, bootstrap_corpus):
    res = _resolver({
        "cqrs": bootstrap_corpus["cqrs"],
        "apache kafka": bootstrap_corpus["kafka"],
        "hl7": bootstrap_corpus["hl7"],
    })
    d = pb.StackComposeDecomposer().decompose(
        catalog, res, "CQRS over Kafka for HL7",
        technologies=["Apache Kafka"], patterns=["CQRS"],
    )
    names = {el.name for el in d.elements}
    assert "CQRS" in names
    assert "Apache Kafka" in names


def test_bootstrap_emits_full_file_plan(catalog, bootstrap_corpus):
    res = _resolver({"cqrs": bootstrap_corpus["cqrs"]})
    d = pb.StackComposeDecomposer().decompose(
        catalog, res, "CQRS demo", patterns=["CQRS"],
    )
    paths = {pf.relative_path for pf in d.planned_files}
    assert "README.md" in paths
    assert "src/main.py" in paths
    assert "tests/test_smoke.py" in paths
    assert "docker-compose.yml" in paths


def test_bootstrap_skips_unresolved_names(catalog, bootstrap_corpus):
    res = _resolver({"cqrs": bootstrap_corpus["cqrs"]})
    d = pb.StackComposeDecomposer().decompose(
        catalog, res, "test", patterns=["CQRS", "GhostPattern"],
    )
    assert any("ghostpattern" in n.lower() for n in d.notes)


def test_bootstrap_no_named_elements_emits_note(catalog):
    res = _resolver({})
    d = pb.StackComposeDecomposer().decompose(
        catalog, res, "ghost project",
    )
    assert any("no named technologies" in n.lower() for n in d.notes)


def test_bootstrap_planner_emits_placeholder_and_prompt_per_file(catalog, bootstrap_corpus):
    res = _resolver({"cqrs": bootstrap_corpus["cqrs"]})
    d = pb.StackComposeDecomposer().decompose(
        catalog, res, "CQRS demo", patterns=["CQRS"],
    )
    plan = pb.ProjectBootstrapPlanner().plan(catalog, d)
    placeholder_paths = [f.filename for f in plan.files if f.purpose == "placeholder"]
    prompt_paths = [f.filename for f in plan.files if f.purpose == "subagent_prompt"]
    assert len(placeholder_paths) == len(d.planned_files)
    assert len(prompt_paths) == len(d.planned_files)
    assert all(p.startswith("_sub_agent_prompts/") for p in prompt_paths)


def test_bootstrap_validator_passes_for_valid_plan(catalog, bootstrap_corpus):
    res = _resolver({
        "cqrs": bootstrap_corpus["cqrs"],
        "apache kafka": bootstrap_corpus["kafka"],
    })
    d = pb.StackComposeDecomposer().decompose(
        catalog, res, "CQRS Kafka", patterns=["CQRS"], technologies=["Apache Kafka"],
    )
    plan = pb.ProjectBootstrapPlanner().plan(catalog, d)
    issues = pb.ProjectBootstrapValidator().validate(catalog, plan)
    errors = [i for i in issues if i.severity == "error"]
    assert errors == []


def test_bootstrap_run_deterministic_writes_project_tree(
    catalog, bootstrap_corpus, tmp_path,
):
    res = _resolver({
        "cqrs": bootstrap_corpus["cqrs"],
        "apache kafka": bootstrap_corpus["kafka"],
    })
    g = pb.make_project_bootstrap_generator()
    pid, report, _ = g.run_deterministic(
        catalog, res, "CQRS demo with Kafka",
        patterns=["CQRS"], technologies=["Apache Kafka"],
        project_name="cqrs-kafka-demo",
        output_root=str(tmp_path),
    )
    assert pid > 0
    pkg_dir = tmp_path / "cqrs-kafka-demo"
    assert (pkg_dir / "README.md").exists()
    assert (pkg_dir / "src" / "main.py").exists()
    assert (pkg_dir / "tests" / "test_smoke.py").exists()
    assert (pkg_dir / "_build_plan.md").exists()
    prompts = list((pkg_dir / "_sub_agent_prompts").glob("*.txt"))
    assert len(prompts) > 0


def test_bootstrap_idempotent(catalog, bootstrap_corpus, tmp_path):
    res = _resolver({"cqrs": bootstrap_corpus["cqrs"]})
    g = pb.make_project_bootstrap_generator()
    pid1, _, _ = g.run_deterministic(
        catalog, res, "CQRS", patterns=["CQRS"],
        project_name="proj", output_root=str(tmp_path),
    )
    pid2, _, _ = g.run_deterministic(
        catalog, res, "CQRS", patterns=["CQRS"],
        project_name="proj", output_root=str(tmp_path),
    )
    assert pid1 == pid2


# ---------------------------------------------------------------------------
# Refactoring Playbook
# ---------------------------------------------------------------------------


@pytest.fixture
def refactor_corpus(catalog):
    """Topic with anti-pattern + recommended refactor."""
    domain = _seed_concept(catalog, "Distributed Systems")
    anti = _seed_concept(catalog, "Tight-Loop Retry", "Anti-Pattern",
                          "Retry without backoff overwhelms downstream.")
    target = _seed_concept(catalog, "Exponential Backoff Retry", "Pattern",
                            "Retry with exponential backoff and jitter.")
    _add_rel(catalog, domain, anti, "REQUIRES")
    _add_rel(catalog, anti, target, "CONTRASTS_WITH")
    p1 = _seed_procedure(catalog, "Implement exponential backoff")
    _link_proc(catalog, p1, target)
    return {"domain": domain, "anti": anti, "target": target, "p1": p1}


def test_refactor_unknown_topic_returns_negative(catalog):
    res = _resolver({})
    g = rp.make_refactoring_generator()
    pid, _, _ = g.run_deterministic(catalog, res, "ghost", output_root="/tmp")
    assert pid == -1


def test_refactor_finds_anti_patterns_via_contrasts(catalog, refactor_corpus):
    res = _resolver({"distributed systems": refactor_corpus["domain"]})
    d = rp.AntiPatternDecomposer().decompose(catalog, res, "Distributed Systems")
    assert len(d.findings) >= 1
    names = {f.anti_name for f in d.findings}
    assert "Tight-Loop Retry" in names


def test_refactor_attaches_target_procedures(catalog, refactor_corpus):
    res = _resolver({"distributed systems": refactor_corpus["domain"]})
    d = rp.AntiPatternDecomposer().decompose(catalog, res, "Distributed Systems")
    f = next(f for f in d.findings if f.anti_name == "Tight-Loop Retry")
    assert "Implement exponential backoff" in f.refactor_procedure_names


def test_refactor_topic_without_findings_emits_note(catalog):
    seed = _seed_concept(catalog, "Lonely")
    res = _resolver({"lonely": seed})
    d = rp.AntiPatternDecomposer().decompose(catalog, res, "Lonely")
    assert any("no anti-pattern" in n for n in d.notes)


def test_refactor_planner_emits_findings_md_and_per_finding(catalog, refactor_corpus):
    res = _resolver({"distributed systems": refactor_corpus["domain"]})
    d = rp.AntiPatternDecomposer().decompose(catalog, res, "Distributed Systems")
    plan = rp.RefactoringPlanner().plan(catalog, d)
    fnames = {f.filename for f in plan.files}
    assert "_findings.md" in fnames
    assert any(name.startswith("refactors/") for name in fnames)


def test_refactor_run_deterministic_writes_files(catalog, refactor_corpus, tmp_path):
    res = _resolver({"distributed systems": refactor_corpus["domain"]})
    g = rp.make_refactoring_generator()
    pid, _, _ = g.run_deterministic(
        catalog, res, "Distributed Systems", output_root=str(tmp_path),
    )
    assert pid > 0
    pkg_dir = tmp_path / "distributed-systems"
    assert (pkg_dir / "_findings.md").exists()
    assert any((pkg_dir / "refactors").glob("*.md"))


def test_refactor_idempotent(catalog, refactor_corpus, tmp_path):
    res = _resolver({"distributed systems": refactor_corpus["domain"]})
    g = rp.make_refactoring_generator()
    pid1, _, _ = g.run_deterministic(
        catalog, res, "Distributed Systems", output_root=str(tmp_path),
    )
    pid2, _, _ = g.run_deterministic(
        catalog, res, "Distributed Systems", output_root=str(tmp_path),
    )
    assert pid1 == pid2
