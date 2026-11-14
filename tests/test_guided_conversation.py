"""Tests for guided_conversation.py (/kb-guided-conversation)."""
from __future__ import annotations

import json
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

import agent_skills  # noqa: E402
import guided_conversation as gc  # noqa: E402


@pytest.fixture
def catalog(tmp_path):
    conn = duckdb.connect(str(tmp_path / "catalog.ddb"))
    conn.execute(SCHEMA_FILE.read_text())
    yield conn
    conn.close()


def _concept(conn, name, ctype="Concept", desc=""):
    return conn.execute(
        "INSERT INTO concept (name, concept_type, description) VALUES (?, ?, ?) "
        "RETURNING concept_id", [name, ctype, desc]).fetchone()[0]


_N = [0]


def _rel(conn, a, b, rel):
    _N[0] += 1
    conn.execute(
        "INSERT INTO concept_relation (from_concept_id, to_concept_id, relation_type, "
        "source_type, source_id, confidence) VALUES (?, ?, ?, 'chapter', ?, 0.9)",
        [a, b, rel, _N[0]])


def _proc(conn, cid, name, steps, pre=None):
    pid = conn.execute(
        "INSERT INTO procedure (name, steps, preconditions) VALUES (?, ?, ?) "
        "RETURNING procedure_id", [name, steps, pre]).fetchone()[0]
    conn.execute("INSERT INTO procedure_concept (procedure_id, concept_id) VALUES (?, ?)",
                 [pid, cid])
    return pid


def _resolver(conn):
    r = MagicMock()

    def look(n):
        row = conn.execute("SELECT concept_id FROM concept WHERE lower(name) = lower(?) "
                           "ORDER BY concept_id LIMIT 1", [n.strip()]).fetchone()
        return int(row[0]) if row else None
    r.resolve_lookup_only.side_effect = look
    return r


@pytest.fixture
def corpus(catalog):
    es = _concept(catalog, "Event Sourcing", "Pattern", "Store state as a log of events.")
    ddd = _concept(catalog, "Domain Events", "Concept", "Facts that happened in the domain.")
    agg = _concept(catalog, "Aggregates", "Concept", "Consistency boundaries.")
    crud = _concept(catalog, "CRUD", "Pattern", "Overwrite current state in place.")
    snap = _concept(catalog, "Snapshotting", "Technique", "Cache aggregate state.")
    _rel(catalog, es, ddd, "REQUIRES")
    _rel(catalog, ddd, agg, "REQUIRES")
    _rel(catalog, es, crud, "CONTRASTS_WITH")
    _proc(catalog, es, "Design an event store",
          "1. List the aggregates\n2. Define event schemas\n3. Choose a stream key",
          pre="You know your aggregates.")
    _proc(catalog, es, "Replay events into a projection", "1. Read the stream\n2. Fold events")
    return {"es": es, "ddd": ddd, "agg": agg, "crud": crud, "snap": snap}


def _run(catalog, tmp_path, goal, output, **kw):
    g = gc.make_guided_conversation_generator()
    pid, report, issues = g.run_deterministic(
        catalog, _resolver(catalog), goal, output=output, output_root=str(tmp_path), **kw)
    return pid, report, issues


@pytest.mark.parametrize("output,expected", [
    ("a design doc", "document"), ("an ADR for the team", "decision"),
    ("a migration plan", "plan"), ("a working prototype", "code"),
    ("a deployment runbook", "checklist"), ("something nice", "document"),
])
def test_detect_output_type(output, expected):
    assert gc.detect_output_type(output) == expected


def test_detect_output_type_rejects_unknown_hint():
    with pytest.raises(ValueError):
        gc.detect_output_type("x", "poem")


def test_topics_from_goal_prefers_longest_resolving_phrase(catalog, corpus):
    names = gc.topics_from_goal(catalog, _resolver(catalog),
                                "Design an order service with event sourcing and a simple design")
    assert names == ["Event Sourcing"]


def test_steps_follow_the_kb(catalog, corpus):
    c = gc.ConversationDecomposer().decompose(
        catalog, _resolver(catalog), "Design an order service with event sourcing",
        output="a design doc")
    kinds = [s.kind for s in c.steps]
    assert kinds[0] == "frame" and kinds[-2:] == ["produce", "review"]
    assert [p.name for p in c.prereqs] == ["Domain Events", "Aggregates"]   # nearest first
    assert [o.name for o in c.decisions[0][1]] == ["Event Sourcing", "CRUD"]
    assert {s.title for s in c.steps if s.kind == "work"} == {
        "Design an event store", "Replay events into a projection"}
    work = next(s for s in c.steps if s.title == "Design an event store")
    # A design doc uses procedures as input; it doesn't run them.
    assert "not running it" in work.purpose


def test_hands_on_output_walks_the_procedure(catalog, corpus):
    c = gc.ConversationDecomposer().decompose(
        catalog, _resolver(catalog), "Build an event-sourced order service", output="code")
    work = next(s for s in c.steps if s.title == "Design an event store")
    assert "You know your aggregates." in work.ask[0]
    assert "one at a time" in work.do[0]


def test_generic_words_and_hyphens_in_topic_picking(catalog, corpus):
    _concept(catalog, "Design", "Concept", "Shaping a thing.")
    _rel(catalog, _concept(catalog, "Canva"), catalog.execute(
        "SELECT concept_id FROM concept WHERE name = 'Design'").fetchone()[0], "REQUIRES")
    names = gc.topics_from_goal(catalog, _resolver(catalog),
                                "Design an event-sourced order service")
    assert names == ["Event Sourcing"]


def test_comparison_goal_makes_one_decision(catalog, corpus):
    kafka = _concept(catalog, "Apache Kafka", "Tool", "A log-based event broker.")
    pulsar = _concept(catalog, "Apache Pulsar", "Tool", "A tiered event broker.")
    topic = _concept(catalog, "Topic", "Concept", "A Kafka stream unit.")
    _rel(catalog, kafka, topic, "CONTRASTS_WITH")
    _rel(catalog, kafka, pulsar, "CONTRASTS_WITH")
    _proc(catalog, kafka, "Download and extract Apache Kafka", "1. Download\n2. Extract")
    c = gc.ConversationDecomposer().decompose(
        catalog, _resolver(catalog),
        "Choose between Apache Kafka and Apache Pulsar for our event backbone",
        output="an ADR")
    assert c.output_type == "decision" and c.comparison
    decides = [s for s in c.steps if s.kind == "decide"]
    assert [s.title for s in decides] == ["Compare Apache Kafka and Apache Pulsar"]
    assert not [s for s in c.steps if s.kind == "work"]       # ADR: no how-to steps


def test_alternatives_drop_parts_and_other_kinds(catalog, corpus):
    kafka = _concept(catalog, "Apache Kafka", "Tool", "A log-based event broker.")
    for name, ctype in (("Transactions in Kafka", "Tool"), ("kafka-topics.sh", "Tool"),
                        ("Topic", "Concept"), ("RabbitMQ", "Tool")):
        _rel(catalog, kafka, _concept(catalog, name, ctype), "CONTRASTS_WITH")
    tm = gc.ks.topic_material(catalog, _resolver(catalog), "Apache Kafka")
    opts, dropped = gc.alternatives(catalog, tm)
    assert [o.name for o in opts] == ["RabbitMQ"]
    assert dropped == 3


def test_procedures_ranked_by_goal_words(catalog, corpus):
    es = corpus["es"]
    _proc(catalog, es, "Add a Slack hook to the event store", "1. Create webhook")
    _proc(catalog, es, "Migrate a CRUD table to event sourcing", "1. Backfill events")
    c = gc.ConversationDecomposer().decompose(
        catalog, _resolver(catalog), "Migrate a CRUD orders table to event sourcing",
        output="a migration plan", topics=["Event Sourcing"], max_procedures=1)
    assert [p.name for p in c.procedures] == ["Migrate a CRUD table to event sourcing"]


def test_max_steps_trims_assess_then_work(catalog, corpus):
    c = gc.ConversationDecomposer().decompose(
        catalog, _resolver(catalog), "event sourcing", output="a doc",
        topics=["Event Sourcing"], max_steps=5)
    kinds = [s.kind for s in c.steps]
    assert len(kinds) == 5
    assert "assess" not in kinds and "decide" in kinds
    assert any("trimmed" in n for n in c.notes)


def test_package_is_a_valid_skill(catalog, corpus, tmp_path):
    pid, report, issues = _run(catalog, tmp_path, "Design an order service with event sourcing",
                               "a design doc")
    assert pid > 0, issues
    meta = json.loads(catalog.execute(
        "SELECT metadata_json FROM generated_package WHERE package_id = ?", [pid]).fetchone()[0])
    name = meta["skill_name"]
    assert name.startswith("guide-")
    pkg = tmp_path / report.package_name
    md = (pkg / name / "SKILL.md").read_text()
    assert f"name: {name}\n" in md
    assert "disable-model-invocation: true" in md
    assert "### 1. Frame the goal" in md and "**Done when:**" in md
    assert not [p for p in meta["spec_problems"] if not p.startswith("warning")]
    for ref in ("background", "options", "procedures", "output-template", "sources"):
        assert (pkg / name / "references" / f"{ref}.md").exists()
    assert "## Design an event store" in (pkg / name / "references/output-template.md").read_text()
    flow = json.loads((pkg / "conversation.json").read_text())
    assert flow["steps"][0]["key"] == "frame"
    assert "success_test" in flow["state"]
    assert (pkg / "_prompts/refine-conversation.md").exists()
    # description fits the spec
    fm_desc = md.split("description: ", 1)[1].split("\n", 1)[0].strip('"')
    assert len(fm_desc) <= agent_skills.DESCRIPTION_MAX


def test_auto_invocation_leaves_model_invocation_on(catalog, corpus, tmp_path):
    pid, report, _ = _run(catalog, tmp_path, "event sourcing", "a plan",
                          topics=["Event Sourcing"], invocation="auto", skill_name="es-plan")
    md = (tmp_path / report.package_name / "es-plan" / "SKILL.md").read_text()
    assert "disable-model-invocation" not in md


def test_unknown_goal_still_generates_a_generic_flow(catalog, tmp_path):
    pid, report, issues = _run(catalog, tmp_path, "write a haiku about tea", "a poem doc")
    assert pid > 0
    assert any("no KB topics" in i.message for i in issues)
    flow = json.loads((tmp_path / report.package_name / "conversation.json").read_text())
    assert [s["kind"] for s in flow["steps"]] == ["frame", "produce", "review"]


def test_migration_goal_has_no_decision_and_prefers_migration_procedures(catalog, corpus):
    es, crud = corpus["es"], corpus["crud"]
    _rel(catalog, crud, _concept(catalog, "Active Record", "Pattern"), "CONTRASTS_WITH")
    _proc(catalog, crud, "Add an index to a CRUD table", "1. Create index on the crud table")
    _proc(catalog, es, "Convert a CRUD model to event sourcing", "1. Emit events")
    c = gc.ConversationDecomposer().decompose(
        catalog, _resolver(catalog), "Migrate a CRUD service to event sourcing",
        output="a migration plan", topics=["CRUD", "Event Sourcing"], max_procedures=1)
    assert not [s for s in c.steps if s.kind == "decide"]
    assert [p.name for p in c.procedures] == ["Convert a CRUD model to event sourcing"]


def test_stray_verbs_are_not_topics(catalog):
    built = _concept(catalog, "Built", "Concept", "Past tense.")
    lexa = _concept(catalog, "Variant Interpretation", "Concept", "Reading variants.")
    for a, b in ((built, lexa), (lexa, built)):
        _rel(catalog, a, b, "REQUIRES")
    assert gc.topics_from_goal(catalog, _resolver(catalog),
                               "Variant interpretation tutor built on Lexa") == [
        "Variant Interpretation"]
