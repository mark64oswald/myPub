"""Tests for scripts/realign_contradictions.py — voting re-alignment."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import realign_contradictions as rc  # noqa: E402

BOOK_TEXT = ("Kafka consumers commit offsets to ZooKeeper by default, and the "
             "consumer group coordinator lives in ZooKeeper as well.")
DOC_TEXT = ("Since Kafka 4.0, KRaft mode is the only supported mode and ZooKeeper "
            "has been removed entirely from the broker.")


@pytest.fixture
def catalog(tmp_path):
    path = tmp_path / "catalog.ddb"
    conn = duckdb.connect(str(path))
    conn.execute((PROJECT_ROOT / "schemas" / "catalog.sql").read_text())
    cid = conn.execute("INSERT INTO concept (name) VALUES ('ZooKeeper') RETURNING concept_id").fetchone()[0]
    bid = conn.execute("INSERT INTO book (title, source_path, publication_date) VALUES "
                       "('Kafka Guide', '/x.epub', DATE '2017-07-01') RETURNING book_id").fetchone()[0]
    chid = conn.execute("INSERT INTO chapter (book_id, title, content) VALUES (?, 'Consumers', ?) "
                        "RETURNING chapter_id", [bid, BOOK_TEXT]).fetchone()[0]
    sid = conn.execute("INSERT INTO doc_source (name, source_type, mcp_server, identifier) "
                       "VALUES ('kafka', 'oss', 'context7', '/apache/kafka') "
                       "RETURNING doc_source_id").fetchone()[0]
    snap = conn.execute("INSERT INTO doc_snapshot (doc_source_id) VALUES (?) RETURNING snapshot_id",
                        [sid]).fetchone()[0]
    dsid = conn.execute("INSERT INTO doc_section (snapshot_id, content) VALUES (?, ?) "
                        "RETURNING doc_section_id", [snap, DOC_TEXT]).fetchone()[0]
    edges = []
    for rel, conf in (("CONTRADICTS", 0.1), ("CORROBORATES", 0.3), ("CORROBORATES", 0.9)):
        edges.append(conn.execute(
            "INSERT INTO alignment_edge (from_doc_section_id, to_chapter_id, concept_id, "
            "relation_type, confidence) VALUES (?, ?, ?, ?, ?) RETURNING alignment_edge_id",
            [dsid, chid, cid, rel, conf]).fetchone()[0])
    conn.close()
    return path, edges


def _answer(verdict, conf=0.8, book="commit offsets to ZooKeeper by default",
            doc="ZooKeeper has been removed entirely"):
    if verdict == "UNRELATED":
        book = doc = ""
    return {"verdict": verdict, "confidence": conf, "book_claim": book,
            "doc_claim": doc, "explanation": "x"}


def test_tally_majority_and_superseded_pooling():
    v = [rc.check_vote(_answer(x), {"chapter_excerpt": BOOK_TEXT, "doc_excerpt": DOC_TEXT})
         for x in ("SUPERSEDED", "SUPERSEDED", "CONTRADICTS")]
    d = rc.tally(v, 3)
    assert d["decision"] == "SUPERSEDED" and d["agree"] == 3
    assert d["confidence"] == pytest.approx(0.8)


def test_tally_needs_majority_of_planned_samples():
    pair = {"chapter_excerpt": BOOK_TEXT, "doc_excerpt": DOC_TEXT}
    one = [rc.check_vote(_answer("CONTRADICTS"), pair)]
    assert rc.tally(one, 3)["decision"] == "NO_MAJORITY"


def test_unverifiable_quote_demotes_vote():
    pair = {"chapter_excerpt": BOOK_TEXT, "doc_excerpt": DOC_TEXT}
    v = rc.check_vote(_answer("CONTRADICTS", doc="ZooKeeper is deprecated in 3.5"), pair)
    assert v["verdict"] == "UNRELATED" and v["claimed_verdict"] == "CONTRADICTS"


def test_prep_process_apply_round_trip(catalog, tmp_path):
    path, (contra, weak, strong) = catalog
    out = tmp_path / "out"
    assert rc.main(["prep", "--catalog", str(path), "--output-dir", str(out)]) == 0
    manifest = json.loads((out / "manifest.json").read_text())
    ids = [p["alignment_edge_id"] for p in manifest["pairs"]]
    assert ids == [contra, weak]          # strong CORROBORATES not re-judged
    prompt = Path(manifest["pairs"][0]["samples"][1]["prompt_path"]).read_text()
    assert prompt.index("BOOK:") < prompt.index("DOC:")   # sample 1 = book first

    answers = {contra: ["SUPERSEDED", "SUPERSEDED", "UNRELATED"],
               weak: ["UNRELATED", "UNRELATED", "CORROBORATES"]}
    for pair in manifest["pairs"]:
        for s, verdict in zip(pair["samples"], answers[pair["alignment_edge_id"]]):
            Path(s["result_path"]).write_text(json.dumps(_answer(verdict)))

    assert rc.main(["process", "--output-dir", str(out), "--catalog", str(path), "--apply"]) == 0
    conn = duckdb.connect(str(path))
    rows = dict((r[0], r[1:]) for r in conn.execute(
        "SELECT alignment_edge_id, relation_type, confidence, explanation FROM alignment_edge").fetchall())
    assert weak not in rows                     # judged unrelated → deleted
    assert rows[strong][0] == "CORROBORATES"   # untouched
    rel, conf, expl = rows[contra]
    assert rel == "CONTRADICTS" and conf == pytest.approx(0.533, abs=1e-3)
    assert expl.startswith("[superseded]") and "ZooKeeper has been removed" in expl
    assert conn.execute("SELECT COUNT(*) FROM alignment_vote").fetchone()[0] == 6


def test_batches_never_mix_sample_indexes_of_one_pair():
    pairs = []
    for eid in range(7):
        p = rc.PairEntry(eid, 1, "c", 1, 1, "CONTRADICTS", 0.1, "", "", "b", None, "d")
        p.samples = [{"prompt_path": f"{eid}_s{k}.txt", "result_path": f"{eid}_s{k}.json"}
                     for k in range(3)]
        pairs.append(p)
    batches = rc.batch_by_sample(pairs, 3, 4)
    assert sum(len(b) for b in batches) == 21
    for b in batches:
        assert len({j["prompt_path"].split("_s")[1] for j in b}) == 1
        assert len({j["prompt_path"].split("_s")[0] for j in b}) == len(b)


def _edge_world(tmp_path):
    root = Path(__file__).resolve().parent.parent
    conn = duckdb.connect(str(tmp_path / "c.ddb"))
    conn.execute((root / "schemas" / "catalog.sql").read_text())
    b = conn.execute("INSERT INTO book (title, source_path) VALUES ('B','/b') RETURNING book_id").fetchone()[0]
    ch = conn.execute("INSERT INTO chapter (book_id, title) VALUES (?, 'c') RETURNING chapter_id", [b]).fetchone()[0]
    src = conn.execute("INSERT INTO doc_source (name, source_type, mcp_server, identifier) "
                       "VALUES ('S','markdown','x','s') RETURNING doc_source_id").fetchone()[0]
    sn = conn.execute("INSERT INTO doc_snapshot (doc_source_id) VALUES (?) RETURNING snapshot_id", [src]).fetchone()[0]
    sec = conn.execute("INSERT INTO doc_section (snapshot_id, content) VALUES (?, 'x') "
                       "RETURNING doc_section_id", [sn]).fetchone()[0]
    cid = conn.execute("INSERT INTO concept (name) VALUES ('K') RETURNING concept_id").fetchone()[0]
    ids = [conn.execute("INSERT INTO alignment_edge (from_doc_section_id, to_chapter_id, concept_id, "
                        "relation_type, confidence) VALUES (?, ?, ?, 'CONTRADICTS', 0.1) "
                        "RETURNING alignment_edge_id", [sec, ch, cid]).fetchone()[0] for _ in range(2)]
    decided = [
        {"alignment_edge_id": ids[0], "current_relation": "CONTRADICTS", "votes": [],
         "decision": {"decision": "UNRELATED", "confidence": 0.9}},
        {"alignment_edge_id": ids[1], "current_relation": "CONTRADICTS", "votes": [],
         "decision": {"decision": "SUPERSEDED", "confidence": 0.8, "explanation": "dated"}},
    ]
    return conn, ids, decided


def test_apply_backs_up_then_deletes_unrelated(tmp_path):
    conn, ids, decided = _edge_world(tmp_path)
    stats = rc.apply_decisions(conn, decided, backup_table="alignment_edge_pre_revote")
    assert stats["deleted_unrelated"] == 1
    assert conn.execute("SELECT COUNT(*) FROM alignment_edge").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM alignment_edge_pre_revote").fetchone()[0] == 2


def test_apply_keep_unrelated_only_relabels(tmp_path):
    conn, ids, decided = _edge_world(tmp_path)
    stats = rc.apply_decisions(conn, decided, keep_unrelated=True)
    assert stats["kept_unrelated"] == 1
    assert conn.execute("SELECT COUNT(*) FROM alignment_edge").fetchone()[0] == 2
    expl = conn.execute("SELECT explanation FROM alignment_edge WHERE alignment_edge_id = ?",
                        [ids[1]]).fetchone()[0]
    assert expl.startswith("[superseded]")
