"""model_emit.py — write a RefModel (or a slice of one) back out as DDL and DBML (2026-11).

Shared by ``/kb-dp-from-model`` (a product's tables) and ``/kb-domain-model``
(a whole model). Output follows Unity Catalog conventions: three-part names
when a catalog is given, ``COMMENT`` on tables and columns, informational
``PRIMARY KEY`` / ``FOREIGN KEY`` constraints. FKs to tables outside the
slice are kept as comments so the slice still runs on its own.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Iterable, Optional, Sequence

import ref_models as rm

_TYPE_MAP = {"INT": "INT", "INTEGER": "INT", "BIGINT": "BIGINT", "SMALLINT": "SMALLINT",
             "STRING": "STRING", "VARCHAR": "STRING", "TEXT": "STRING", "CHAR": "STRING",
             "DATE": "DATE", "TIMESTAMP": "TIMESTAMP", "DATETIME": "TIMESTAMP",
             "BOOLEAN": "BOOLEAN", "BOOL": "BOOLEAN", "DOUBLE": "DOUBLE", "FLOAT": "DOUBLE",
             "DECIMAL": "DECIMAL(18,2)", "NUMERIC": "DECIMAL(18,2)", "BINARY": "BINARY"}


def uc_type(data_type: str) -> str:
    t = (data_type or "").strip().upper()
    if not t:
        return "STRING"
    if t.startswith(("DECIMAL", "NUMERIC")) and "(" in t:
        return t.replace("NUMERIC", "DECIMAL")
    if t.startswith(("ARRAY", "MAP", "STRUCT", "TIMESTAMP_NTZ")):
        return t
    return _TYPE_MAP.get(re.split(r"[\s(]", t)[0], "STRING")


def _sql_str(text: str) -> str:
    return "'" + (text or "").replace("'", "''").replace("\n", " ").strip() + "'"


def _ident(name: str) -> str:
    return name if re.fullmatch(r"[a-z_][a-z0-9_]*", name) else f"`{name}`"


def table_name(e: rm.RefEntity, catalog: Optional[str] = None) -> str:
    parts = ([catalog] if catalog else []) + ([e.domain] if e.domain else []) + [e.name]
    return ".".join(_ident(p) for p in parts)


class FkResolver:
    """Resolves attribute FKs to ``(entity_key, column)`` within one model."""

    def __init__(self, model: rm.RefModel):
        self.index = model.entity_index()
        self._by_name: dict[str, list[str]] = {}
        self._by_pk: dict[str, list[str]] = {}
        for e in model.entities:
            self._by_name.setdefault(e.name, []).append(e.key)
            if e.primary_key:
                self._by_pk.setdefault(e.primary_key, []).append(e.key)

    def __getitem__(self, key: str) -> rm.RefEntity:
        return self.index[key]

    def target(self, a: rm.RefAttribute) -> Optional[tuple[str, str]]:
        if not a.fk_to:
            return None
        key = rm.resolve_fk_target(a.fk_to, self.index, self._by_name, self._by_pk)
        if key is None:
            return None
        parts = a.fk_to.split(".")
        col = parts[-1] if len(parts) >= 3 else a.name
        cols = {x.name for x in self.index[key].attributes}
        if col not in cols:
            col = self.index[key].primary_key.split(",")[0] or col
        return key, col


def topo_order(entities: Sequence[rm.RefEntity], index: FkResolver) -> list[rm.RefEntity]:
    """Parents before children (cycles broken in input order)."""
    keys = {e.key for e in entities}
    deps: dict[str, set[str]] = defaultdict(set)
    for e in entities:
        for a in e.attributes:
            t = index.target(a)
            if t and t[0] in keys and t[0] != e.key:
                deps[e.key].add(t[0])
    out: list[rm.RefEntity] = []
    done: set[str] = set()
    remaining = list(entities)
    while remaining:
        ready = [e for e in remaining if deps[e.key] <= done] or remaining[:1]
        for e in ready:
            out.append(e)
            done.add(e.key)
        remaining = [e for e in remaining if e.key not in done]
    return out


def uc_ddl(entities: Sequence[rm.RefEntity], index: FkResolver, *,
           catalog: Optional[str] = None, header: str = "") -> str:
    """CREATE TABLE statements for ``entities`` (FKs to tables not in the slice
    become comments)."""
    keys = {e.key for e in entities}
    out = [f"-- {line}" for line in header.splitlines()] if header else []
    schemas = sorted({e.domain for e in entities if e.domain})
    for s in schemas:
        out.append(f"CREATE SCHEMA IF NOT EXISTS "
                   f"{'.'.join(_ident(p) for p in ([catalog] if catalog else []) + [s])};")
    for e in topo_order(entities, index):
        cols, cons, notes = [], [], []
        for a in e.attributes:
            line = f"  {_ident(a.name)} {uc_type(a.data_type)}"
            if a.is_pk:
                line += " NOT NULL"
            if a.description:
                line += f" COMMENT {_sql_str(a.description[:300])}"
            cols.append(line)
        pk = [c for c in e.primary_key.split(",") if c] or [a.name for a in e.attributes if a.is_pk]
        if pk:
            cons.append(f"  CONSTRAINT pk_{e.name} PRIMARY KEY ({', '.join(map(_ident, pk))})")
        for a in e.attributes:
            t = index.target(a)
            if not t:
                continue
            tkey, tcol = t
            if tkey in keys:
                cons.append(f"  CONSTRAINT fk_{e.name}_{a.name} FOREIGN KEY ({_ident(a.name)}) "
                            f"REFERENCES {table_name(index[tkey], catalog)} ({_ident(tcol)})")
            else:
                notes.append(f"-- {a.name} -> {tkey}.{tcol} (outside this slice)")
        body = ",\n".join(cols + cons)
        comment = f"\nCOMMENT {_sql_str(e.description[:500])}" if e.description else ""
        out += [""] + notes + [f"CREATE TABLE IF NOT EXISTS {table_name(e, catalog)} (\n{body}\n)"
                               f"{comment};"]
    return "\n".join(out).lstrip("\n") + "\n"


def dbml(entities: Sequence[rm.RefEntity], index: FkResolver, *,
         project: str = "") -> str:
    keys = {e.key for e in entities}
    out = [f"Project {_dbml_name(project)} {{\n  database_type: 'Databricks'\n}}", ""] if project else []
    refs = []
    for e in entities:
        out.append(f"Table {_dbml_name(e.key)} {{")
        pk = set(e.primary_key.split(",")) | {a.name for a in e.attributes if a.is_pk}
        for a in e.attributes:
            settings = []
            if a.name in pk:
                settings.append("pk")
            if a.description:
                settings.append("note: " + _dbml_str(a.description[:200]))
            out.append(f"  {_dbml_name(a.name)} {uc_type(a.data_type).lower()}"
                       + (f" [{', '.join(settings)}]" if settings else ""))
            t = index.target(a)
            if t and t[0] in keys:
                refs.append(f"Ref: {_dbml_name(e.key)}.{_dbml_name(a.name)} > "
                            f"{_dbml_name(t[0])}.{_dbml_name(t[1])}")
        if e.description:
            out.append(f"  Note: {_dbml_str(e.description[:300])}")
        out += ["}", ""]
    return "\n".join(out + refs) + "\n"


def _dbml_name(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) else f'"{name}"'


def _dbml_str(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'").replace("\n", " ") + "'"


def closure(seed: Iterable[str], index: FkResolver, *, depth: int = 1,
            skip_domains: Sequence[str] = ()) -> list[str]:
    """``seed`` plus the tables it references, ``depth`` hops out."""
    keys = list(dict.fromkeys(seed))
    frontier = list(keys)
    for _ in range(depth):
        nxt = []
        for k in frontier:
            for a in index[k].attributes:
                t = index.target(a)
                if t and t[0] not in keys and index[t[0]].domain not in skip_domains:
                    keys.append(t[0])
                    nxt.append(t[0])
        frontier = nxt
    return keys
