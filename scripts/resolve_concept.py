#!/usr/bin/env python3
"""
resolve_concept.py — Review and resolve borderline concept-resolution items.

Backs the /kb-review-concepts slash command. The resolver puts a
provisional concept in `concept` (with pending_review=TRUE) and a row
in `concept_resolution_queue` whenever an extraction candidate's
embedding similarity to the nearest existing concept falls in the
borderline band (0.75 ≤ sim < 0.90 by default). This CLI exposes the
four actions from arch doc §5.2 so a human can decide:

    merge <queue_id> [--register-alias]
        The candidate IS the nearest concept. Delete the provisional
        concept, rewrite any concept_relation edges that reference it
        to point at the target instead. With --register-alias, also
        register the candidate_name as an alias on the target so
        future extractions match automatically.

    alias <queue_id>
        Shorthand for `merge --register-alias`. Semantically equivalent.

    keep-separate <queue_id>
        The candidate is genuinely distinct. Clear pending_review on
        the provisional so it becomes a canonical concept.

    rename <queue_id> <new-name> [--merge-into TARGET]
        The extractor produced a poor name. Rename the provisional
        concept. With --merge-into <target_concept_id>, also merge
        edges into the target (same mechanics as `merge`).

The CLI also supports:
    list [--limit N]          top-N pending queue items with context
    show <queue_id>           full details for one item

Every action marks concept_resolution_queue.resolution_action and
stamps reviewed_at so the same row doesn't reappear in subsequent
list calls.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = PROJECT_ROOT / "data" / "catalog.ddb"
LOG = logging.getLogger("resolve_concept")


# ----------------------------------------------------------------------------
# Queue lookups
# ----------------------------------------------------------------------------

@dataclass
class QueueItem:
    """One row from concept_resolution_queue with denormalized names."""

    queue_id: int
    candidate_name: str
    candidate_context: Optional[str]
    source_type: Optional[str]
    source_id: Optional[int]
    provisional_concept_id: int
    nearest_concept_id: Optional[int]
    nearest_name: Optional[str]
    similarity_score: Optional[float]
    resolution_action: str
    created_at: str


def _load_item(conn: duckdb.DuckDBPyConnection, queue_id: int) -> QueueItem:
    """Fetch one queue row with the nearest concept's name joined in."""
    row = conn.execute(
        """
        SELECT q.queue_id, q.candidate_name, q.candidate_context,
               q.source_type, q.source_id,
               q.provisional_concept_id, q.nearest_concept_id,
               nearest.name AS nearest_name,
               q.similarity_score, q.resolution_action, q.created_at
          FROM concept_resolution_queue q
          LEFT JOIN concept nearest ON q.nearest_concept_id = nearest.concept_id
         WHERE q.queue_id = ?
        """,
        [queue_id],
    ).fetchone()
    if row is None:
        raise ValueError(f"queue_id {queue_id} not found")
    return QueueItem(*row)


def _require_pending(item: QueueItem) -> None:
    if item.resolution_action != "pending":
        raise ValueError(
            f"queue_id {item.queue_id} is already resolved "
            f"(action={item.resolution_action!r})"
        )
    if item.provisional_concept_id is None:
        raise ValueError(
            f"queue_id {item.queue_id} has no provisional_concept_id "
            f"(data issue — run the backfill migration)"
        )


# ----------------------------------------------------------------------------
# Action implementations
# ----------------------------------------------------------------------------

def _rewrite_edges(
    conn: duckdb.DuckDBPyConnection, from_id: int, to_id: int
) -> tuple[int, int]:
    """Rewrite concept_relation edges pointing at `from_id` to point at `to_id`.

    Returns (rows_moved, rows_dropped_as_duplicates). When rewriting collides
    with a pre-existing edge for the same PK (target already has that exact
    edge from the same source), we drop the duplicate rather than crash.
    """
    moved = 0
    dropped = 0
    for side in ("from_concept_id", "to_concept_id"):
        # Find edges that will collide with an existing edge after the move.
        # PK is (from, to, relation_type, source_type, source_id); the column
        # being rewritten is `side`.
        other = "to_concept_id" if side == "from_concept_id" else "from_concept_id"
        colliders = conn.execute(
            f"""
            SELECT a.from_concept_id, a.to_concept_id, a.relation_type,
                   a.source_type, a.source_id
              FROM concept_relation a
             WHERE a.{side} = ?
               AND EXISTS (
                   SELECT 1 FROM concept_relation b
                    WHERE b.{side} = ?
                      AND b.{other} = a.{other}
                      AND b.relation_type = a.relation_type
                      AND b.source_type = a.source_type
                      AND b.source_id = a.source_id
               )
            """,
            [from_id, to_id],
        ).fetchall()
        for (f_id, t_id, rtype, stype, sid) in colliders:
            conn.execute(
                "DELETE FROM concept_relation "
                "WHERE from_concept_id = ? AND to_concept_id = ? "
                "AND relation_type = ? AND source_type = ? AND source_id = ?",
                [f_id, t_id, rtype, stype, sid],
            )
            dropped += 1
        # Remaining rows can be rewritten safely.
        count = conn.execute(
            f"SELECT COUNT(*) FROM concept_relation WHERE {side} = ?", [from_id]
        ).fetchone()[0]
        if count:
            conn.execute(
                f"UPDATE concept_relation SET {side} = ? WHERE {side} = ?",
                [to_id, from_id],
            )
            moved += count
    return moved, dropped


def _delete_provisional(conn: duckdb.DuckDBPyConnection, concept_id: int) -> None:
    """Remove provisional concept and its embedding.

    Caller must ensure no concept_relation edges reference it anymore.
    concept_alias, concept_query_log, concept_doc_link, and
    concept_resolution_queue.nearest_concept_id all reference concept; scrub
    them or they'll block the delete (they're FK-enforced).
    """
    conn.execute("DELETE FROM concept_alias WHERE concept_id = ?", [concept_id])
    conn.execute("DELETE FROM concept_query_log WHERE concept_id = ?", [concept_id])
    conn.execute("DELETE FROM concept_doc_link WHERE concept_id = ?", [concept_id])
    conn.execute(
        "UPDATE concept_resolution_queue SET nearest_concept_id = NULL "
        "WHERE nearest_concept_id = ?",
        [concept_id],
    )
    conn.execute("DELETE FROM concept_embedding WHERE concept_id = ?", [concept_id])
    conn.execute("DELETE FROM concept WHERE concept_id = ?", [concept_id])


def _mark_resolved(
    conn: duckdb.DuckDBPyConnection, queue_id: int, action: str
) -> None:
    """Stamp the queue row with the chosen action and reviewed_at."""
    conn.execute(
        "UPDATE concept_resolution_queue "
        "   SET resolution_action = ?, reviewed_at = CURRENT_TIMESTAMP "
        " WHERE queue_id = ?",
        [action, queue_id],
    )


def do_merge(
    conn: duckdb.DuckDBPyConnection,
    queue_id: int,
    register_alias: bool,
) -> None:
    """Fold the provisional concept into its nearest neighbor."""
    item = _load_item(conn, queue_id)
    _require_pending(item)
    if item.nearest_concept_id is None:
        raise ValueError(
            f"queue_id {queue_id} has no nearest_concept_id; cannot merge"
        )
    moved, dropped = _rewrite_edges(
        conn, item.provisional_concept_id, item.nearest_concept_id
    )
    if register_alias:
        # Register candidate_name as alias on the target, idempotently.
        existing = conn.execute(
            "SELECT alias_id FROM concept_alias "
            "WHERE concept_id = ? AND LOWER(alias) = LOWER(?)",
            [item.nearest_concept_id, item.candidate_name],
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO concept_alias (concept_id, alias, alias_type) "
                "VALUES (?, ?, 'synonym')",
                [item.nearest_concept_id, item.candidate_name],
            )
    _delete_provisional(conn, item.provisional_concept_id)
    _mark_resolved(conn, queue_id, "alias" if register_alias else "merge")
    conn.commit()
    LOG.info(
        "queue %d: merged %r (id=%d) → %r (id=%d); edges moved=%d dropped=%d%s",
        queue_id, item.candidate_name, item.provisional_concept_id,
        item.nearest_name, item.nearest_concept_id,
        moved, dropped,
        " + alias registered" if register_alias else "",
    )


def do_keep_separate(conn: duckdb.DuckDBPyConnection, queue_id: int) -> None:
    """Accept the provisional as genuinely distinct; clear pending_review."""
    item = _load_item(conn, queue_id)
    _require_pending(item)
    conn.execute(
        "UPDATE concept SET pending_review = FALSE, updated_at = CURRENT_TIMESTAMP "
        "WHERE concept_id = ?",
        [item.provisional_concept_id],
    )
    _mark_resolved(conn, queue_id, "keep_separate")
    conn.commit()
    LOG.info(
        "queue %d: kept %r (id=%d) separate from %r (id=%s)",
        queue_id, item.candidate_name, item.provisional_concept_id,
        item.nearest_name, item.nearest_concept_id,
    )


def do_rename(
    conn: duckdb.DuckDBPyConnection,
    queue_id: int,
    new_name: str,
    merge_into: Optional[int],
) -> None:
    """Rename the provisional. With --merge-into, also fold into that concept."""
    item = _load_item(conn, queue_id)
    _require_pending(item)
    if merge_into is not None:
        # Merge edges into the target, then delete the provisional.
        moved, dropped = _rewrite_edges(conn, item.provisional_concept_id, merge_into)
        # Register the rename as an alias on the target (helps future
        # resolution when the extractor emits the same bad name again).
        existing = conn.execute(
            "SELECT alias_id FROM concept_alias "
            "WHERE concept_id = ? AND LOWER(alias) = LOWER(?)",
            [merge_into, item.candidate_name],
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO concept_alias (concept_id, alias, alias_type) "
                "VALUES (?, ?, 'synonym')",
                [merge_into, item.candidate_name],
            )
        _delete_provisional(conn, item.provisional_concept_id)
        _mark_resolved(conn, queue_id, "rename")
        conn.commit()
        LOG.info(
            "queue %d: renamed %r → %r, merged into id=%d (edges moved=%d dropped=%d)",
            queue_id, item.candidate_name, new_name, merge_into, moved, dropped,
        )
        return

    # No target → rename and clear pending_review.
    # DuckDB 1.5 bug: UPDATE on a UNIQUE-constrained column (concept has
    # UNIQUE(name, concept_type)) fails with a spurious FK error when the
    # row is referenced by any inbound FK (concept_embedding here). Work
    # around by parking the embedding and any aliases outside the FK
    # scope during the UPDATE, then re-linking.
    emb_row = conn.execute(
        "SELECT embedding, model FROM concept_embedding WHERE concept_id = ?",
        [item.provisional_concept_id],
    ).fetchone()
    alias_rows = conn.execute(
        "SELECT alias, alias_type FROM concept_alias WHERE concept_id = ?",
        [item.provisional_concept_id],
    ).fetchall()
    conn.execute(
        "DELETE FROM concept_alias WHERE concept_id = ?",
        [item.provisional_concept_id],
    )
    conn.execute(
        "DELETE FROM concept_embedding WHERE concept_id = ?",
        [item.provisional_concept_id],
    )
    conn.execute(
        "UPDATE concept "
        "   SET name = ?, pending_review = FALSE, updated_at = CURRENT_TIMESTAMP "
        " WHERE concept_id = ?",
        [new_name, item.provisional_concept_id],
    )
    if emb_row is not None:
        conn.execute(
            "INSERT INTO concept_embedding (concept_id, embedding, model) "
            "VALUES (?, ?, ?)",
            [item.provisional_concept_id, emb_row[0], emb_row[1]],
        )
    for alias, alias_type in alias_rows:
        conn.execute(
            "INSERT INTO concept_alias (concept_id, alias, alias_type) "
            "VALUES (?, ?, ?)",
            [item.provisional_concept_id, alias, alias_type],
        )
    _mark_resolved(conn, queue_id, "rename")
    conn.commit()
    LOG.info(
        "queue %d: renamed %r (id=%d) → %r and kept as standalone concept",
        queue_id, item.candidate_name, item.provisional_concept_id, new_name,
    )


# ----------------------------------------------------------------------------
# Listing / display
# ----------------------------------------------------------------------------

def do_list(conn: duckdb.DuckDBPyConnection, limit: int) -> None:
    """Print the next N pending items in queue order (highest similarity first)."""
    rows = conn.execute(
        """
        SELECT q.queue_id, q.candidate_name,
               nearest.concept_id AS nearest_id, nearest.name AS nearest_name,
               q.similarity_score, q.source_type, q.source_id
          FROM concept_resolution_queue q
          LEFT JOIN concept nearest ON q.nearest_concept_id = nearest.concept_id
         WHERE q.resolution_action = 'pending'
         ORDER BY q.similarity_score DESC, q.queue_id
         LIMIT ?
        """,
        [limit],
    ).fetchall()
    if not rows:
        print("queue is empty — nothing pending.")
        return
    total = conn.execute(
        "SELECT COUNT(*) FROM concept_resolution_queue WHERE resolution_action = 'pending'"
    ).fetchone()[0]
    print(f"{len(rows)} of {total} pending items:")
    for q_id, cand, _nid, nname, sim, stype, sid in rows:
        sim_str = f"{sim:.3f}" if sim is not None else "  -  "
        src = f"{stype}:{sid}" if stype else "?"
        print(f"  q={q_id:<4} sim={sim_str}  {cand!r:<42} ↔ "
              f"{(nname or '?')!r:<40}  src={src}")


def do_show(conn: duckdb.DuckDBPyConnection, queue_id: int) -> None:
    """Print everything we know about one queue item."""
    item = _load_item(conn, queue_id)
    print(f"queue_id:              {item.queue_id}")
    print(f"resolution_action:     {item.resolution_action}")
    print(f"candidate_name:        {item.candidate_name!r}")
    print(f"provisional_concept:   id={item.provisional_concept_id}")
    print(f"nearest_concept:       id={item.nearest_concept_id} "
          f"name={item.nearest_name!r}")
    sim_str = f"{item.similarity_score:.3f}" if item.similarity_score else "-"
    print(f"similarity:            {sim_str}")
    print(f"source:                {item.source_type}:{item.source_id}")
    if item.candidate_context:
        snippet = item.candidate_context[:300]
        print(f"candidate_context:     {snippet!r}")

    # Also print the provisional concept's description for context.
    prov = _concept_description(item.provisional_concept_id)
    if prov:
        print(f"provisional desc:      {prov[:300]!r}")
    near = _concept_description(item.nearest_concept_id) if item.nearest_concept_id else None
    if near:
        print(f"nearest desc:          {near[:300]!r}")


_CONN_FOR_DESC: Optional[duckdb.DuckDBPyConnection] = None


def _concept_description(concept_id: Optional[int]) -> Optional[str]:
    """Fetch concept.description via the module-level connection (set in main)."""
    if concept_id is None or _CONN_FOR_DESC is None:
        return None
    row = _CONN_FOR_DESC.execute(
        "SELECT description FROM concept WHERE concept_id = ?", [concept_id]
    ).fetchone()
    return row[0] if row else None


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main() -> int:
    """Parse args and dispatch the requested subcommand."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="show pending queue items")
    p_list.add_argument("--limit", type=int, default=10)

    p_show = sub.add_parser("show", help="details for one queue item")
    p_show.add_argument("queue_id", type=int)

    p_merge = sub.add_parser("merge",
                             help="fold provisional into nearest concept")
    p_merge.add_argument("queue_id", type=int)
    p_merge.add_argument("--register-alias", action="store_true")

    p_alias = sub.add_parser("alias",
                             help="shorthand for `merge --register-alias`")
    p_alias.add_argument("queue_id", type=int)

    p_keep = sub.add_parser("keep-separate",
                            help="accept provisional as distinct")
    p_keep.add_argument("queue_id", type=int)

    p_rename = sub.add_parser("rename",
                              help="rename provisional (optionally merge-into)")
    p_rename.add_argument("queue_id", type=int)
    p_rename.add_argument("new_name", type=str)
    p_rename.add_argument("--merge-into", type=int, default=None,
                          help="fold into this target concept_id after renaming")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    conn = duckdb.connect(str(args.catalog))
    # concept_embedding may carry an HNSW index from Phase 1.5. DuckDB
    # refuses to modify a table with an unknown-type index bound to it,
    # so load VSS before any DELETE/UPDATE touches concept_embedding.
    # LOAD is cheap on a per-connection basis; safe to call unconditionally.
    try:
        conn.execute("LOAD vss")
    except duckdb.Error:
        # VSS extension not installed on this catalog — that's fine for
        # in-memory test fixtures. Skip silently; any DML against a live
        # HNSW-indexed table would have failed anyway.
        pass
    global _CONN_FOR_DESC  # pylint: disable=global-statement
    _CONN_FOR_DESC = conn
    try:
        if args.command == "list":
            do_list(conn, args.limit)
        elif args.command == "show":
            do_show(conn, args.queue_id)
        elif args.command == "merge":
            do_merge(conn, args.queue_id, args.register_alias)
        elif args.command == "alias":
            do_merge(conn, args.queue_id, register_alias=True)
        elif args.command == "keep-separate":
            do_keep_separate(conn, args.queue_id)
        elif args.command == "rename":
            do_rename(conn, args.queue_id, args.new_name, args.merge_into)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
