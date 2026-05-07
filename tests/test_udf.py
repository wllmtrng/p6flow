"""UDF pivot tests.

Synthetic coverage for slugification, type dispatch, collision
disambiguation. Integration test against the synthetic fixture
verifies the wide table shape end to end.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from p6flow.loader import load_xer
from p6flow.tokenizer import parse_tables
from p6flow.udf import (
    _resolve_column_names,
    _slugify_label,
    materialize_udf_wide,
)

SYNTH_FIXTURE = (
    Path(__file__).resolve().parent.parent / "fixtures" / "synthetic_v2212.xer"
)


# ---- Slugification ------------------------------------------------


@pytest.mark.parametrize(
    "label,expected",
    [
        ("MSP Activity ID", "udf_msp_activity_id"),
        ("Notes", "udf_notes"),
        ("Text_04", "udf_text_04"),
        ("B/L Reference", "udf_b_l_reference"),
        ("$Cost (USD)", "udf_cost_usd"),
        ("   spaces   ", "udf_spaces"),
        ("", "udf_unnamed"),
        ("@@@", "udf_unnamed"),
    ],
)
def test_slugify_label(label, expected):
    assert _slugify_label(label) == expected


def test_collision_disambiguation():
    """Two labels slugify to the same column -> second gets `_<id>`."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE UDFVALUE (udf_type_id BIGINT, udf_text VARCHAR, "
                "udf_number DOUBLE, udf_date TIMESTAMP, udf_code_id BIGINT)")
    types = [
        (10, "Note", "FT_TEXT"),
        (11, "note", "FT_TEXT"),
        (12, "NOTE!", "FT_TEXT"),
    ]
    cols, _ = _resolve_column_names(con, types)
    assert [c[3] for c in cols] == ["udf_note", "udf_note_11", "udf_note_12"]


# ---- Synthetic pivot ----------------------------------------------


def _build_udf_schema(con):
    """Minimal UDFTYPE/UDFVALUE/TASK setup that exercises every type
    branch present in _TYPE_DISPATCH."""
    con.execute("CREATE TABLE TASK (task_id BIGINT PRIMARY KEY)")
    con.execute(
        "CREATE TABLE UDFTYPE ("
        "  udf_type_id BIGINT PRIMARY KEY, "
        "  table_name VARCHAR, "
        "  udf_type_label VARCHAR, "
        "  logical_data_type VARCHAR"
        ")"
    )
    con.execute(
        "CREATE TABLE UDFVALUE ("
        "  udf_type_id BIGINT, "
        "  fk_id BIGINT, "
        "  proj_id BIGINT, "
        "  udf_text VARCHAR, "
        "  udf_number DECIMAL(18,4), "
        "  udf_date TIMESTAMP, "
        "  udf_code_id BIGINT"
        ")"
    )


def test_synthetic_pivot_typed_columns():
    """End-to-end: text, number, integer, date types each land in
    correctly-typed columns in the wide table."""
    con = duckdb.connect(":memory:")
    _build_udf_schema(con)

    con.execute(
        "INSERT INTO UDFTYPE VALUES "
        "(1, 'TASK', 'Notes', 'FT_TEXT'), "
        "(2, 'TASK', 'Cost', 'FT_NUMBER'), "
        "(3, 'TASK', 'Iteration', 'FT_TYPE_INTEGER'), "
        "(4, 'TASK', 'Due Date', 'FT_DATE'), "
        "(5, 'TASK', 'Total Earned Previous', 'FT_MONEY')"
    )
    con.execute("INSERT INTO TASK VALUES (100), (101)")
    con.execute(
        "INSERT INTO UDFVALUE VALUES "
        "(1, 100, 99, 'Hello',     NULL,    NULL,                       NULL), "
        "(2, 100, 99, NULL,        12.50,   NULL,                       NULL), "
        "(3, 100, 99, NULL,        7,       NULL,                       NULL), "
        "(4, 101, 99, NULL,        NULL,    TIMESTAMP '2026-05-06 09:30', NULL), "
        "(5, 100, 99, NULL,        9999.95, NULL,                       NULL)"
    )

    materialize_udf_wide(con)
    schema = {row[0]: row[1] for row in con.execute("DESCRIBE TASK_UDF").fetchall()}
    assert schema["task_id"] == "BIGINT"
    assert schema["udf_notes"].startswith("VARCHAR")
    # FT_NUMBER preserves the source DECIMAL type
    assert "DECIMAL" in schema["udf_cost"] or schema["udf_cost"] == "DOUBLE"
    # FT_TYPE_INTEGER applies a CAST to BIGINT
    assert schema["udf_iteration"] == "BIGINT"
    assert schema["udf_due_date"] == "TIMESTAMP"

    rows = {r[0]: r for r in con.execute(
        "SELECT task_id, udf_notes, udf_cost, udf_iteration, udf_due_date, "
        "udf_total_earned_previous FROM TASK_UDF ORDER BY task_id"
    ).fetchall()}
    assert rows[100][1] == "Hello"
    assert float(rows[100][2]) == 12.50
    assert rows[100][3] == 7
    assert rows[101][4] is not None
    # FT_MONEY: regression for a dispatch bug that silently dropped
    # EAV rows when the type wasn't in _TYPE_DISPATCH.
    assert float(rows[100][5]) == 9999.95


def test_skips_when_udf_tables_missing():
    """No UDFTYPE/UDFVALUE -> empty counts, empty unknown list."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE TASK (task_id BIGINT PRIMARY KEY)")
    assert materialize_udf_wide(con) == ({}, [])


def test_unknown_logical_data_type_inferred_from_data():
    """A logical_data_type not in _TYPE_DISPATCH must still pivot
    correctly by inspecting which UDFVALUE column has data, and the
    type must show up in the unknown_types log."""
    con = duckdb.connect(":memory:")
    _build_udf_schema(con)
    # FT_FUTURE_TYPE doesn't exist; data lives in udf_number, so the
    # inspector must pick udf_number and produce a numeric column.
    con.execute(
        "INSERT INTO UDFTYPE VALUES "
        "(99, 'TASK', 'Mystery Field', 'FT_FUTURE_TYPE')"
    )
    con.execute("INSERT INTO TASK VALUES (200)")
    con.execute(
        "INSERT INTO UDFVALUE VALUES "
        "(99, 200, 99, NULL, 42.5, NULL, NULL)"
    )
    counts, unknown = materialize_udf_wide(con)
    assert counts.get("TASK_UDF") == 1
    # The unknown type was logged with its inferred source column.
    assert len(unknown) == 1
    u = unknown[0]
    assert u["logical_data_type"] == "FT_FUTURE_TYPE"
    assert u["udf_type_label"] == "Mystery Field"
    assert u["inferred_source_column"] == "udf_number"
    assert u["entity_table"] == "TASK"
    # And the data made it through the pivot.
    val = con.execute(
        "SELECT udf_mystery_field FROM TASK_UDF WHERE task_id = 200"
    ).fetchone()[0]
    assert float(val) == 42.5


# ---- Fixture integration ------------------------------------------


@pytest.fixture(scope="module")
def loaded_db():
    if not SYNTH_FIXTURE.exists():
        pytest.skip(f"fixture not found: {SYNTH_FIXTURE}")
    con = duckdb.connect(":memory:")
    load_xer(con, list(parse_tables(SYNTH_FIXTURE)))
    counts, _ = materialize_udf_wide(con)
    return con, counts


def test_task_udf_created(loaded_db):
    con, counts = loaded_db
    tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    assert "TASK_UDF" in tables
    assert "UDF_COLUMN_MAP" in tables
    assert counts["TASK_UDF"] > 0


def test_expected_pivot_columns(loaded_db):
    """The synthetic fixture defines three TASK-scoped UDFTYPEs
    (MSP Activity ID, Notes, Text_01). The wide table must carry
    one VARCHAR column per slugified label."""
    con, _ = loaded_db
    schema = {r[0]: r[1] for r in con.execute("DESCRIBE TASK_UDF").fetchall()}
    expected = {
        "task_id", "proj_id",
        "udf_msp_activity_id", "udf_notes", "udf_text_01",
    }
    assert expected.issubset(schema.keys())
    for col in expected - {"task_id", "proj_id"}:
        assert schema[col].startswith("VARCHAR")


def test_pivot_values_round_trip(loaded_db):
    """Sanity: per-task value count in TASK_UDF matches the EAV row
    count for that task in UDFVALUE."""
    con, _ = loaded_db
    # Pick any task with at least one UDF assigned (synthetic fixture
    # has 3 UDFs distributed across 3 tasks).
    task_id = con.execute(
        "SELECT fk_id FROM UDFVALUE GROUP BY fk_id "
        "HAVING COUNT(*) >= 1 ORDER BY fk_id LIMIT 1"
    ).fetchone()[0]
    eav_count = con.execute(
        "SELECT COUNT(*) FROM UDFVALUE WHERE fk_id = ?", [task_id]
    ).fetchone()[0]

    cols = [
        r[0] for r in con.execute("DESCRIBE TASK_UDF").fetchall()
        if r[0].startswith("udf_")
    ]
    nn_expr = " + ".join(
        f'(CASE WHEN "{c}" IS NOT NULL THEN 1 ELSE 0 END)' for c in cols
    )
    pivot_count = con.execute(
        f"SELECT {nn_expr} FROM TASK_UDF WHERE task_id = ?", [task_id]
    ).fetchone()[0]
    assert pivot_count == eav_count


def test_column_map_round_trip(loaded_db):
    """UDF_COLUMN_MAP must let consumers recover original labels."""
    con, _ = loaded_db
    rows = {
        r[0]: r[1] for r in con.execute(
            "SELECT column_name, udf_type_label "
            "FROM UDF_COLUMN_MAP WHERE wide_table = 'TASK_UDF'"
        ).fetchall()
    }
    assert rows["udf_msp_activity_id"] == "MSP Activity ID"
    assert rows["udf_notes"] == "Notes"
