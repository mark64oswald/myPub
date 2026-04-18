"""
Regression tests for DuckDB 1.5.0 FK-handling bugs we work around in
production. Pinning each variant so a future DuckDB version that fixes
(or changes) the behavior surfaces via a failing test — at which point
we can remove the workaround.

Today's observed variants (all produce a spurious "still referenced by
a foreign key in a different table" error):

  1. UPDATE on a FLOAT[N] (fixed-size array) column on a row that has
     inbound FK references.
     Workaround: keep embeddings in side tables; INSERT, don't UPDATE.

  2. UPDATE/DELETE on a row with a *self-referential* FK (even when
     setting the FK column to NULL).
     Workaround: drop the self-ref FK at the schema level; enforce the
     parent-pointer invariant in application code.

  3. UPDATE on a UNIQUE-constrained column on a row that has inbound
     FK references.
     Workaround (used in resolve_concept.do_rename): park the rows
     holding the inbound FKs in Python, UPDATE, reinsert.

Each test builds a minimal in-memory DuckDB that exercises the pattern
and asserts the error (currently expected to raise). When DuckDB
finally handles one of these correctly, the corresponding test will
fail and we should lift the workaround.
"""

from __future__ import annotations

import duckdb
import pytest


# ----------------------------------------------------------------------------
# Variant 1: UPDATE on FLOAT[N] column with inbound FK
# ----------------------------------------------------------------------------

def test_duckdb_bug_array_update_with_inbound_fk_still_fails():
    """UPDATE on FLOAT[N] when the row is FK-referenced → spurious error.

    A plain scalar (non-PK, non-UNIQUE) UPDATE on the same row is fine —
    the bug is specific to the array column.
    """
    c = duckdb.connect(":memory:")
    c.execute(
        "CREATE TABLE p ("
        "  id BIGINT PRIMARY KEY, scalar INTEGER, emb FLOAT[4]"
        ")"
    )
    c.execute("CREATE TABLE c (id BIGINT PRIMARY KEY, p_id BIGINT REFERENCES p(id))")
    c.execute("INSERT INTO p VALUES (1, 10, NULL)")
    c.execute("INSERT INTO c VALUES (100, 1)")

    # Plain scalar UPDATE on an FK-referenced row works normally.
    c.execute("UPDATE p SET scalar = 99 WHERE id = 1")
    assert c.execute("SELECT scalar FROM p WHERE id = 1").fetchone()[0] == 99

    # FLOAT[N] update on the same referenced row mis-fires.
    with pytest.raises(duckdb.ConstraintException, match="foreign key"):
        c.execute("UPDATE p SET emb = [0.1, 0.2, 0.3, 0.4]::FLOAT[4] WHERE id = 1")
    c.close()


def test_duckdb_array_update_on_unreferenced_row_works():
    """Sanity check — the same UPDATE works fine when nothing references the row."""
    c = duckdb.connect(":memory:")
    c.execute("CREATE TABLE p (id BIGINT PRIMARY KEY, emb FLOAT[4])")
    c.execute("INSERT INTO p VALUES (1, NULL)")
    # Row is NOT referenced by anything — array UPDATE works normally.
    c.execute("UPDATE p SET emb = [0.1, 0.2, 0.3, 0.4]::FLOAT[4] WHERE id = 1")
    row = c.execute("SELECT emb FROM p").fetchone()
    assert row is not None
    c.close()


# ----------------------------------------------------------------------------
# Variant 2: UPDATE/DELETE on a self-referential FK
# ----------------------------------------------------------------------------

def test_duckdb_bug_self_ref_fk_blocks_update_to_null():
    """With a self-ref FK, UPDATE … SET parent = NULL on a parent row fails.

    Our real-world scenario was clearing chapter.parent_chapter_id before
    deleting chapters on re-index. DuckDB rejected it with the same
    spurious FK error, even though NULL is a legal FK value.
    """
    c = duckdb.connect(":memory:")
    c.execute(
        "CREATE TABLE t ("
        "  id BIGINT PRIMARY KEY,"
        "  parent BIGINT REFERENCES t(id)"
        ")"
    )
    c.execute("INSERT INTO t VALUES (1, NULL)")
    c.execute("INSERT INTO t VALUES (2, 1)")
    c.execute("INSERT INTO t VALUES (3, 2)")

    # Deleting or updating row 2 (which is both a parent and a child)
    # should just work: UPDATE to NULL on a nullable FK column is trivially
    # valid. DuckDB 1.5 rejects it anyway.
    with pytest.raises(duckdb.ConstraintException, match="foreign key"):
        c.execute("UPDATE t SET parent = NULL WHERE id = 2")

    with pytest.raises(duckdb.ConstraintException, match="foreign key"):
        c.execute("DELETE FROM t WHERE id = 2")
    c.close()


def test_duckdb_self_ref_leaf_delete_works():
    """Sanity check — deleting a self-ref LEAF (no inbound references) is fine."""
    c = duckdb.connect(":memory:")
    c.execute(
        "CREATE TABLE t (id BIGINT PRIMARY KEY, parent BIGINT REFERENCES t(id))"
    )
    c.execute("INSERT INTO t VALUES (1, NULL)")
    c.execute("INSERT INTO t VALUES (2, 1)")
    c.execute("INSERT INTO t VALUES (3, 2)")
    # 3 is a leaf (nothing points at it) — delete succeeds.
    c.execute("DELETE FROM t WHERE id = 3")
    assert c.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2
    c.close()


# ----------------------------------------------------------------------------
# Variant 3: UPDATE on a UNIQUE-constrained column with inbound FK
# ----------------------------------------------------------------------------

def test_duckdb_bug_unique_column_update_with_inbound_fk_still_fails():
    """UPDATE on a UNIQUE column when the row is FK-referenced → spurious error.

    This is what bit resolve_concept.do_rename before we added the
    park-embedding-and-aliases workaround.
    """
    c = duckdb.connect(":memory:")
    c.execute(
        "CREATE TABLE p (id BIGINT PRIMARY KEY, name VARCHAR NOT NULL, UNIQUE(name))"
    )
    c.execute(
        "CREATE TABLE p_emb ("
        "  id BIGINT PRIMARY KEY REFERENCES p(id), emb FLOAT[4]"
        ")"
    )
    c.execute("INSERT INTO p VALUES (1, 'Orig')")
    c.execute("INSERT INTO p_emb VALUES (1, [0.0, 0.0, 0.0, 0.0]::FLOAT[4])")

    # UPDATE on the UNIQUE column errors, even though nothing is actually
    # FK-broken by the rename.
    with pytest.raises(duckdb.ConstraintException, match="foreign key"):
        c.execute("UPDATE p SET name = 'New' WHERE id = 1")
    c.close()


def test_duckdb_unique_column_update_without_inbound_fk_works():
    """Sanity check — same UPDATE is fine when nothing references the row."""
    c = duckdb.connect(":memory:")
    c.execute(
        "CREATE TABLE p (id BIGINT PRIMARY KEY, name VARCHAR NOT NULL, UNIQUE(name))"
    )
    c.execute("INSERT INTO p VALUES (1, 'Orig')")
    c.execute("UPDATE p SET name = 'New' WHERE id = 1")
    assert c.execute("SELECT name FROM p").fetchone()[0] == "New"
    c.close()


# ----------------------------------------------------------------------------
# Bonus: the "park-and-reinsert" workaround pattern we use in do_rename
# ----------------------------------------------------------------------------

def test_unique_update_workaround_park_and_reinsert_works():
    """When an UPDATE on a UNIQUE column is blocked by the bug, temporarily
    remove the inbound-FK rows, UPDATE, then re-insert them. This is the
    pattern used in scripts/resolve_concept.py::do_rename.
    """
    c = duckdb.connect(":memory:")
    c.execute(
        "CREATE TABLE p (id BIGINT PRIMARY KEY, name VARCHAR NOT NULL, UNIQUE(name))"
    )
    c.execute(
        "CREATE TABLE p_emb (id BIGINT PRIMARY KEY REFERENCES p(id), emb FLOAT[4])"
    )
    c.execute("INSERT INTO p VALUES (1, 'Orig')")
    c.execute("INSERT INTO p_emb VALUES (1, [0.1, 0.2, 0.3, 0.4]::FLOAT[4])")

    saved = c.execute("SELECT emb FROM p_emb WHERE id = 1").fetchone()[0]
    c.execute("DELETE FROM p_emb WHERE id = 1")
    c.execute("UPDATE p SET name = 'New' WHERE id = 1")
    c.execute("INSERT INTO p_emb VALUES (1, ?::FLOAT[4])", [saved])

    assert c.execute("SELECT name FROM p").fetchone()[0] == "New"
    assert c.execute("SELECT emb FROM p_emb").fetchone()[0] is not None
    c.close()
