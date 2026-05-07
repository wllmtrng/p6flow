"""Regression tests for bugs exposed by real-world XER inputs. Each
test maps to a parser failure mode that the synthetic fixture didn't
cover until the bug was found.

Use these to lock in the fixes; without them, a future "cleanup" of
the tokenizer or loader could silently drop these accommodations.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from p6flow.ddl import duckdb_ddl
from p6flow.loader import RowArityMismatch, load_xer
from p6flow.tokenizer import _dedup_fields, _open_text, parse_tables


def _write_xer(path: Path, body: bytes | str) -> Path:
    if isinstance(body, str):
        body = body.encode()
    path.write_bytes(body)
    return path


# ---- Fix #1: full-file encoding probe ------------------------------


def test_encoding_probe_handles_late_high_byte(tmp_path):
    """An ASCII header followed by a cp1252 high byte deep in the file
    must not pick utf-8 and crash mid-stream. Regression for an
    encoding-fallback bug seen on real-world XER exports where the
    first 4096 bytes were ASCII-only."""
    # Long ASCII prefix > 4096 bytes, then a cp1252-only byte (0x9d =
    # right double quote in cp1252, invalid as utf-8 start byte).
    prefix = b"ERMHDR\t22.12\t2026-05-04\tProject\n%T\tACCOUNT\n%F\tacct_id\tacct_name\n"
    padding = b"%R\t1\tA" + b"x" * 8000 + b"\n"
    high_byte_row = b"%R\t2\tDescription with curly quote\x9d here\n"
    end = b"%E\n"
    xer = _write_xer(tmp_path / "f.xer", prefix + padding + high_byte_row + end)

    tables = list(parse_tables(xer))
    assert len(tables) == 1
    assert tables[0].name == "ACCOUNT"
    assert len(tables[0].rows) == 2
    # The high-byte char survives as the cp1252-decoded equivalent.
    assert "”" in tables[0].rows[1][1] or "\x9d" in tables[0].rows[1][1]


def test_open_text_returns_seekable(tmp_path):
    """_open_text returns StringIO that can be read line-by-line."""
    xer = _write_xer(tmp_path / "f.xer", "line1\nline2\nline3\n")
    handle = _open_text(xer)
    assert handle.readline() == "line1\n"
    assert handle.readline() == "line2\n"


def test_utf8_bom_stripped(tmp_path):
    """UTF-8 BOM must not contaminate the first decoded cell. Some
    real-world exporters prepend the BOM; cp1252 decodes it as 'ï»¿'
    which then poisons every downstream cell."""
    body = b"\xef\xbb\xbfERMHDR\t22.12\t2026-05-04\n%T\tACCOUNT\n%F\tacct_id\n%R\t1\n%E\n"
    xer = _write_xer(tmp_path / "f.xer", body)
    tables = list(parse_tables(xer))
    # No BOM survives in any cell
    for t in tables:
        for row in t.rows:
            for cell in row:
                assert "﻿" not in cell
                assert "ï" not in cell  # cp1252 representation of BOM


# ---- Fix #2: empty %T blocks ---------------------------------------


def test_empty_table_block_tolerated(tmp_path):
    """A %T followed immediately by another %T (no %F) is a real
    exporter quirk. Treat as zero-column zero-row table and continue."""
    body = (
        "ERMHDR\t22.12\t2026-05-04\n"
        "%T\tRSRC\n"  # empty: no %F, no %R
        "%T\tACTVTYPE\n"
        "%F\tactv_code_type_id\tproj_id\n"
        "%R\t1\t99\n"
        "%E\n"
    )
    xer = _write_xer(tmp_path / "f.xer", body)
    tables = {t.name: t for t in parse_tables(xer)}
    assert tables["RSRC"].fields == []
    assert tables["RSRC"].rows == []
    assert tables["ACTVTYPE"].rows == [["1", "99"]]


def test_empty_table_skipped_in_load(tmp_path):
    """A zero-column table must NOT trigger DuckDB's 'Table must have
    at least one column!' error in load_xer."""
    body = (
        "ERMHDR\t22.12\t2026-05-04\n"
        "%T\tRSRC\n"
        "%T\tACCOUNT\n"
        "%F\tacct_id\tacct_name\n"
        "%R\t1\tFoo\n"
        "%E\n"
    )
    xer = _write_xer(tmp_path / "f.xer", body)
    con = duckdb.connect(":memory:")
    counts = load_xer(con, list(parse_tables(xer)))
    assert "ACCOUNT" in counts
    # RSRC shouldn't appear because it's a zero-field table.
    assert "RSRC" not in counts


# ---- Fix #3: DDL identifier quoting --------------------------------


def test_ddl_quotes_column_names_with_spaces():
    """Malformed %F can land us with a column name like
    'create_user update_user'. DDL must quote it so DuckDB accepts."""
    sql = duckdb_ddl(
        "TASK", ["task_id", "create_user update_user"], overrides=None
    )
    assert '"create_user update_user"' in sql
    # Should be parseable by DuckDB.
    con = duckdb.connect(":memory:")
    con.execute(sql)
    cols = {r[0] for r in con.execute("DESCRIBE TASK").fetchall()}
    assert "create_user update_user" in cols


def test_ddl_quotes_reserved_word_columns():
    """A field literally named 'order' or 'select' must not break DDL."""
    sql = duckdb_ddl("X", ["order", "select"], overrides=None)
    con = duckdb.connect(":memory:")
    con.execute(sql)


# ---- Fix #4: trailing empty cells ----------------------------------


def test_trailing_empty_cell_tolerated(tmp_path):
    """Some exporters emit a trailing tab on every %R, producing one
    extra empty cell. _normalize_rows must drop trailing empties before
    the arity check."""
    body = (
        "ERMHDR\t22.12\t2026-05-04\n"
        "%T\tACCOUNT\n"
        "%F\tacct_id\tacct_name\n"
        "%R\t1\tFoo\t\n"  # one extra empty cell
        "%E\n"
    )
    xer = _write_xer(tmp_path / "f.xer", body)
    con = duckdb.connect(":memory:")
    counts = load_xer(con, list(parse_tables(xer)))
    assert counts["ACCOUNT"] == 1


def test_trailing_nonempty_cell_still_raises(tmp_path):
    """Real overflow (extra cell with data) is still a hard error."""
    body = (
        "ERMHDR\t22.12\t2026-05-04\n"
        "%T\tACCOUNT\n"
        "%F\tacct_id\tacct_name\n"
        "%R\t1\tFoo\textra-data\n"
        "%E\n"
    )
    xer = _write_xer(tmp_path / "f.xer", body)
    con = duckdb.connect(":memory:")
    with pytest.raises(RowArityMismatch, match="extra-data"):
        load_xer(con, list(parse_tables(xer)))


# ---- Fix #5: literal 'null' in typed casts -------------------------


def test_literal_null_in_numeric_column(tmp_path):
    """Some P6 exporters write the literal string 'null' in numeric
    cells in lieu of empty strings. CAST AS DOUBLE must treat that
    as SQL NULL."""
    body = (
        "ERMHDR\t22.12\t2026-05-04\n"
        "%T\tTASK\n"
        "%F\ttask_id\ttotal_float_hr_cnt\n"
        "%R\t1\t8.5\n"
        "%R\t2\tnull\n"
        "%R\t3\tNULL\n"
        "%R\t4\t\n"
        "%E\n"
    )
    xer = _write_xer(tmp_path / "f.xer", body)
    con = duckdb.connect(":memory:")
    load_xer(con, list(parse_tables(xer)))
    rows = con.execute(
        "SELECT task_id, total_float_hr_cnt FROM TASK ORDER BY task_id"
    ).fetchall()
    assert rows[0][1] == 8.5
    assert rows[1][1] is None  # 'null'
    assert rows[2][1] is None  # 'NULL'
    assert rows[3][1] is None  # ''


def test_literal_null_preserved_in_varchar_column(tmp_path):
    """A VARCHAR column might legitimately hold the literal string
    'null' (e.g. user note field). Don't convert it for VARCHAR casts."""
    body = (
        "ERMHDR\t22.12\t2026-05-04\n"
        "%T\tACCOUNT\n"
        "%F\tacct_id\tacct_name\n"
        "%R\t1\tnull\n"
        "%E\n"
    )
    xer = _write_xer(tmp_path / "f.xer", body)
    con = duckdb.connect(":memory:")
    load_xer(con, list(parse_tables(xer)))
    name = con.execute(
        "SELECT acct_name FROM ACCOUNT WHERE acct_id=1"
    ).fetchone()[0]
    assert name == "null"  # preserved verbatim


# ---- Fix #6: duplicate column names --------------------------------


def test_dedup_fields_basic():
    assert _dedup_fields(["a", "b", "a"]) == ["a", "b", "a_2"]
    assert _dedup_fields(["a", "a", "a"]) == ["a", "a_2", "a_3"]
    assert _dedup_fields(["a", "b", "c"]) == ["a", "b", "c"]


def test_duplicate_field_in_xer_renames_second(tmp_path):
    """Some real XERs declare the same column name twice in one %F
    line (e.g. last_recalc_date). The second occurrence is suffixed
    `_2` so the loader can keep all the row data."""
    body = (
        "ERMHDR\t22.12\t2026-05-04\n"
        "%T\tPROJECT\n"
        "%F\tproj_id\tlast_recalc_date\tlast_recalc_date\n"
        "%R\t1\t2025-01-01\t2025-06-01\n"
        "%E\n"
    )
    xer = _write_xer(tmp_path / "f.xer", body)
    tables = list(parse_tables(xer))
    assert tables[0].fields == [
        "proj_id", "last_recalc_date", "last_recalc_date_2"
    ]
    # Both columns retain their data through load.
    con = duckdb.connect(":memory:")
    load_xer(con, tables)
    cols = {r[0] for r in con.execute("DESCRIBE PROJECT").fetchall()}
    assert "last_recalc_date" in cols
    assert "last_recalc_date_2" in cols


# ---- --add-dw-columns flag ----------------------------------------


def test_dw_columns_added_to_source_table(tmp_path):
    """With add_dw_columns=True, every emitted source-table parquet
    gets the five DW columns at the end: source_xer, source_sha256,
    flow_published_at, exported_at, _raw."""
    from datetime import UTC, datetime

    from p6flow.output import write_parquet

    body = (
        "ERMHDR\t22.12\t2026-05-04\n"
        "%T\tACCOUNT\n"
        "%F\tacct_id\tacct_name\n"
        "%R\t1\tFoo\n"
        "%R\t2\tBar\n"
        "%E\n"
    )
    src = _write_xer(tmp_path / "MY-XER.xer", body)
    out = tmp_path / "out"
    con = duckdb.connect(":memory:")
    counts = load_xer(con, list(parse_tables(src)), add_dw_columns=True)
    before = datetime.now(UTC).replace(tzinfo=None)
    write_parquet(con, out, src, counts, add_dw_columns=True)

    cols = [r[0] for r in con.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{out / 'ACCOUNT.parquet'}')"
    ).fetchall()]
    # DW columns last, in this order:
    assert cols[-5:] == [
        "source_xer", "source_sha256", "flow_published_at", "exported_at", "_raw"
    ]

    rows = con.execute(f'''
        SELECT acct_id, source_xer, source_sha256, flow_published_at,
               exported_at, _raw
        FROM read_parquet('{out / 'ACCOUNT.parquet'}')
        ORDER BY acct_id
    ''').fetchall()
    assert len(rows) == 2
    assert rows[0][1] == "MY-XER"
    assert len(rows[0][2]) == 64  # SHA256
    # flow_published_at >= the time we recorded just before the COPY
    assert rows[0][3] >= before
    # exported_at == ERMHDR export_date
    assert rows[0][4] == datetime(2026, 5, 4)
    # _raw is the byte-faithful source line, captured by the tokenizer
    # before _split_tab. Newline-stripped only.
    assert rows[0][5] == "%R\t1\tFoo"
    assert rows[1][5] == "%R\t2\tBar"


def test_dw_raw_preserves_trailing_empty_cells(tmp_path):
    """Per-row _raw must reflect the on-disk source line, not a re-
    construction from normalized cells. A row with a trailing tab
    loses the trailing empty in `rows` but the raw `%R` line keeps it."""
    from p6flow.output import write_parquet

    body = (
        "ERMHDR\t22.12\t2026-05-04\n"
        "%T\tACCOUNT\n"
        "%F\tacct_id\tacct_name\n"
        "%R\t1\tFoo\t\n"  # trailing tab, dropped from cells, kept in _raw
        "%E\n"
    )
    src = _write_xer(tmp_path / "f.xer", body)
    out = tmp_path / "out"
    con = duckdb.connect(":memory:")
    counts = load_xer(con, list(parse_tables(src)), add_dw_columns=True)
    write_parquet(con, out, src, counts, add_dw_columns=True)

    raw = con.execute(
        f"SELECT _raw FROM read_parquet('{out / 'ACCOUNT.parquet'}')"
    ).fetchone()[0]
    # Trailing tab survives because tokenizer captured the line before
    # _split_tab + trailing-empty trimming.
    assert raw == "%R\t1\tFoo\t"


def test_dw_columns_off_keeps_narrow_schema(tmp_path):
    """Default add_dw_columns=False preserves the per-XER narrow schema."""
    from p6flow.output import write_parquet

    body = (
        "ERMHDR\t22.12\t2026-05-04\n"
        "%T\tACCOUNT\n"
        "%F\tacct_id\tacct_name\n"
        "%R\t1\tFoo\n"
        "%E\n"
    )
    src = _write_xer(tmp_path / "f.xer", body)
    out = tmp_path / "out"
    con = duckdb.connect(":memory:")
    counts = load_xer(con, list(parse_tables(src)))
    write_parquet(con, out, src, counts)

    cols = {r[0] for r in con.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{out / 'ACCOUNT.parquet'}')"
    ).fetchall()}
    assert "source_xer" not in cols
    assert "source_sha256" not in cols
    assert "flow_published_at" not in cols
    assert "exported_at" not in cols
    assert "_raw" not in cols


def test_dw_columns_handle_apostrophes_in_filename(tmp_path):
    """Source filename with an embedded apostrophe must not break the
    inlined SQL string literal."""
    from p6flow.output import write_parquet

    body = (
        "ERMHDR\t22.12\t2026-05-04\n"
        "%T\tACCOUNT\n"
        "%F\tacct_id\tacct_name\n"
        "%R\t1\tFoo\n"
        "%E\n"
    )
    src = _write_xer(tmp_path / "John's File.xer", body)
    out = tmp_path / "out"
    con = duckdb.connect(":memory:")
    counts = load_xer(con, list(parse_tables(src)), add_dw_columns=True)
    write_parquet(con, out, src, counts, add_dw_columns=True)

    tag = con.execute(
        f"SELECT DISTINCT source_xer FROM read_parquet('{out / 'ACCOUNT.parquet'}')"
    ).fetchone()[0]
    assert tag == "John's File"


def test_dw_raw_column_null_for_derived_tables(tmp_path):
    """Derived tables (not parsed from %R lines) emit _raw = NULL so
    every warehouse table has the same DW-column shape."""
    from p6flow.calendar import materialize_calendar_derived
    from p6flow.output import write_parquet

    fixture = (
        Path(__file__).resolve().parent.parent
        / "fixtures" / "synthetic_v2212.xer"
    )
    if not fixture.exists():
        pytest.skip("fixture missing")
    out = tmp_path / "out"
    con = duckdb.connect(":memory:")
    counts = load_xer(con, list(parse_tables(fixture)), add_dw_columns=True)
    counts.update(materialize_calendar_derived(con))
    write_parquet(con, out, fixture, counts, add_dw_columns=True)

    # CALENDAR_WEEKLY is derived; _raw should be NULL.
    raws = con.execute(
        f"SELECT DISTINCT _raw FROM read_parquet('{out / 'CALENDAR_WEEKLY.parquet'}')"
    ).fetchall()
    assert raws == [(None,)]
    # CALENDAR is a source table; _raw should NOT be NULL.
    src_raws = con.execute(
        f"SELECT _raw FROM read_parquet('{out / 'CALENDAR.parquet'}') LIMIT 1"
    ).fetchone()[0]
    assert src_raws is not None and src_raws.startswith("%R\t")


# ---- Security: hostile %T table name ------------------------------
#
# The %T directive flows unsanitized into SQL identifiers and Parquet
# output paths. A whitelist regex in the tokenizer is the single fix
# that defangs both SQL injection and path-traversal-via-COPY.


@pytest.mark.parametrize(
    "name",
    [
        "../../etc/passwd",            # path traversal
        "evil') TO '/tmp/owned' --",   # SQL string-literal escape
        "DROP_TABLE; --",              # statement injection attempt
        "lower_case_name",             # not a real P6 table name shape
        "WITH SPACE",                  # whitespace in identifier
        "",                            # empty after strip
    ],
)
def test_hostile_table_name_rejected(tmp_path, name):
    body = (
        b"ERMHDR\t22.12\t2026-05-04\tProject\n"
        + f"%T\t{name}\n%F\tx\n%R\t1\n%E\n".encode()
    )
    xer = _write_xer(tmp_path / "f.xer", body)
    with pytest.raises(ValueError, match="invalid %T table name"):
        list(parse_tables(xer))


def test_legitimate_table_names_accepted(tmp_path):
    """Sanity check: every uppercase identifier shape used by real P6
    schemas survives the new whitelist."""
    for name in ("ACCOUNT", "AC_PROJRSRCROLELIST", "TASK", "PROJWBS_X"):
        xer = _write_xer(
            tmp_path / f"{name}.xer",
            f"ERMHDR\t22.12\t2026-05-04\tProj\n%T\t{name}\n%F\tid\n%R\t1\n%E\n",
        )
        tables = list(parse_tables(xer))
        assert len(tables) == 1 and tables[0].name == name


# ---- Security: deeply nested clndr_data ------------------------------


def _make_nested_blob(depth: int) -> str:
    """Build a clndr_data blob nested `depth` levels deep. Node grammar
    is `(<flags>||<name>(<attrs>)(<children>))`; built iteratively so
    construction itself doesn't hit Python's recursion limit."""
    blob = "(||leaf()())"
    for _ in range(depth):
        blob = f"(||x()({blob}))"
    return blob


def test_calendar_nesting_depth_limit():
    """clndr_data parser must reject pathologically deep nesting at the
    parse layer before blowing Python's recursion limit."""
    from p6flow.calendar import _MAX_NESTING_DEPTH, _parse_root

    blob = _make_nested_blob(_MAX_NESTING_DEPTH + 5)
    with pytest.raises(ValueError, match="nesting exceeds depth limit"):
        _parse_root(blob)


def test_calendar_legitimate_nesting_accepted():
    """Real P6 calendars nest only a few levels deep — make sure normal
    blobs still parse."""
    from p6flow.calendar import _parse_root

    node = _parse_root(_make_nested_blob(5))
    assert node.name == "x"


def test_calendar_depth_limit_propagates_through_materialize(tmp_path):
    """Regression guard against accidentally wrapping the parse path in
    a try/except that swallows the ValueError. The depth-limit check is
    only a meaningful DoS defense if it aborts the whole load — silently
    skipping a hostile row would leave the rest of the pipeline running
    on attacker-controlled timing."""
    from p6flow.calendar import _MAX_NESTING_DEPTH, materialize_calendar_derived

    con = duckdb.connect(":memory:")
    con.execute(
        "CREATE TABLE CALENDAR (clndr_id BIGINT, clndr_data VARCHAR)"
    )
    con.execute(
        "INSERT INTO CALENDAR VALUES (1, ?)",
        [_make_nested_blob(_MAX_NESTING_DEPTH + 5)],
    )
    with pytest.raises(ValueError, match="nesting exceeds depth limit"):
        materialize_calendar_derived(con)
