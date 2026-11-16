"""Tests for the Open Data Product generators (2026-11).

odp_gen.py (shared ODCS + SDK helpers), odcs_contract_gen.py
(/kb-data-contract), odp_portfolio_gen.py (/kb-dp-portfolio),
odp_recipe_gen.py (/kb-dp-recipe) and odp_review_gen.py (/kb-dp-review),
against a tiny seeded catalog. ODCS output is checked against the vendored
JSON Schema when ``jsonschema`` is installed; ODPS/ODPC/ODPG/ODPR output is
checked with the open-data-products SDK when it's on PATH.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import duckdb
import pytest

yaml = pytest.importorskip("yaml")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FILE = PROJECT_ROOT / "schemas" / "catalog.sql"
KB_MCP = PROJECT_ROOT / "mcp-servers" / "kb-mcp"
if str(KB_MCP) not in sys.path:
    sys.path.insert(0, str(KB_MCP))

import odcs_contract_gen  # noqa: E402
import odp_gen as og  # noqa: E402

SDK = shutil.which("open-data-products")
needs_sdk = pytest.mark.skipif(SDK is None, reason="open-data-products SDK not installed")


@pytest.fixture
def catalog(tmp_path):
    conn = duckdb.connect(str(tmp_path / "catalog.ddb"))
    conn.execute(SCHEMA_FILE.read_text())
    yield conn
    conn.close()


@pytest.fixture
def corpus(catalog):
    cid = catalog.execute(
        "INSERT INTO concept (name, concept_type, description) VALUES ('Data Contract', "
        "'Concept', 'An agreement between a data producer and its consumers.') "
        "RETURNING concept_id").fetchone()[0]
    pid = catalog.execute(
        "INSERT INTO procedure (name, steps, failure_modes, source_type, source_id) VALUES "
        "('Define a data contract', '1. Name the owner.\n2. Agree the schema.\n3. Agree SLAs.', "
        "'Contracts nobody enforces drift from the data.', 'chapter', 1) "
        "RETURNING procedure_id").fetchone()[0]
    catalog.execute("INSERT INTO procedure_concept VALUES (?, ?)", [pid, cid])
    ids = {"data contract": cid}
    resolver = MagicMock()
    resolver.resolve_lookup_only.side_effect = lambda n: ids.get((n or "").strip().lower())
    return catalog, resolver


DDL = """
CREATE TABLE main.sales.orders (
  order_id BIGINT NOT NULL,
  customer_email STRING COMMENT 'buyer email',
  amount DECIMAL(10,2) NOT NULL,
  items ARRAY<STRUCT<sku: STRING, qty: INT>>,
  updated_at TIMESTAMP,
  PRIMARY KEY (order_id)
)"""


# ---------------------------------------------------------------------------
# odp_gen helpers
# ---------------------------------------------------------------------------


def test_parse_ddl():
    table, cols = og.parse_ddl(DDL)
    assert table == "main.sales.orders"
    by = {c.name: c for c in cols}
    assert list(by) == ["order_id", "customer_email", "amount", "items", "updated_at"]
    assert by["order_id"].primary_key and by["order_id"].required
    assert by["amount"].required and by["amount"].logical_type == "number"
    assert by["items"].physical_type.startswith("ARRAY<STRUCT<") and by["items"].logical_type == "array"
    assert by["customer_email"].personal and by["customer_email"].description == "buyer email"
    assert not by["amount"].personal


def test_parse_column_specs():
    cols = og.parse_columns(["id bigint pk", "email: string required -- login", "note"])
    assert [(c.name, c.logical_type, c.primary_key, c.required) for c in cols] == [
        ("id", "integer", True, True), ("email", "string", False, True), ("note", "string", False, False)]
    assert cols[1].description == "login"


@pytest.mark.parametrize("platform", ["databricks", "snowflake", "bigquery", "postgres", "api", "generic"])
def test_contract_is_schema_valid_for_every_platform(platform):
    pytest.importorskip("jsonschema")
    _, cols = og.parse_ddl(DDL)
    doc = og.odcs_contract(contract_id="orders", name="orders", table="orders", columns=cols,
                           purpose="Orders.", platform=platform, location="main.sales.orders")
    assert og.check_odcs(doc, "x").ok is True


def test_detect_platform():
    assert og.detect_platform("orders in Unity Catalog") == "databricks"
    assert og.detect_platform("a Snowflake share") == "snowflake"
    assert og.detect_platform("just a table") == "generic"


# ---------------------------------------------------------------------------
# /kb-data-contract
# ---------------------------------------------------------------------------


def test_data_contract_from_ddl(corpus, tmp_path):
    conn, resolver = corpus
    gen = odcs_contract_gen.make_data_contract_generator()
    pid, report, issues = gen.run_deterministic(
        conn, resolver, "Orders placed in the web shop, on Databricks", ddl=DDL,
        output_root=str(tmp_path))
    assert pid != -1, issues
    root = tmp_path / "orders"
    doc = yaml.safe_load((root / "orders.odcs.yaml").read_text())
    assert doc["apiVersion"] == "v3.1.0" and doc["kind"] == "DataContract"
    assert doc["servers"][0] == {"server": "production", "environment": "prod",
                                 "type": "databricks", "host": "TODO.cloud.databricks.com",
                                 "catalog": "main", "schema": "sales"}
    props = {p["name"]: p for p in doc["schema"][0]["properties"]}
    assert props["customer_email"]["classification"] == "restricted"
    assert props["amount"]["quality"][0]["metric"] == "nullValues"
    metrics = {q["metric"] for q in doc["schema"][0]["quality"]}
    assert metrics == {"rowCount", "duplicateValues"}
    latency = next(s for s in doc["slaProperties"] if s["property"] == "latency")
    assert latency["element"] == "orders.updated_at"
    assert any("customer_email" in i.message for i in issues)            # restricted warning
    checklist = (root / "checklist.md").read_text()
    assert "Define a data contract" in checklist and "drift" in checklist
    assert "TODO" in (root / "_prompts/fill-contract.md").read_text()
    if og.check_odcs(doc, "x").ok is not None:
        assert "passes" in (root / "_validation.md").read_text()


def test_data_contract_without_columns_warns(corpus, tmp_path):
    conn, resolver = corpus
    pid, report, _ = odcs_contract_gen.make_data_contract_generator().run_deterministic(
        conn, resolver, "customer_master", output_root=str(tmp_path))
    assert pid != -1
    assert any("placeholder" in n for n in report.notes)
    doc = yaml.safe_load((tmp_path / "customer-master" / "customer-master.odcs.yaml").read_text())
    assert doc["servers"][0]["type"] == "local"


@needs_sdk
def test_data_contract_wraps_into_a_valid_odps(corpus, tmp_path):
    conn, resolver = corpus
    odcs_contract_gen.make_data_contract_generator().run_deterministic(
        conn, resolver, "Orders on Databricks", ddl=DDL, output_root=str(tmp_path))
    root = tmp_path / "orders"
    proc = subprocess.run([SDK, "product", "wrap-contract", "--input", "orders.odcs.yaml",
                           "--output", "orders.odps.yaml"], cwd=root, capture_output=True,
                          text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ---------------------------------------------------------------------------
# Bootstrap data-product profile: its ODCS file now passes the schema too
# ---------------------------------------------------------------------------


def test_bootstrap_contract_passes_odcs_schema():
    pytest.importorskip("jsonschema")
    from types import SimpleNamespace
    import odp_profile
    for platform in ("databricks", "snowflake", "api", "generic"):
        ctx = SimpleNamespace(project_name="churn", slug="churn", pkg="churn", purpose="",
                              ctx_summary="", request="Churn scores.", data={"platform": platform})
        doc = yaml.safe_load(odp_profile.odcs_contract(ctx))
        assert og.check_odcs(doc, platform).ok is True, platform


# ---------------------------------------------------------------------------
# /kb-dp-portfolio
# ---------------------------------------------------------------------------

import odp_portfolio_gen  # noqa: E402
import odp_recipe_gen  # noqa: E402
import odp_review_gen  # noqa: E402

PRODUCTS = ["Customer churn scores: weekly churn risk per customer",
            "Customer 360: one profile per customer"]
USE_CASES = ["Retention campaigns: target at-risk customers => Customer churn scores, Customer 360",
             "Service triage: prioritise tickets => Customer 360"]
OBJECTIVES = ["Reduce churn: keep paying customers => Retention campaigns",
              "Faster support => Service triage, Unknown thing"]


def test_parse_items_and_links():
    items = odp_portfolio_gen.parse_items(["A: first => X, Y", "B", ": no name"])
    assert [(i.name, i.description, i.links) for i in items] == [
        ("A", "first", ["X", "Y"]), ("B", "", [])]


def _portfolio(corpus, tmp_path, **kw):
    conn, resolver = corpus
    pid, report, issues = odp_portfolio_gen.make_dp_portfolio_generator().run_deterministic(
        conn, resolver, "Customer retention on Databricks", products=PRODUCTS,
        output_root=str(tmp_path), **kw)
    assert pid != -1, issues
    return tmp_path / "customer-retention-on-databricks", report


def test_portfolio_graph_follows_the_links(corpus, tmp_path):
    root, report = _portfolio(corpus, tmp_path, use_cases=USE_CASES, objectives=OBJECTIVES)
    assert any("Unknown thing" in n for n in report.notes)
    graph = yaml.safe_load((root / "graph/customer-retention-on-databricks.odpg.yaml")
                           .read_text())["graph"]
    edges = {(e["from"], e["type"], e["to"]) for e in graph["edges"]}
    assert ("UC-SERVICE-TRIAGE", "uses", "DP-CUSTOMER-360") in edges
    assert ("UC-SERVICE-TRIAGE", "uses", "DP-CUSTOMER-CHURN-SCORES") not in edges
    assert ("UC-RETENTION-CAMPAIGNS", "supports", "OBJ-REDUCE-CHURN") in edges
    # Derived: churn scores feed retention campaigns, which support Reduce churn.
    assert ("DP-CUSTOMER-CHURN-SCORES", "contributesTo", "OBJ-REDUCE-CHURN") in edges
    assert ("DP-CUSTOMER-CHURN-SCORES", "contributesTo", "OBJ-FASTER-SUPPORT") not in edges
    assert ("KPI-REDUCE-CHURN", "measures", "OBJ-REDUCE-CHURN") in edges
    # Governance: each product is governed by the portfolio policy and its contract.
    for dp, slug in (("DP-CUSTOMER-360", "customer-360"),
                     ("DP-CUSTOMER-CHURN-SCORES", "customer-churn-scores")):
        assert (dp, "governedBy", "POL-CUSTOMER-RETENTION-ON-DATABRICKS") in edges
        assert (dp, "governedBy", f"POL-{slug.upper()}-CONTRACT") in edges
    policy = yaml.safe_load((root / "governance/customer-retention-on-databricks.policy.yaml")
                            .read_text())
    assert {r["id"] for r in policy["rules"]} >= {"ownership", "contract-first"}
    node_ids = {n["id"] for n in graph["nodes"]}
    assert all(f in node_ids and t in node_ids for f, _, t in edges)
    for n in graph["nodes"]:
        assert (root / "graph" / n["$ref"].split("#")[0]).resolve().is_file(), n
    odps = yaml.safe_load((root / "products/customer-churn-scores.odps.yaml").read_text())
    assert odps["product"]["productStrategy"]["contributesToKPI"]["id"] == "kpi-reduce-churn"
    assert odps["product"]["dataAccess"]["default"]["outputPortType"] == "Delta Sharing"


def test_portfolio_defaults_without_use_cases(corpus, tmp_path):
    root, _ = _portfolio(corpus, tmp_path)
    frags = sorted(p.name for p in (root / "catalog/fragments").iterdir())
    assert "use_case_customer-360-use-case.yaml" in frags
    assert len([f for f in frags if f.startswith("business_objective_")]) == 1


def test_portfolio_needs_products(corpus, tmp_path):
    conn, resolver = corpus
    pid, _, issues = odp_portfolio_gen.make_dp_portfolio_generator().run_deterministic(
        conn, resolver, "empty", products=[], output_root=str(tmp_path))
    assert pid == -1 and "product" in issues[0].message


@needs_sdk
def test_portfolio_passes_the_sdk(corpus, tmp_path):
    root, _ = _portfolio(corpus, tmp_path, use_cases=USE_CASES, objectives=OBJECTIVES)
    assert (root / "catalog/customer-retention-on-databricks.odpc.yaml").is_file()
    assert "FAILS" not in (root / "_validation.md").read_text()
    analysis = subprocess.run([SDK, "odpg-analyze", "graph/customer-retention-on-databricks.odpg.yaml",
                               "--json"], cwd=root, capture_output=True, text=True, timeout=120)
    assert analysis.returncode == 0, analysis.stderr
    assert __import__("json").loads(analysis.stdout)["analysis"]["ungovernedAssets"] == []
    proc = subprocess.run(["make", "all", f"ODP={SDK}"], cwd=root, capture_output=True,
                          text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ---------------------------------------------------------------------------
# /kb-dp-recipe
# ---------------------------------------------------------------------------


def _product_file(corpus, tmp_path) -> Path:
    root, _ = _portfolio(corpus, tmp_path / "pf", use_cases=USE_CASES, objectives=OBJECTIVES)
    return root / "products/customer-churn-scores.odps.yaml"


def test_recipe_from_an_odps_file(corpus, tmp_path):
    conn, resolver = corpus
    product = _product_file(corpus, tmp_path)
    pid, report, issues = odp_recipe_gen.make_dp_recipe_generator().run_deterministic(
        conn, resolver, "churn scores", odps_path=str(product),
        graph_file="graph/retention.odpg.yaml", output_root=str(tmp_path / "out"))
    assert pid != -1, issues
    root = tmp_path / "out" / "customer-churn-scores-recipes"
    ci = yaml.safe_load((root / "recipes/customer-churn-scores-ci.recipe.yaml").read_text())
    agent = yaml.safe_load((root / "recipes/customer-churn-scores-agent.recipe.yaml").read_text())
    assert ci["recipe"]["type"] == "ci" and agent["recipe"]["type"] == "agent"
    assert ci["recipe"]["steps"][0]["with"]["document"] == "products/customer-churn-scores.odps.yaml"
    assert [s["command"] for s in agent["recipe"]["steps"]] == ["validate", "explain", "odpg.render"]
    assert agent["recipe"]["review"]["mode"] == "both"
    assert "Delta Sharing" in " ".join(agent["recipe"]["x-mypub-buildPlan"])
    handoff = (root / "handoff/README.md").read_text()
    for h in ("Recipe readme", "Delivery plan", "Contract plan", "AI agent brief"):
        assert f"## {h}" in handoff
    assert not (root / "products").exists()          # read, not rewritten


def test_recipe_ci_only_without_a_file(corpus, tmp_path):
    conn, resolver = corpus
    pid, report, _ = odp_recipe_gen.make_dp_recipe_generator().run_deterministic(
        conn, resolver, "Supplier risk scores behind a REST API", mode="ci",
        output_root=str(tmp_path))
    assert pid != -1
    root = tmp_path / "supplier-risk-scores-behind-a-rest-api-recipes"
    names = {p.name for p in (root / "recipes").iterdir()}
    assert names == {"supplier-risk-scores-behind-a-rest-api-ci.recipe.yaml"}
    assert any("wrote a draft" in n for n in report.notes)


@needs_sdk
def test_recipes_pass_the_sdk(corpus, tmp_path):
    conn, resolver = corpus
    product = _product_file(corpus, tmp_path)
    odp_recipe_gen.make_dp_recipe_generator().run_deterministic(
        conn, resolver, "x", odps_path=str(product), output_root=str(tmp_path / "out"))
    text = (tmp_path / "out/customer-churn-scores-recipes/_validation.md").read_text()
    assert text.count("passes") == 4 and "FAILS" not in text


# ---------------------------------------------------------------------------
# /kb-dp-review
# ---------------------------------------------------------------------------


def test_review_grades_sections():
    full = {"name": "X", "productID": "x", "visibility": "public", "status": "production",
            "type": "dataset", "description": "d", "valueProposition": "v",
            "productVersion": "1.0.0", "tags": ["a"]}
    doc = {"schema": "https://opendataproducts.org/v4.2/schema/odps.json", "version": "4.2",
           "product": {"details": {"en": full},
                       "SLA": {"declarative": {"default": {"dimensions": [
                           {"dimension": "uptime", "objective": 0, "unit": "percent"}]}}},
                       "license": {"scope": {"definition": "TODO: decide"}}}}
    scores, findings = odp_review_gen.review_product(doc)
    assert scores["details"] == 2 and scores["SLA"] == 1 and scores["license"] == 1
    assert scores["contract"] == 0
    by = {f.section: f for f in findings}
    assert by["contract"].severity == "error" and by["pricingPlans"].severity == "advice"
    assert "zero target" in by["SLA"].message and "placeholder" in by["license"].message
    letter, pct = odp_review_gen.grade(scores)
    assert letter == "F" and 0 < pct < 0.4


def test_review_package(corpus, tmp_path):
    conn, resolver = corpus
    product = _product_file(corpus, tmp_path)
    pid, _, issues = odp_review_gen.make_dp_review_generator().run_deterministic(
        conn, resolver, str(product), odps_path=str(product), output_root=str(tmp_path / "out"))
    assert pid != -1, issues
    root = tmp_path / "out" / "review-customer-churn-scores"
    review = (root / "review.md").read_text()
    assert review.startswith("# Review: Customer Churn Scores") and "**Grade" in review
    data = __import__("json").loads((root / "findings.json").read_text())
    assert data["sections"]["dataHolder"] == 0
    assert any(f["section"] == "productStrategy" for f in data["findings"])   # zero KPI target
    # The product's contractURL resolved to the generated contract, which passes ODCS.
    assert any(c["target"] == "customer-churn-scores.odcs.yaml" for c in data["checks"])


def test_review_unreadable_file(corpus, tmp_path):
    conn, resolver = corpus
    pid, _, issues = odp_review_gen.make_dp_review_generator().run_deterministic(
        conn, resolver, str(tmp_path / "missing.yaml"), output_root=str(tmp_path))
    assert pid == -1 and "can't read" in issues[0].message


# ---------------------------------------------------------------------------
# Library grounding: ODP books and procedure filtering
# ---------------------------------------------------------------------------


def _add_book(conn, title, chapters):
    bid = conn.execute("INSERT INTO book (title, source_path) VALUES (?, ?) RETURNING book_id",
                       [title, f"/x/{title}.epub"]).fetchone()[0]
    for i, (ct, body) in enumerate(chapters, 1):
        conn.execute("INSERT INTO chapter (book_id, chapter_num, title, content) VALUES (?,?,?,?)",
                     [bid, i, ct, body])
    return bid


def test_book_citations_rank_and_skip_unrelated(catalog):
    _add_book(catalog, "Data Product Portfolios", [
        ("Preface", "Thanks to everyone."),
        ("Portfolio thinking", "A portfolio of data products, each tied to a use case."),
        ("KPIs", "Every objective needs a KPI.")])
    _add_book(catalog, "Some Other Book", [("Portfolio", "portfolio portfolio")])
    cites = og.book_citations(catalog, og.ODP_BOOKS, keywords=("portfolio", "kpi"), per_book=5)
    labels = [c.label for c in cites]
    assert labels == ["Data Product Portfolios — Portfolio thinking",
                      "Data Product Portfolios — KPIs"]
    assert all(c.source_type == "chapter" for c in cites)


def test_relevant_procedures_drops_off_topic():
    from types import SimpleNamespace as NS
    procs = [NS(name="Troubleshoot Dynamics 365 authentication"),
             NS(name="Create a Delta Sharing recipient"),
             NS(name="Grant table permissions in Unity Catalog")]
    kept = og.relevant_procedures(procs, ("Delta Sharing", "Unity Catalog") + ("grant",))
    assert [p.name for p in kept] == ["Create a Delta Sharing recipient",
                                      "Grant table permissions in Unity Catalog"]


def test_portfolio_cites_the_book(corpus, tmp_path):
    conn, _ = corpus
    _add_book(conn, "Data Product Portfolios", [("Use cases first", "Start from the use case.")])
    root, _ = _portfolio(corpus, tmp_path)
    assert "Data Product Portfolios — Use cases first" in (root / "guidance.md").read_text()


def test_relevant_procedures_drops_near_duplicates():
    from types import SimpleNamespace as NS
    procs = [NS(name="Add a streaming table to a share in Catalog Explorer"),
             NS(name="Add a managed Iceberg table to a share in Catalog Explorer"),
             NS(name="Publish pipeline tables to a Unity Catalog schema")]
    kept = og.relevant_procedures(procs, ("share", "table", "publish"))
    assert [p.name for p in kept] == [procs[0].name, procs[2].name]
