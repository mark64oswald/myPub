"""topic_gate + its use in Dialog, Content Brief and Tutorial."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import duckdb
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp-servers" / "kb-mcp"))

import content_brief as cb  # noqa: E402
import topic_gate as tg  # noqa: E402
import tutorial as tut  # noqa: E402


@pytest.fixture
def conn(tmp_path):
    c = duckdb.connect(str(tmp_path / "c.ddb"))
    c.execute((ROOT / "schemas" / "catalog.sql").read_text())
    yield c
    c.close()


def _concept(conn, name, vec=None):
    cid = conn.execute("INSERT INTO concept (name) VALUES (?) RETURNING concept_id",
                       [name]).fetchone()[0]
    if vec is not None:
        conn.execute("INSERT INTO concept_embedding (concept_id, embedding) VALUES (?, ?)",
                     [cid, (vec + [0.0] * 384)[:384]])
    return cid


def test_gate_drops_off_topic_and_passes_unembedded(conn):
    topic = _concept(conn, "Delta Lake", [1.0, 0.0])
    near = _concept(conn, "Parquet", [0.9, 0.1])
    far = _concept(conn, "Machine Learning", [0.0, 1.0])
    blank = _concept(conn, "Mystery")
    kept, dropped, sims = tg.gate_concepts(conn, topic, [near, far, blank])
    assert kept == [near, blank] and dropped == [far]
    assert sims[blank] is None
    assert tg.gate_concepts(conn, topic, [far], 0)[0] == [far]          # off switch
    no_vec = _concept(conn, "Unembedded topic")
    assert tg.gate_concepts(conn, no_vec, [far])[0] == [far]           # nothing to compare


def test_content_brief_anchors_skip_off_topic_neighbours(conn):
    topic = _concept(conn, "Delta Lake", [1.0, 0.0])
    near = _concept(conn, "Parquet", [0.9, 0.1])
    far = _concept(conn, "Machine Learning", [0.0, 1.0])
    for i, other in enumerate([near, far, far]):
        conn.execute("INSERT INTO concept_relation (from_concept_id, to_concept_id, relation_type, "
                     "source_type, source_id) VALUES (?, ?, 'RELATED_TO', 'chapter', ?)",
                     [topic, other, i])
    res = MagicMock()
    res.resolve_lookup_only.return_value = topic
    d = cb.RhetoricalContentDecomposer().decompose(conn, res, "Delta Lake")
    anchors = {s.anchor_concept_id for s in d.sections}
    assert far not in anchors and near in anchors
    assert any("skipped as section anchors" in n for n in d.notes)


def test_tutorial_procedure_must_be_on_topic(conn):
    topic = _concept(conn, "Delta Lake", [1.0, 0.0])
    stage = _concept(conn, "Data Pipeline", [0.7, 0.7])
    kafka = _concept(conn, "Kafka", [0.0, 1.0])
    off = conn.execute("INSERT INTO procedure (name, steps) VALUES ('Set up Kafka', 's') "
                       "RETURNING procedure_id").fetchone()[0]
    on = conn.execute("INSERT INTO procedure (name, steps) VALUES ('Write a Delta table', "
                      "'longer steps here') RETURNING procedure_id").fetchone()[0]
    conn.execute("INSERT INTO procedure_concept VALUES (?, ?), (?, ?), (?, ?), (?, ?)",
                 [off, stage, off, kafka, on, stage, on, topic])
    best = tut.ProcedureBackedDecomposer._best_procedure_for_stage(conn, [stage], topic_id=topic)
    assert best.procedure_id == on
    ungated = tut.ProcedureBackedDecomposer._best_procedure_for_stage(conn, [stage])
    assert ungated.procedure_id == off       # shorter steps win without the gate
