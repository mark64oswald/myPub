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


def _seed_package(tmp_path, generator, units, files):
    """Catalog with one concept + chapter, a package of `units`, and its files on disk."""
    db = tmp_path / "catalog.ddb"
    conn = duckdb.connect(str(db))
    conn.execute((PROJECT_ROOT / "schemas" / "catalog.sql").read_text())
    q = lambda sql, a=(): conn.execute(sql, list(a)).fetchone()[0]  # noqa: E731
    cid = q("INSERT INTO concept (name, concept_type) VALUES ('Istio', 'Tool') RETURNING concept_id")
    bid = q("INSERT INTO book (title, source_path) VALUES ('Mesh Book', '/m.epub') RETURNING book_id")
    ch = q("INSERT INTO chapter (book_id, title, content) VALUES (?, 'Istio', "
           "'Istio injects an Envoy sidecar into every pod.') RETURNING chapter_id", [bid])
    conn.execute("INSERT INTO concept_relation (from_concept_id, to_concept_id, relation_type, "
                 "source_type, source_id, confidence) VALUES (?, ?, 'CITES', 'chapter', ?, 0.9)",
                 [cid, cid, ch])
    pid = q("INSERT INTO generated_package (generator_type, name) VALUES (?, 'pkg') "
            "RETURNING package_id", [generator])
    for unit_type, name, ordinal, meta in units:
        meta = {k: (cid if v == "CID" else [cid] if v == ["CID"] else v) for k, v in meta.items()}
        conn.execute("INSERT INTO generated_unit (package_id, unit_type, name, ordinal, metadata_json) "
                     "VALUES (?, ?, ?, ?, ?)", [pid, unit_type, name, ordinal, json.dumps(meta)])
    conn.close()
    root = tmp_path / "pkgs"
    for rel, text in files.items():
        (root / "pkg" / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / "pkg" / rel).write_text(text)
    return db, root, ch


def test_slide_deck_prose_uses_slide_headings(tmp_path):
    notes = "# Mesh — Speaker Notes\n\n## Slide 1 — Istio\n\n_(no notes)_\n"
    db, root, ch = _seed_package(tmp_path, "slide_deck",
                                 [("slide", "Istio", 1, {"concept_ids": ["CID"]})],
                                 {"speaker-notes.md": notes})
    out = tmp_path / "prose"
    assert pp.main(["prep", "--catalog", str(db), "--generator", "slide_deck", "--package", "pkg",
                    "--package-root", str(root), "--output-dir", str(out)]) == 0
    prompt = (out / "prompt.txt").read_text()
    assert "--- Slide 1 — Istio ---" in prompt and f"[ch:{ch}]" in prompt
    (out / "result.md").write_text(notes.replace("_(no notes)_", f"Istio adds a sidecar [ch:{ch}]."))
    assert pp.main(["process", "--output-dir", str(out)]) == 0
    assert "sidecar" in (root / "pkg" / "speaker-notes.md").read_text()
    assert (root / "pkg" / "_deterministic" / "speaker-notes.md").read_text() == notes


def test_content_brief_prose_splits_back_into_section_files(tmp_path):
    s1 = "# 1. Why a mesh\n\n**Thesis:** Meshes move retries out of code.\n"
    s2 = "# 2. Costs\n\n**Thesis:** Sidecars cost latency.\n"
    db, root, ch = _seed_package(
        tmp_path, "content_brief",
        [("content_section", "Why a mesh", 1, {"anchor_concept_id": "CID"}),
         ("content_section", "Costs", 2, {"anchor_concept_id": None})],
        {"sections/1-why-a-mesh.md": s1, "sections/2-costs.md": s2, "_brief.md": "# brief\n"})
    out = tmp_path / "prose"
    assert pp.main(["prep", "--catalog", str(db), "--generator", "content_brief", "--package", "pkg",
                    "--package-root", str(root), "--output-dir", str(out)]) == 0
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["target_files"] == ["sections/1-why-a-mesh.md", "sections/2-costs.md"]
    prompt = (out / "prompt.txt").read_text()
    assert "<!-- file: sections/2-costs.md -->" in prompt and "--- 1. Why a mesh ---" in prompt

    # Dropping a file marker is rejected.
    (out / "result.md").write_text("<!-- file: sections/1-why-a-mesh.md -->\n" + s1 + s2)
    assert pp.main(["process", "--output-dir", str(out)]) == 1

    result = ("<!-- file: sections/1-why-a-mesh.md -->\n" + s1
              + f"\n## Draft\n\nIstio injects a sidecar [ch:{ch}].\n"
              + "<!-- file: sections/2-costs.md -->\n" + s2)
    (out / "result.md").write_text(result)
    assert pp.main(["process", "--output-dir", str(out)]) == 0
    one = (root / "pkg" / "sections" / "1-why-a-mesh.md").read_text()
    assert "## Draft" in one and "<!-- file" not in one and "Costs" not in one
    assert (root / "pkg" / "sections" / "2-costs.md").read_text() == s2
    assert (root / "pkg" / "_deterministic" / "sections" / "1-why-a-mesh.md").read_text() == s1
    assert (root / "pkg" / "_brief.md").read_text() == "# brief\n"
