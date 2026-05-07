"""FK validation tests.

One real-fixture integration test (must surface the documented orphan)
and a battery of synthetic tests covering both violation kinds plus
the NULL-in-nullable-FK no-op case.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from p6flow.loader import load_xer
from p6flow.tokenizer import parse_tables
from p6flow.validate import validate_fks

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "synthetic_v2212.xer"


# ---- Real fixture --------------------------------------------------


@pytest.fixture(scope="module")
def loaded_db():
    if not FIXTURE.exists():
        pytest.skip(f"fixture not found: {FIXTURE}")
    con = duckdb.connect(":memory:")
    load_xer(con, list(parse_tables(FIXTURE)))
    return con


def test_fixture_surfaces_known_orphan(loaded_db):
    """The synthetic fixture deliberately seeds ACTVTYPE.proj_id=429
    with no matching PROJECT row, so the validator must flag exactly
    one orphan FK violation against PROJECT."""
    violations = validate_fks(loaded_db)
    orphans = [v for v in violations if v.kind == "orphan"]
    actvtype = [v for v in orphans if v.child_table == "ACTVTYPE"]
    assert len(actvtype) == 1
    v = actvtype[0]
    assert v.target_table == "PROJECT"
    assert v.local_fields == ("proj_id",)
    assert v.violating_rows == 1
    assert ("429",) in v.sample_keys


# ---- Synthetic ----------------------------------------------------


def _build_minimal_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Real-schema-name tables for FOREIGN_KEYS lookup hits:

    - ACTVTYPE.proj_id is NULLABLE in pmSchema.xml -> exercises Case A
      (orphan) and Case B (NULL = legit, no violation).
    - BUDGCHNG.proj_id and BUDGCHNG.wbs_id are NOT NULL in pmSchema.xml
      -> exercises Case C (missing_required_fk).

    DuckDB-level NOT NULL is intentionally relaxed below so we can plant
    NULLs to test the validator. The validator reads nullability from
    COLUMN_TYPES, not from DuckDB's column metadata.
    """
    con.execute("CREATE TABLE PROJECT (proj_id BIGINT PRIMARY KEY)")
    con.execute("CREATE TABLE PROJWBS (wbs_id BIGINT PRIMARY KEY)")
    con.execute(
        "CREATE TABLE ACTVTYPE ("
        "  actv_code_type_id BIGINT PRIMARY KEY, "
        "  proj_id BIGINT"
        ")"
    )
    con.execute(
        "CREATE TABLE BUDGCHNG ("
        "  budg_chng_id BIGINT PRIMARY KEY, "
        "  proj_id BIGINT, "
        "  wbs_id BIGINT"
        ")"
    )


def test_clean_db_returns_no_violations():
    con = duckdb.connect(":memory:")
    _build_minimal_schema(con)
    con.execute("INSERT INTO PROJECT VALUES (1), (2)")
    con.execute("INSERT INTO PROJWBS VALUES (50)")
    con.execute("INSERT INTO ACTVTYPE VALUES (10, 1), (11, 2), (12, NULL)")
    con.execute("INSERT INTO BUDGCHNG VALUES (100, 1, 50), (101, 2, 50)")
    assert validate_fks(con) == []


def test_orphan_detected():
    """Non-null FK pointing at a missing parent → kind='orphan'."""
    con = duckdb.connect(":memory:")
    _build_minimal_schema(con)
    con.execute("INSERT INTO PROJECT VALUES (1)")
    con.execute(
        "INSERT INTO ACTVTYPE VALUES (10, 1), (11, 999), (12, 999), (13, 888)"
    )
    violations = [v for v in validate_fks(con) if v.child_table == "ACTVTYPE"]
    orphans = [v for v in violations if v.kind == "orphan"]
    assert len(orphans) == 1
    v = orphans[0]
    assert v.violating_rows == 3  # two pointing at 999, one at 888
    assert set(v.sample_keys) == {("999",), ("888",)}  # DISTINCT applied


def test_nullable_fk_null_is_not_violation():
    """ACTVTYPE.proj_id is nullable in pmSchema.xml; NULL should not
    fire either kind of violation."""
    con = duckdb.connect(":memory:")
    _build_minimal_schema(con)
    con.execute("INSERT INTO PROJECT VALUES (1)")
    con.execute("INSERT INTO ACTVTYPE VALUES (10, NULL), (11, 1)")
    violations = [v for v in validate_fks(con) if v.child_table == "ACTVTYPE"]
    assert violations == []


def test_required_fk_null_detected():
    """BUDGCHNG.proj_id is NOT NULL in pmSchema.xml. NULL there must
    surface as kind='missing_required_fk'."""
    con = duckdb.connect(":memory:")
    _build_minimal_schema(con)
    con.execute("INSERT INTO PROJECT VALUES (1)")
    con.execute("INSERT INTO PROJWBS VALUES (50)")
    con.execute(
        "INSERT INTO BUDGCHNG VALUES (100, 1, 50), (101, NULL, 50), (102, NULL, 50)"
    )
    violations = [v for v in validate_fks(con) if v.child_table == "BUDGCHNG"]
    missing = [v for v in violations if v.kind == "missing_required_fk"]
    # Two missing_required_fk entries: one per FK relation. proj_id is
    # the one we planted NULLs in; wbs_id is clean.
    proj = [v for v in missing if v.local_fields == ("proj_id",)]
    assert len(proj) == 1
    assert proj[0].violating_rows == 2
    assert proj[0].sample_keys == (("<NULL>",),)


def test_skips_relations_when_target_table_missing():
    """If the parent table isn't in this XER, the relation is skipped
    (matches DDL emission policy)."""
    con = duckdb.connect(":memory:")
    # Only ACTVTYPE present, no PROJECT. proj_id=999 should NOT be
    # reported as orphan because PROJECT is out of scope.
    con.execute(
        "CREATE TABLE ACTVTYPE ("
        "  actv_code_type_id BIGINT PRIMARY KEY, "
        "  proj_id BIGINT"
        ")"
    )
    con.execute("INSERT INTO ACTVTYPE VALUES (10, 999)")
    assert validate_fks(con) == []
