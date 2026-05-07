"""End-to-end load test against a real XER fixture.

Uses one of the v22.12 sample XERs to exercise the full pipeline:
tokenizer -> inference -> DDL emit -> DuckDB load.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from p6flow.loader import load_xer
from p6flow.tokenizer import parse_tables, read_header

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures"
SAMPLE_XER = FIXTURE_DIR / "synthetic_v2212.xer"


@pytest.fixture(scope="module")
def loaded_db():
    if not SAMPLE_XER.exists():
        pytest.skip(f"fixture not found: {SAMPLE_XER}")
    tables = list(parse_tables(SAMPLE_XER))
    con = duckdb.connect(":memory:")
    counts = load_xer(con, tables)
    return con, counts, tables


def test_header_decodes(loaded_db):
    hdr = read_header(SAMPLE_XER)
    assert hdr.version == "22.12"
    assert hdr.export_date == "2025-07-15"


def test_table_counts_match_load_counts(loaded_db):
    _, counts, tables = loaded_db
    by_name = {t.name: len(t.rows) for t in tables}
    for name, n in counts.items():
        assert n == by_name[name], (
            f"{name}: loader returned {n} but parsed table had {by_name[name]}"
        )


def test_core_tables_present(loaded_db):
    con, _, _ = loaded_db
    tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    for required in ("PROJECT", "PROJWBS", "TASK", "TASKPRED", "CALENDAR"):
        assert required in tables, f"missing core table {required}"


def test_task_columns_typed_correctly(loaded_db):
    con, _, _ = loaded_db
    types = dict(con.execute("DESCRIBE TASK").fetchall().__class__(
        (row[0], row[1]) for row in con.execute("DESCRIBE TASK").fetchall()
    ))
    assert types["task_id"] == "BIGINT"
    assert types["proj_id"] == "BIGINT"
    assert types["task_code"].startswith("VARCHAR")
    assert types["task_name"].startswith("VARCHAR")
    # TIMESTAMP carrying a date column from the schema
    assert "TIMESTAMP" in types["target_start_date"]
    # crt_path_num is in v22.12 XER but NOT in v26.4 schema; inference picks BIGINT
    assert types["crt_path_num"] == "BIGINT"


def test_legacy_xer_columns_handled(loaded_db):
    con, _, _ = loaded_db
    # plan_open_state is XER-only (not in pmSchema.xml) and inference falls
    # back to VARCHAR since it has no naming-suffix opinion and the values
    # are all empty in this XER.
    types = {row[0]: row[1] for row in con.execute("DESCRIBE PROJWBS").fetchall()}
    assert "plan_open_state" in types
    assert types["plan_open_state"] == "VARCHAR"


def test_row_counts_nonzero_for_core_tables(loaded_db):
    _, counts, _ = loaded_db
    assert counts["TASK"] > 0
    assert counts["PROJWBS"] > 0
    assert counts["TASKPRED"] > 0


def test_total_float_data_is_real(loaded_db):
    """Sanity check that schedule data made it through: at least some tasks
    on the critical path (negative or zero float)."""
    con, _, _ = loaded_db
    critical = con.execute(
        "SELECT COUNT(*) FROM TASK WHERE total_float_hr_cnt <= 0"
    ).fetchone()[0]
    assert critical > 0


def test_missing_eof_marker_raises(tmp_path):
    """An XER missing its %E terminator should be rejected at parse time."""
    broken = tmp_path / "broken.xer"
    broken.write_text(
        "ERMHDR\t22.12\t2026-05-04\n"
        "%T\tACCOUNT\n"
        "%F\tacct_id\tacct_name\n"
        "%R\t1\tFoo\n"
    )
    with pytest.raises(ValueError, match="file ended without %E marker"):
        list(parse_tables(broken))
