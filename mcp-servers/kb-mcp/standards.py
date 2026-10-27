"""
standards.py — healthcare standards registry (data/standards/standards.yaml).

A small lookup layer for the generators that scaffold or translate
healthcare solutions. Each standard lists the doc sources that cover it
(spec, guide, library, tool); this module stores that in three tables and
resolves each entry to a live doc_source row so a generator can cite the
right sources instead of a hard-coded list.

Tables (no FK to doc_source: a reference-only or not-yet-loaded source
still gets a row):
    standard           one row per standard
    standard_source    standard → source pointer (exact id or name match)
    standard_relation  MAPS_TO | PROFILES | SUPERSEDES between standards
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Optional

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
REGISTRY_FILE = PROJECT_ROOT / "data" / "standards" / "standards.yaml"

LICENSE_TIERS = {"open", "free_reg", "licensed"}
ROLES = {"spec", "guide", "library", "tool"}
RELATIONS = {"MAPS_TO", "PROFILES", "SUPERSEDES"}

DDL = """
CREATE TABLE IF NOT EXISTS standard (
    standard_key  VARCHAR PRIMARY KEY,
    name          VARCHAR NOT NULL,
    family        VARCHAR,
    market        VARCHAR,
    license_tier  VARCHAR NOT NULL,
    official_url  VARCHAR,
    versions      VARCHAR[],
    anchors       VARCHAR[],
    generators    VARCHAR[],
    notes         VARCHAR
);
CREATE TABLE IF NOT EXISTS standard_source (
    standard_key  VARCHAR NOT NULL,
    name          VARCHAR NOT NULL,
    role          VARCHAR NOT NULL,
    source_type   VARCHAR,
    identifier    VARCHAR,
    name_match    VARCHAR,
    planned       BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (standard_key, name)
);
CREATE TABLE IF NOT EXISTS standard_relation (
    from_key  VARCHAR NOT NULL,
    to_key    VARCHAR NOT NULL,
    relation  VARCHAR NOT NULL,
    PRIMARY KEY (from_key, to_key, relation)
);
"""


def read_registry(path: Path = REGISTRY_FILE) -> dict[str, Any]:
    import yaml
    return yaml.safe_load(path.read_text()) or {}


def validate(reg: dict[str, Any]) -> list[str]:
    """Return a list of problems; empty means the registry is loadable."""
    errors: list[str] = []
    keys: set[str] = set()
    for std in reg.get("standards") or []:
        key = std.get("key")
        if not key or not std.get("name"):
            errors.append(f"standard missing key/name: {std!r}")
            continue
        if key in keys:
            errors.append(f"{key}: duplicate key")
        keys.add(key)
        if std.get("license_tier") not in LICENSE_TIERS:
            errors.append(f"{key}: bad license_tier {std.get('license_tier')!r}")
        names: set[str] = set()
        for src in std.get("sources") or []:
            label = f"{key}/{src.get('name')}"
            if src.get("name") in names:
                errors.append(f"{label}: duplicate source name")
            names.add(src.get("name"))
            if src.get("role") not in ROLES:
                errors.append(f"{label}: bad role {src.get('role')!r}")
            exact = src.get("source_type") and src.get("identifier")
            if not exact and not src.get("match"):
                errors.append(f"{label}: needs source_type+identifier or match")
            if (std.get("license_tier") == "licensed" and src.get("role") == "spec"):
                errors.append(f"{label}: licensed standard cannot index its spec")
    for rel in reg.get("relations") or []:
        if rel.get("relation") not in RELATIONS:
            errors.append(f"relation {rel!r}: bad relation")
        for end in ("from", "to"):
            if rel.get(end) not in keys:
                errors.append(f"relation {rel!r}: unknown {end} key")
    return errors


def ensure_tables(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(DDL)


def load(conn: duckdb.DuckDBPyConnection, reg: dict[str, Any]) -> dict[str, int]:
    """Replace the registry tables with the contents of ``reg``.

    The registry file is the source of truth, so a load is a full refresh
    of three small tables, not an upsert.
    """
    errors = validate(reg)
    if errors:
        raise ValueError("invalid standards registry:\n  " + "\n  ".join(errors))
    ensure_tables(conn)
    conn.execute("BEGIN")
    try:
        for t in ("standard_relation", "standard_source", "standard"):
            conn.execute(f"DELETE FROM {t}")
        n_src = 0
        for std in reg.get("standards") or []:
            conn.execute(
                "INSERT INTO standard VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [std["key"], std["name"], std.get("family"), std.get("market"),
                 std["license_tier"], std.get("official_url"),
                 [str(v) for v in std.get("versions") or []],
                 [str(a) for a in std.get("anchors") or []],
                 list(std.get("generators") or []),
                 (std.get("notes") or "").strip() or None],
            )
            for src in std.get("sources") or []:
                conn.execute(
                    "INSERT INTO standard_source VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [std["key"], src["name"], src["role"], src.get("source_type"),
                     src.get("identifier"), src.get("match"),
                     bool(src.get("planned", False))],
                )
                n_src += 1
        for rel in reg.get("relations") or []:
            conn.execute("INSERT INTO standard_relation VALUES (?, ?, ?)",
                         [rel["from"], rel["to"], rel["relation"]])
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"standards": len(reg.get("standards") or []), "sources": n_src,
            "relations": len(reg.get("relations") or [])}


_RESOLVE_SQL = """
WITH sections AS (
    SELECT sn.doc_source_id, COUNT(*) AS n_sections
    FROM doc_section ds JOIN doc_snapshot sn USING (snapshot_id)
    GROUP BY sn.doc_source_id
)
SELECT s.standard_key, s.license_tier, ss.name, ss.role, ss.planned,
       d.doc_source_id, d.name AS doc_source_name, d.source_type,
       d.identifier, d.authority_score, COALESCE(x.n_sections, 0) AS n_sections
FROM standard s
JOIN standard_source ss USING (standard_key)
LEFT JOIN doc_source d
  ON (ss.identifier IS NOT NULL
      AND d.source_type = ss.source_type AND d.identifier = ss.identifier)
  OR (ss.identifier IS NULL AND ss.name_match IS NOT NULL
      AND d.name ILIKE ss.name_match)
LEFT JOIN sections x ON x.doc_source_id = d.doc_source_id
WHERE {where}
ORDER BY s.standard_key, ss.role, ss.name, d.doc_source_id
"""


_RESOLVE_COLS = ("standard_key", "license_tier", "name", "role", "planned",
                 "doc_source_id", "doc_source_name", "source_type", "identifier",
                 "authority_score", "n_sections")


def standard_sources(
    conn: duckdb.DuckDBPyConnection,
    *,
    keys: Optional[Iterable[str]] = None,
    family: Optional[str] = None,
    generator: Optional[str] = None,
    roles: Optional[Iterable[str]] = None,
    loaded_only: bool = True,
) -> list[dict[str, Any]]:
    """Doc sources registered for the selected standards.

    ``loaded_only`` drops pointers with no matching doc_source row or no
    sections yet, which is what a generator citing sources wants. A name
    match can resolve to several doc_source rows; each comes back.
    """
    where, params = ["TRUE"], []
    if keys is not None:
        keys = list(keys)
        where.append(f"s.standard_key IN ({', '.join('?' * len(keys)) or 'NULL'})")
        params += keys
    if family:
        where.append("s.family = ?")
        params.append(family)
    if generator:
        where.append("list_contains(s.generators, ?)")
        params.append(generator)
    if roles is not None:
        roles = list(roles)
        where.append(f"ss.role IN ({', '.join('?' * len(roles)) or 'NULL'})")
        params += roles
    if loaded_only:
        where.append("d.doc_source_id IS NOT NULL AND COALESCE(x.n_sections, 0) > 0")
    rows = conn.execute(_RESOLVE_SQL.format(where=" AND ".join(where)), params).fetchall()
    return [dict(zip(_RESOLVE_COLS, row)) for row in rows]


def status(conn: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    """Every registry pointer with its resolution state, for the CLI."""
    rows = standard_sources(conn, loaded_only=False)
    for r in rows:
        if r["doc_source_id"] is None:
            r["state"] = "planned" if r["planned"] else "missing"
        elif r["n_sections"] == 0:
            r["state"] = "registered, no sections"
        else:
            r["state"] = "loaded"
    return rows


# ---------------------------------------------------------------------------
# Generator helpers
# ---------------------------------------------------------------------------

# Which registry standards a generator request touches, from the words in
# its source / target / purpose. Deliberately coarse: it only widens the
# set of doc sources a generator cites beyond its hard-coded tools_cited.
_KEY_PATTERNS: list[tuple[str, str]] = [
    ("hl7v2", r"\bhl7\s?v2|\badt\b|\boru\b|\borm\b|\bmsh\b|\bpid\b"),
    ("fhir-r4", r"\bfhir\b"),
    ("us-core", r"\bus core\b|\buscdi\b"),
    ("x12-5010", r"\bx12\b|\bedi\b|\b(270|271|276|277|278|834|835|837)[pi]?\b"),
    ("davinci-pas", r"prior auth|\bpas\b|\b278\b"),
    ("carin-bb", r"explanationofbenefit|explanation of benefit|\beob\b|\bcarin\b|blue button"),
    ("cda", r"\bc-?cda\b|\bccd\b"),
    ("dicom-deid", r"\bdicom\b"),
]


def infer_standard_keys(text: str) -> list[str]:
    t = (text or "").lower()
    keys = [k for k, pat in _KEY_PATTERNS if re.search(pat, t)]
    if "hl7v2" in keys and "fhir-r4" in keys:
        keys.append("hl7v2-to-fhir")
    return keys


def cited_source_names(
    conn: duckdb.DuckDBPyConnection,
    tools_cited: Iterable[str],
    text: str = "",
) -> list[str]:
    """``tools_cited`` plus the loaded registry sources for the standards
    that ``text`` names. Falls back to ``tools_cited`` alone when the
    registry tables are absent (catalog not migrated yet)."""
    names = list(dict.fromkeys(tools_cited))
    keys = infer_standard_keys(text)
    if not keys:
        return names
    try:
        rows = standard_sources(conn, keys=keys)
    except duckdb.Error:
        return names
    for r in rows:
        if r["doc_source_name"] not in names:
            names.append(r["doc_source_name"])
    return names


def cite_concepts(
    conn: duckdb.DuckDBPyConnection,
    source_names: Iterable[str],
    *,
    limit: int = 30,
    name_like: Optional[str] = None,
) -> list[tuple[int, str, int, str]]:
    """(concept_id, concept name, doc_section_id, source name) citations
    drawn round-robin across ``source_names``, so one alphabetically early
    source can't fill the whole limit."""
    names = list(dict.fromkeys(source_names))
    if not names:
        return []
    params: list[Any] = list(names)
    extra = ""
    if name_like:
        extra = "AND LOWER(c.name) LIKE LOWER(?)"
        params.append(name_like)
    rows = conn.execute(
        f"""
        WITH hits AS (
            SELECT DISTINCT c.concept_id, c.name, ds.doc_section_id, src.name AS src_name
              FROM concept c
              JOIN concept_relation cr ON cr.from_concept_id = c.concept_id
                                       OR cr.to_concept_id = c.concept_id
              JOIN doc_section ds ON cr.source_id = ds.doc_section_id
                                  AND cr.source_type = 'doc_section'
              JOIN doc_snapshot sn USING (snapshot_id)
              JOIN doc_source src ON sn.doc_source_id = src.doc_source_id
             WHERE src.name IN ({', '.join('?' * len(names))}) {extra}
        ), one_per_concept AS (
            SELECT * FROM hits
            QUALIFY row_number() OVER (PARTITION BY concept_id, src_name
                                       ORDER BY doc_section_id) = 1
        )
        SELECT concept_id, name, doc_section_id, src_name,
               row_number() OVER (PARTITION BY src_name ORDER BY concept_id) AS rn
          FROM one_per_concept
         QUALIFY rn <= ?
         ORDER BY rn, src_name
        """,
        params + [limit],
    ).fetchall()
    return [(int(r[0]), r[1], int(r[2]), r[3]) for r in rows[:limit]]
