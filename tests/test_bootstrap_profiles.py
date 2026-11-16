"""Tests for Project Bootstrap category profiles (bootstrap_profiles.py)."""
from __future__ import annotations

import json
import subprocess
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
import bootstrap_profiles as bp  # noqa: E402
import bootstrap_runtime as br  # noqa: E402
import lexa_plugin  # noqa: E402
import project_bootstrap as pb  # noqa: E402


@pytest.fixture
def catalog(tmp_path):
    conn = duckdb.connect(str(tmp_path / "catalog.ddb"))
    conn.execute(SCHEMA_FILE.read_text())
    yield conn
    conn.close()


def _concept(conn, name, ctype="Tool", desc=""):
    return conn.execute(
        "INSERT INTO concept (name, concept_type, description) VALUES (?, ?, ?) "
        "RETURNING concept_id", [name, ctype, desc]).fetchone()[0]


def _resolver(name_to_id):
    r = MagicMock()
    r.resolve_lookup_only.side_effect = lambda n: name_to_id.get(n.strip().lower())
    return r


def _frontmatter(text: str) -> dict:
    head = text.split("---")[1]
    out = {}
    for line in head.strip().splitlines():
        if ":" in line and not line.startswith(" "):
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip().strip('"')
    return out


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("query,expected", [
    ("Databricks pipeline that ingests claims into Unity Catalog", "databricks"),
    ("Lakeflow declarative pipeline for IoT data", "databricks"),
    ("A claims-processing service tested with HealthSim cohorts", "healthsim"),
    ("Research assistant built on Lexa for variant interpretation", "lexa"),
    ("A Claude Code plugin with skills for code review", "claude-plugin"),
    ("An MCP App that shows a chart of query results", "mcp-app"),
    ("An MCP server exposing our ticket system", "mcp-server"),
    ("Rust async web server", None),
])
def test_detect_profile_from_keywords(query, expected):
    prof = bp.detect_profile(query)
    assert (prof.name if prof else None) == expected


def test_detect_profile_hint_wins_and_aliases():
    assert bp.detect_profile("Databricks job", "none") is None
    assert bp.detect_profile("anything", "dab").name == "databricks"
    assert bp.detect_profile("anything", "plugin").name == "claude-plugin"
    with pytest.raises(ValueError):
        bp.detect_profile("anything", "mainframe")


def test_plugin_about_databricks_is_a_plugin():
    assert bp.detect_profile("Claude Code plugin with skills for Databricks cost review").name \
        == "claude-plugin"


def test_profile_requests_pick_up_prose_concepts_and_drop_fragments(catalog):
    uc = _concept(catalog, "Unity Catalog", desc="Governance for Databricks data")
    unity = _concept(catalog, "Unity", desc="A game engine")
    claims = _concept(catalog, "Claims Data", "Concept", "Insurance claims records")
    for cid in (uc, unity, claims):
        catalog.execute("INSERT INTO concept_relation (from_concept_id, to_concept_id, "
                        "relation_type, source_type, source_id, confidence) "
                        "VALUES (?, ?, 'REQUIRES', 'chapter', ?, 0.9)", [cid, uc if cid != uc else claims, cid])
    names = {"unity catalog": uc, "unity": unity, "claims data": claims}
    d = pb.StackComposeDecomposer().decompose(
        catalog, _resolver(names), "Databricks pipeline that loads claims data into Unity Catalog",
        project_name="p")
    got = [el.name for el in d.elements]
    assert "Claims Data" in got and "Unity Catalog" in got
    assert "Unity" not in got


def test_plugin_with_mcp_app_stays_a_plugin():
    prof = bp.detect_profile("Claude Code plugin with an MCP App dashboard")
    assert prof.name == "claude-plugin"


# ---------------------------------------------------------------------------
# Databricks
# ---------------------------------------------------------------------------


def _generate(catalog, tmp_path, query, names=None, **kw):
    g = pb.make_project_bootstrap_generator()
    pid, report, issues = g.run_deterministic(
        catalog, _resolver(names or {}), query, output_root=str(tmp_path), **kw)
    assert pid > 0, issues
    return pid, Path(tmp_path) / kw.get("project_name", ""), issues


def test_databricks_profile_plans_bundle_layout(catalog):
    d = pb.StackComposeDecomposer().decompose(
        catalog, _resolver({}), "Databricks pipeline for member claims",
        project_name="claims-etl")
    paths = [pf.relative_path for pf in d.planned_files]
    assert d.stack.profile == "databricks"
    for p in ("databricks.yml", "resources/claims_etl_etl.pipeline.yml",
              "resources/claims_etl_job.job.yml", "src/claims_etl/main.py",
              "src/claims_etl_etl/transformations/bronze.py", "tests/test_logic.py"):
        assert p in paths
    # A Databricks bundle is not a plain Python scaffold.
    assert "src/main.py" not in paths


def test_databricks_anchor_concepts_join_the_stack(catalog):
    uc = _concept(catalog, "Unity Catalog", desc="Governance layer for Databricks data")
    d = pb.StackComposeDecomposer().decompose(
        catalog, _resolver({"unity catalog": uc}), "Databricks pipeline",
        project_name="p")
    assert [el.name for el in d.elements] == ["Unity Catalog"]
    assert any("anchored on Unity Catalog" in n for n in d.notes)
    # Missing anchors are not reported one by one.
    assert not any("Lakeflow" in n and "not found" in n for n in d.notes)


def test_databricks_package_is_valid_and_tests_pass_offline(catalog, tmp_path):
    yaml = pytest.importorskip("yaml")
    _, pkg, issues = _generate(catalog, tmp_path, "Databricks pipeline for claims",
                               project_name="claims-etl")
    assert not [i for i in issues if i.severity == "error"]
    bundle = yaml.safe_load((pkg / "databricks.yml").read_text())
    assert bundle["bundle"]["name"] == "claims_etl"
    assert bundle["include"] == ["resources/*.yml"]
    assert set(bundle["targets"]) == {"dev", "prod"}
    pipe = yaml.safe_load((pkg / "resources/claims_etl_etl.pipeline.yml").read_text())
    job = yaml.safe_load((pkg / "resources/claims_etl_job.job.yml").read_text())
    assert pipe["resources"]["pipelines"]["claims_etl_etl"]["serverless"] is True
    tasks = job["resources"]["jobs"]["claims_etl_job"]["tasks"]
    assert tasks[0]["pipeline_task"]["pipeline_id"] == "${resources.pipelines.claims_etl_etl.id}"
    assert tasks[1]["python_wheel_task"]["package_name"] == "claims_etl"
    assert "from pyspark import pipelines as dp" in (
        pkg / "src/claims_etl_etl/transformations/silver.py").read_text()

    # The shipped unit tests run without Spark or a workspace.
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                          cwd=pkg, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    # Request-specific files are listed for sub-agent dispatch; config is complete.
    pending = {p.relative_path for p in br.dispatch_manifest(pkg)}
    assert "src/claims_etl/logic.py" in pending
    assert "databricks.yml" not in pending
    prompt = next((pkg / "_sub_agent_prompts").glob("*logic*.txt")).read_text()
    assert "pyspark import pipelines as dp" in prompt


def test_databricks_runtime_detected_and_cli_optional(tmp_path):
    (tmp_path / "databricks.yml").write_text("bundle:\n  name: x\n")
    (tmp_path / "pyproject.toml").write_text("")
    assert br.detect_stack(tmp_path) == "databricks"
    calls = []

    def runner(cmd, cwd, timeout):
        calls.append(cmd)
        return 0, "ok"

    report = br.validate_project(
        tmp_path, runner=runner, which=lambda t: None if t == "databricks" else "/bin/" + t)
    steps = {s.name: s.status for s in report.steps}
    assert steps == {"placeholders": "pass", "setup": "pass", "test": "pass",
                     "smoke": "skipped"}
    assert report.passed
    assert ["databricks", "bundle", "validate"] not in calls


# ---------------------------------------------------------------------------
# HealthSim
# ---------------------------------------------------------------------------


def test_healthsim_profile_adds_plugin_and_reader_to_python(catalog, tmp_path):
    _, pkg, _ = _generate(catalog, tmp_path, "Claims adjudication service tested with HealthSim",
                          project_name="claims-svc")
    settings = json.loads((pkg / ".claude/settings.json").read_text())
    assert settings["enabledPlugins"] == {"healthsim@healthsim": True}
    assert settings["extraKnownMarketplaces"]["healthsim"]["source"]["repo"] == \
        "mark64oswald/healthsim-workspace"
    skill = (pkg / ".claude/skills/claims-svc-test-data/SKILL.md").read_text()
    fm = _frontmatter(skill)
    assert fm["name"] == "claims-svc-test-data"
    assert fm["disable-model-invocation"] == "true"
    assert not [p for p in agent_skills.validate(fm["name"], fm["description"],
                                                 folder="claims-svc-test-data")
                if not p.startswith("warning")]
    assert (pkg / "src/main.py").exists()          # python base stack is kept
    assert "duckdb" in (pkg / "pyproject.toml").read_text()
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                           "tests/test_healthsim_cohorts.py"],
                          cwd=pkg, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ---------------------------------------------------------------------------
# Lexa
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_installed_lexa(tmp_path_factory, monkeypatch):
    """Keep tests off any lexa-bioscience plugin installed on this machine."""
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))
    monkeypatch.delenv(lexa_plugin.ENV_VAR, raising=False)


def _fake_lexa(root: Path, version="9.9.0") -> Path:
    """A minimal lexa-bioscience plugin: three skills, two servers."""
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin/plugin.json").write_text(json.dumps(
        {"name": "lexa-bioscience", "version": version, "skills": "./skills/",
         "mcpServers": "./.mcp.json"}))
    for folder, name, desc in (
        ("genomics", "genomics-analysis",
         "Variant annotation and pathogenicity. Load it BEFORE calling clinvar or ensembl tools."),
        ("drug-discovery", "drug-discovery-analysis",
         "Drug targets and compounds. Load it BEFORE calling the chembl tools."),
        ("provenance", "data-provenance", "Record provenance. Mentions clinvar in passing."),
        ("education", "education-mode", "Teach concepts. Uses the learner tools."),
    ):
        (root / "skills" / folder).mkdir(parents=True)
        (root / "skills" / folder / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: \"{desc}\"\n---\n\n# {name}\n")
    (root / "runtime").mkdir()
    ops = {f"lexa:{srv}:{tool}": {"server": f"{srv}-mcp", "tool": tool, "parameters": [
        {"name": "query", "required": True}, {"name": "limit", "required": False}]}
        for srv, tool in (("clinvar", "search_variant"), ("clinvar", "batch_classify"),
                          ("chembl", "search_compound"), ("learner", "learner_state"))}
    (root / "runtime/operation-contract.json").write_text(json.dumps({"operations": ops}))
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {
        s: {"command": "python3"} for s in ("clinvar-mcp", "chembl-mcp", "learner-mcp",
                                             "ensembl-mcp")}}))
    return root


def test_lexa_plugin_is_found_in_claude_plugin_folders(tmp_path):
    home = tmp_path / "home"
    _fake_lexa(home / ".claude/plugins/cache/lexa/lexa-bioscience/0.1.0", "0.1.0")
    newer = _fake_lexa(home / ".claude/plugins/cache/lexa/lexa-bioscience/0.3.0", "0.3.0")
    assert lexa_plugin.find_plugin(home=home, env={}) == newer
    explicit = _fake_lexa(tmp_path / "checkout" / "lexa-bioscience")
    assert lexa_plugin.find_plugin(str(tmp_path / "checkout"), home=home, env={}) == explicit
    assert lexa_plugin.find_plugin(home=tmp_path / "nobody", env={}) is None


def test_lexa_capabilities_and_selection(tmp_path):
    caps = lexa_plugin.read_capabilities(_fake_lexa(tmp_path / "lx"))
    assert caps.version == "9.9.0"
    assert {s.folder for s in caps.skills} == {"genomics", "drug-discovery", "provenance",
                                               "education"}
    assert caps.servers == ["chembl-mcp", "clinvar-mcp", "ensembl-mcp", "learner-mcp"]
    t = caps.tools_for("clinvar-mcp")[1]
    assert t.qualified == "mcp__plugin_lexa-bioscience_clinvar-mcp__search_variant"
    assert t.params == ["query", "limit?"]
    sel = lexa_plugin.select(caps, "Agent that checks variant pathogenicity")
    assert [s.folder for s in sel.skills] == ["genomics", "provenance"]
    # Provenance's passing mention of clinvar doesn't count; genomics' does.
    assert sel.servers == ["clinvar-mcp", "ensembl-mcp"]
    tutor = lexa_plugin.select(caps, "A tutor for drug targets and compounds")
    assert [s.folder for s in tutor.skills][:2] == ["education", "drug-discovery"]
    assert "learner-mcp" in tutor.servers and "chembl-mcp" in tutor.servers


def test_lexa_profile_reads_the_installed_plugin(catalog, tmp_path):
    root = _fake_lexa(tmp_path / "lx")
    g = pb.make_project_bootstrap_generator()
    pid, report, _ = g.run_deterministic(
        catalog, _resolver({}), "Lexa agent that classifies variant pathogenicity",
        project_name="vc", output_root=str(tmp_path / "out"), plugin_path=str(root))
    pkg = tmp_path / "out" / "vc"
    claude_md = (pkg / "CLAUDE.md").read_text()
    assert "`/lexa-bioscience:genomics`" in claude_md
    assert "| `clinvar-mcp` | `batch_classify`, `search_variant` |" in claude_md
    assert "mcp__plugin_lexa-bioscience_clinvar-mcp__batch_classify" in claude_md
    skill = (pkg / ".claude/skills/vc-workflow/SKILL.md").read_text()
    assert "`/lexa-bioscience:genomics`" in skill and "`clinvar-mcp`" in skill
    assert bp.MARKER not in skill
    inv = json.loads((pkg / ".claude/lexa-capabilities.json").read_text())
    assert inv["version"] == "9.9.0" and inv["selected"]["skills"] == ["genomics", "provenance"]
    assert set(inv["mcp_servers"]) == {"chembl-mcp", "clinvar-mcp", "ensembl-mcp", "learner-mcp"}
    # The MCP servers come in through the enabled plugin.
    settings = json.loads((pkg / ".claude/settings.json").read_text())
    assert settings["enabledPlugins"] == {"lexa-bioscience@lexa": True}
    meta = json.loads(catalog.execute(
        "SELECT metadata_json FROM generated_package WHERE package_id = ?", [pid]
    ).fetchone()[0])
    assert meta["profile_data"]["plugin_version"] == "9.9.0"
    assert meta["profile_data"]["installed"] is True
    assert any("read plugin 9.9.0" in n for n in report.notes)


def test_lexa_profile_writes_agent_with_guard_rails(catalog, tmp_path):
    pid, pkg, _ = _generate(catalog, tmp_path, "Variant interpretation tutor built on Lexa",
                            project_name="variant-tutor")
    settings = json.loads((pkg / ".claude/settings.json").read_text())
    assert settings["enabledPlugins"] == {"lexa-bioscience@lexa": True}
    claude_md = (pkg / "CLAUDE.md").read_text()
    for rail in ("not a medical device", "contains_real_data: false", "education-domain-rules"):
        assert rail.lower() in claude_md.lower()
    fm = _frontmatter((pkg / ".claude/skills/variant-tutor-workflow/SKILL.md").read_text())
    assert fm["name"] == "variant-tutor-workflow"
    assert not (pkg / "pyproject.toml").exists()   # no language unless asked
    meta = json.loads(catalog.execute(
        "SELECT metadata_json FROM generated_package WHERE package_id = ?", [pid]
    ).fetchone()[0])
    assert meta["profile"] == "lexa"
    # No plugin installed: built-in snapshot, flagged in CLAUDE.md and metadata.
    assert meta["profile_data"]["installed"] is False
    assert "was not installed" in claude_md


def test_lexa_profile_accepts_a_stack(catalog):
    d = pb.StackComposeDecomposer().decompose(
        catalog, _resolver({}), "Lexa agent", stack="typescript", project_name="x")
    paths = {pf.relative_path for pf in d.planned_files}
    assert {"package.json", "CLAUDE.md", ".claude/settings.json"} <= paths


def test_routed_profile_falls_back_to_stack_in_generator(catalog):
    d = pb.StackComposeDecomposer().decompose(
        catalog, _resolver({}), "An MCP server in python", project_name="x")
    assert d.stack.profile is None
    assert d.stack.name == "python"
    assert any("own generator" in n for n in d.notes)
