"""Tests for scripts/prose_pass.py — grounded prose layer for Dialog/Tutorial."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "mcp-servers" / "kb-mcp"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import dialog as dlg  # noqa: E402
import prose_pass as pp  # noqa: E402


def _build_dialog(tmp_path):
    db = tmp_path / "catalog.ddb"
    conn = duckdb.connect(str(db))
    conn.execute((PROJECT_ROOT / "schemas" / "catalog.sql").read_text())
    q = lambda sql, a=(): conn.execute(sql, list(a)).fetchone()[0]  # noqa: E731
    sm = q("INSERT INTO concept (name, concept_type) VALUES ('Service Mesh', 'Pattern') RETURNING concept_id")
    cb = q("INSERT INTO concept (name, concept_type) VALUES ('Circuit Breaker', 'Pattern') RETURNING concept_id")
    istio = q("INSERT INTO concept (name, concept_type) VALUES ('Istio', 'Tool') RETURNING concept_id")
    bid = q("INSERT INTO book (title, source_path) VALUES ('Mesh Book', '/m.epub') RETURNING book_id")
    ch = q("INSERT INTO chapter (book_id, title, content) VALUES (?, 'Istio', "
           "'Istio injects an Envoy sidecar into every pod.') RETURNING chapter_id", [bid])
    rel = "INSERT INTO concept_relation (from_concept_id, to_concept_id, relation_type, source_type, source_id, confidence) VALUES (?, ?, ?, 'chapter', ?, 0.9)"
    for a, b, t, s in ((cb, cb, "IMPLEMENTS", 101), (cb, cb, "IMPLEMENTS", 102),
                       (istio, istio, "CITES", ch), (sm, cb, "CITES", 103), (sm, istio, "CITES", 104)):
        conn.execute(rel, [a, b, t, s])
    res = MagicMock()
    res.resolve_lookup_only.side_effect = lambda n: {"service mesh": sm}.get(n.lower())
    root = tmp_path / "pkgs"
    pid, _, _ = dlg.make_dialog_generator().run_deterministic(conn, res, "Service Mesh",
                                                             output_root=str(root))
    assert pid > 0
    conn.close()
    return db, root, ch


def test_prep_then_process_replaces_dialogue_and_keeps_backup(tmp_path):
    db, root, ch = _build_dialog(tmp_path)
    out = tmp_path / "prose"
    assert pp.main(["prep", "--catalog", str(db), "--generator", "dialog",
                    "--package", "service-mesh", "--package-root", str(root),
                    "--output-dir", str(out)]) == 0
    prompt = (out / "prompt.txt").read_text()
    assert f"[ch:{ch}]" in prompt and "Envoy sidecar" in prompt
    manifest = json.loads((out / "manifest.json").read_text())
    assert f"ch:{ch}" in manifest["allowed_citations"]

    draft = (root / "service-mesh" / "dialogue.md").read_text()
    # A result that cites an id it wasn't given is rejected.
    (out / "result.md").write_text(draft + "\nIstio is great [ch:999999].\n")
    assert pp.main(["process", "--output-dir", str(out)]) == 1

    revised = draft + f"\n**Practitioner:** Istio puts an Envoy sidecar in every pod [ch:{ch}].\n"
    (out / "result.md").write_text(revised)
    assert pp.main(["process", "--output-dir", str(out)]) == 0
    assert "Envoy sidecar" in (root / "service-mesh" / "dialogue.md").read_text()
    assert (root / "service-mesh" / "_deterministic" / "dialogue.md").read_text() == draft


def test_check_result_flags_dropped_heading():
    manifest = {"headings": ["# Dialogue: X", "## Beat 1: Istio"], "allowed_citations": []}
    assert pp.check_result("# Dialogue: X\nno beats", manifest) == ["missing heading: ## Beat 1: Istio"]
