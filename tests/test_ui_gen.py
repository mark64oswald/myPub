"""Tests for /kb-ui (2026-11): ui_spec rules, ui_inputs adapters, ui_emit code and
the ui_gen generator, against a tiny product package and an empty catalog.

The generated TypeScript is checked structurally here; ``setup.sh --build``
(Node + network) is the real type-check and is run by hand on the Mac.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import duckdb
import pytest

pytest.importorskip("yaml")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FILE = PROJECT_ROOT / "schemas" / "catalog.sql"
KB_MCP = PROJECT_ROOT / "mcp-servers" / "kb-mcp"
if str(KB_MCP) not in sys.path:
    sys.path.insert(0, str(KB_MCP))

import ui_emit as ue  # noqa: E402
import ui_gen  # noqa: E402
import ui_inputs as ui  # noqa: E402
import ui_spec as us  # noqa: E402

PRODUCT = """\
schema: https://opendataproducts.org/v4.2/schema/odps.json
version: '4.2'
product:
  details:
    en:
      productID: claims
      name: Claims
      description: the claim lifecycle from submission to payment
      tags: [claim]
  productStrategy:
    productKPIs:
    - name: Collection Rate Numerator
      calculation: SUM(total_paid_amount) over claim.claim (metric view claim)
      unit: percentage
    - name: Collection Rate Denominator
      calculation: SUM(total_billed_amount) over claim.claim (metric view claim)
      unit: percentage
    - name: Avg Billed Amount Per Claim
      calculation: AVG(total_billed_amount) over claim.claim (metric view claim)
      unit: currency
    - name: Active Consuming Teams
      calculation: count of teams
"""

CONTRACT = """\
apiVersion: v3.1.0
kind: DataContract
id: claims
name: Claims
version: 0.1.0
status: draft
domain: claim
description:
  purpose: claims
schema:
- name: claim
  physicalName: demo.claim.claim
  description: One row per claim submitted to a payer.
  properties:
  - {name: claim_id, logicalType: integer, physicalType: bigint, primaryKey: true, required: true}
  - {name: claim_number, logicalType: string, physicalType: string}
  - {name: claim_status, logicalType: string, physicalType: string,
     description: "Claim status (e.g., submitted, paid, denied)"}
  - {name: total_billed_amount, logicalType: number, physicalType: decimal(12,2)}
  - {name: service_from_date, logicalType: date, physicalType: date}
  - {name: patient_medical_record_number, logicalType: string, physicalType: string}
  - {name: payer_id, logicalType: integer, physicalType: bigint,
     description: Foreign key linking to payer.payer}
  - {name: created_timestamp, logicalType: timestamp, physicalType: timestamp}
"""

METRICS = """\
CREATE OR REPLACE VIEW `demo`.`_metrics`.`claim`
WITH METRICS
LANGUAGE YAML
AS $$
  version: 1.1
  source: demo.claim.claim
  dimensions:
    - name: claim_status
      expr: claim_status
      comment: "Status (e.g., submitted, paid, denied)"
    - name: service_month
      expr: DATE_TRUNC('MONTH', service_from_date)
  measures:
    - name: total_billed_amount
      expr: SUM(total_billed_amount)
    - name: total_paid_amount
      expr: SUM(total_paid_amount)
    - name: avg_billed_amount_per_claim
      expr: AVG(total_billed_amount)
$$;
"""

DDL = """
CREATE TABLE shop.orders (
  order_id BIGINT NOT NULL,
  order_number STRING NOT NULL,
  customer_email STRING,
  status STRING COMMENT 'Order status (e.g., open, shipped, cancelled)',
  region STRING COMMENT 'Sales region (e.g., North, South, East, West, Central)',
  amount DECIMAL(10,2) NOT NULL,
  ordered_on DATE,
  notes STRING,
  is_gift BOOLEAN,
  PRIMARY KEY (order_id)
);
"""


@pytest.fixture
def catalog(tmp_path):
    conn = duckdb.connect(str(tmp_path / "catalog.ddb"))
    conn.execute(SCHEMA_FILE.read_text())
    resolver = MagicMock()
    resolver.resolve_lookup_only.return_value = None
    yield conn, resolver
    conn.close()


@pytest.fixture
def product_dir(tmp_path):
    root = tmp_path / "pkg"
    for sub, name, text in (("products", "claims.odps.yaml", PRODUCT),
                            ("contracts", "claims.odcs.yaml", CONTRACT),
                            ("metrics", "claims.sql", METRICS)):
        (root / sub).mkdir(parents=True)
        (root / sub / name).write_text(text)
    return root


# ---------------------------------------------------------------------------
# Rules and adapters
# ---------------------------------------------------------------------------


def test_field_rules():
    e = ui.entities_from_ddl(DDL)[0]
    kinds = {f.name: f.kind for f in e.fields}
    assert kinds["order_id"] == "id" and kinds["amount"] == "currency"
    assert kinds["status"] == "enum" and kinds["is_gift"] == "boolean"
    assert kinds["ordered_on"] == "date"
    status = next(f for f in e.fields if f.name == "status")
    assert status.enum_values == ["open", "shipped", "cancelled"]
    assert next(f for f in e.fields if f.name == "customer_email").restricted
    assert us.control_for(status)[0] == "Radio Group"
    region = next(f for f in e.fields if f.name == "region")
    assert us.control_for(region)[0] == "Select"


def test_table_columns_skip_restricted_fk_and_audit(product_dir):
    prod = ui.load_product(product_dir)
    claim = prod.entities[0]
    cols = [f.name for f in us.table_fields(claim)]
    assert cols[0] == "claim_id" and "claim_number" in cols and "claim_status" in cols
    for gone in ("patient_medical_record_number", "payer_id", "created_timestamp"):
        assert gone not in cols
    assert next(f for f in claim.fields if f.name == "patient_medical_record_number").restricted


def test_kpis_pair_ratios_and_drop_adoption(product_dir):
    prod = ui.load_product(product_dir)
    kpis = ui.kpi_metrics(prod.kpis, prod.metrics)
    names = [m.name for m in kpis]
    assert names[0] == "collection_rate" and kpis[0].format == "percent"
    assert kpis[0].numerator == "total_paid_amount"
    assert "avg_billed_amount_per_claim" in names
    assert not any("consuming" in n for n in names)


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------


def _run(catalog, tmp_path, **kw):
    conn, resolver = catalog
    pid, report, issues = ui_gen.make_ui_generator().run_deterministic(
        conn, resolver, kw.pop("request", "claims app"), output_root=str(tmp_path / "out"), **kw)
    assert pid != -1, [i.message for i in issues]
    assert not [i for i in issues if i.severity == "error"]
    return tmp_path / "out" / kw["app_name"], issues


@pytest.mark.parametrize("stack,base", [("vite", "base"), ("nextjs", "base"), ("vite", "radix"),
                                        ("nextjs", "radix")])
def test_data_product_package(catalog, tmp_path, product_dir, stack, base):
    root, issues = _run(catalog, tmp_path, product_path=str(product_dir), stack=stack, base=base,
                        app_name=f"claims-{stack}-{base}")
    spec = json.loads((root / "ui-spec.json").read_text())
    assert us.check_spec(spec) == [] or us.check_spec(spec) == [
        "jsonschema not installed; schema not checked"]
    assert spec["mode"] == "data-product" and spec["read_only"] is True
    assert [p["archetype"] for p in spec["pages"]] == ["dashboard", "list"]
    roles = [r["role"] for r in spec["pages"][0]["regions"]]
    assert roles == ["kpi", "trend", "breakdown"]
    trend = spec["pages"][0]["regions"][1]
    assert trend["bindings"]["metric"] == "collection_rate"
    assert "line" in trend["pattern"].lower()               # rates draw as a line
    src = root / "app-src"
    if stack == "vite":
        assert (src / "App.tsx").is_file() and not (src / "app").exists()
        assert '"href": "#/overview"' in (src / "nav.ts").read_text()
    else:
        assert (src / "app/page.tsx").is_file() and (src / "app/claims/page.tsx").is_file()
        assert 'redirect("/overview")' in (src / "app/page.tsx").read_text()
    shell = (src / "components/kb/app-shell.tsx").read_text()
    assert ("render={<a href" in shell) == (base == "base")
    assert ("asChild" in shell) == (base == "radix")
    assert not (src / "lib/schemas.ts").exists()            # read-only: no forms
    sample = (src / "data/sample.ts").read_text()
    assert "SAMPLE-PAT-" in sample                          # restricted column placeholder
    assert "useTable" in (src / "components/kb/data-table.tsx").read_text()
    setup = (root / "setup.sh").read_text()
    assert f"--template {'vite' if stack == 'vite' else 'next'} --base {base}" in setup
    assert " chart" in setup and "@tanstack/react-table@^9" in setup
    notes = (root / "design-notes.md").read_text()
    assert "Rule: Rates over time draw as a line" in notes
    # Empty catalog: grounding warnings, never errors.
    assert any("not in the catalog" in i.message for i in issues)


def test_schema_package_has_forms(catalog, tmp_path):
    root, _ = _run(catalog, tmp_path, request="web shop orders", ddl=DDL, stack="nextjs",
                   base="radix", app_name="shop")
    spec = json.loads((root / "ui-spec.json").read_text())
    assert spec["mode"] == "schema" and spec["read_only"] is False
    page = spec["pages"][0]
    assert page["archetype"] == "list" and [r["role"] for r in page["regions"]] == ["table", "form"]
    src = root / "app-src"
    schemas = (src / "lib/schemas.ts").read_text()
    assert 'order_number: z.string().min(1, "Order number is required.")' in schemas
    assert 'status: z.enum(["open", "shipped", "cancelled"]).optional()' in schemas
    form = (src / "views/orders-form.tsx").read_text()
    assert "RadioGroup" in form and "EnumSelect" in form and "DateField" in form
    assert "Switch" in form and "Textarea" in form
    assert "asChild" in (src / "components/kb/date-field.tsx").read_text()
    setup = (root / "setup.sh").read_text()
    for item in ("field", "radio-group", "switch", "textarea", "popover", "calendar"):
        assert f" {item}" in setup
    assert "react-hook-form" in setup and "date-fns" in setup


def test_grounding_uses_catalog(catalog, tmp_path):
    conn, _ = catalog
    sid = conn.execute(
        "INSERT INTO doc_source (name, source_type, mcp_server, identifier) VALUES "
        "('shadcn/ui: Data Table', 'web_md', 'web', 'https://ui.shadcn.com/docs/components/data-table.md') "
        "RETURNING doc_source_id").fetchone()[0]
    snap = conn.execute(
        "INSERT INTO doc_snapshot (doc_source_id, url, content_hash) VALUES (?, "
        "'https://ui.shadcn.com/docs/components/data-table', 'h') RETURNING snapshot_id",
        [sid]).fetchone()[0]
    conn.execute("INSERT INTO doc_section (snapshot_id, heading_text, content, ordinal) VALUES "
                 "(?, 'Sorting', 'Use sorting with useTable.', 1)", [snap])
    bid = conn.execute("INSERT INTO book (title, source_path) VALUES ('Designing Interfaces', 'x.epub') "
                       "RETURNING book_id").fetchone()[0]
    conn.execute("INSERT INTO chapter (book_id, chapter_num, title, content) VALUES "
                 "(?, 1, 'Searching and Filtering: How Can I See Only the Data That I Need?', 'filters'), "
                 "(?, 2, 'Tables', 'tables')", [bid, bid])
    root, issues = _run(catalog, tmp_path, request="web shop orders", ddl=DDL, app_name="shop2")
    notes = (root / "design-notes.md").read_text()
    assert "shadcn docs: shadcn/ui: Data Table" in notes
    assert "Book: Designing Interfaces — Searching and Filtering" in notes
    assert not any("no book chapters" in i.message for i in issues)


def test_sample_data_is_deterministic():
    e = ui.entities_from_ddl(DDL)[0]
    assert ue.sample_rows(e, 5) == ue.sample_rows(e, 5)
    row = ue.sample_rows(e, 1)[0]
    assert row["customer_email"].startswith("SAMPLE-")
    assert row["status"] in ("open", "shipped", "cancelled")
