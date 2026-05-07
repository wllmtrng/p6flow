"""User-Defined Field (UDF) flattening from EAV to typed wide tables.

P6 stores UDF metadata in UDFTYPE (one row per defined field) and
values in UDFVALUE (one row per (entity_row, udf_type) pair). This
module reads both and emits one wide DuckDB table per discriminator
(`UDFTYPE.table_name`), e.g. TASK_UDF, PROJECT_UDF, RSRC_UDF.

EAV mechanics:
    UDFVALUE.udf_type_id ─→ UDFTYPE.udf_type_id           (which UDF)
    UDFVALUE.fk_id       ─→ <UDFTYPE.table_name>.<pk>     (which row)
    UDFVALUE.proj_id     ─→ PROJECT.proj_id               (project scope)

Type dispatch lives in UDFTYPE.logical_data_type and selects which
UDFVALUE column carries the value:

    FT_TEXT             udf_text       VARCHAR
    FT_NUMBER           udf_number     DOUBLE
    FT_TYPE_INTEGER     udf_number     BIGINT (CAST applied)
    FT_TYPE_COST        udf_number     DOUBLE
    FT_DATE             udf_date       TIMESTAMP
    FT_START_DATE       udf_date       TIMESTAMP
    FT_END_DATE         udf_date       TIMESTAMP
    FT_TYPE_CODE        UDFCODE.udf_code_name (joined)  VARCHAR
    FT_INDICATOR        computed via formula on UDFTYPE — skipped

In all 4 fixtures with UDFs, only FT_TEXT appears. Other branches are
unverified against real data; design favors correctness in code over
empirical coverage.

Pivot column naming:
    udf_type_label is slugified (lowercase, non-alnum -> _) and
    prefixed `udf_`. Collisions disambiguated by appending `_<id>`.
    udf_type_name is intentionally ignored: P6 auto-generates it as
    `user_field_<id>` and it carries no semantic meaning.

A sidecar UDF_COLUMN_MAP table preserves the (label -> column_name)
mapping so consumers can recover the human-readable name.
"""

from __future__ import annotations

import re

import duckdb
import pyarrow as pa

from .schema import PRIMARY_KEYS

# Map logical_data_type -> (source_column_in_UDFVALUE, optional_duckdb_cast).
# `code_name` is a synthesized column from a LEFT JOIN to UDFCODE; see
# the pivot SQL below.
#
# FT_MONEY appears in real exports alongside FT_NUMBER. Both store
# decimal values in udf_number; FT_MONEY semantically distinguishes a
# currency column but the storage is identical. Treat the same.
_TYPE_DISPATCH: dict[str, tuple[str, str | None]] = {
    "FT_TEXT": ("udf_text", None),
    "FT_NUMBER": ("udf_number", None),
    "FT_MONEY": ("udf_number", None),
    "FT_FLOAT_2_DECIMALS": ("udf_number", None),
    "FT_INT": ("udf_number", "BIGINT"),
    "FT_TYPE_INTEGER": ("udf_number", "BIGINT"),
    "FT_TYPE_COST": ("udf_number", None),
    "FT_DATE": ("udf_date", None),
    "FT_START_DATE": ("udf_date", None),
    "FT_END_DATE": ("udf_date", None),
    "FT_TYPE_CODE": ("code_name", None),
    "FT_STATICTYPE": ("udf_text", None),  # enum/dropdown
}


def _slugify_label(label: str) -> str:
    """`MSP Activity ID` -> `udf_msp_activity_id`. Empty/non-alnum
    labels collapse to `udf_unnamed`."""
    s = re.sub(r"[^a-zA-Z0-9]+", "_", label.lower()).strip("_")
    return f"udf_{s}" if s else "udf_unnamed"


def _inspect_udf_storage(
    con: duckdb.DuckDBPyConnection, udf_type_id: int
) -> tuple[str, str | None]:
    """Inspect UDFVALUE for a given udf_type_id to find which value
    column actually holds data. Returns (source_column, cast_to).

    Used as a fallback when logical_data_type is not in _TYPE_DISPATCH:
    a new P6 type code we haven't catalogued still pivots correctly
    based on observed data instead of being silently dropped.

    Tie-break: udf_text wins (most permissive target). All-NULL falls
    back to udf_text and produces an all-NULL pivot column.
    """
    row = con.execute(
        "SELECT "
        "  COALESCE(SUM(CASE WHEN udf_text    IS NOT NULL THEN 1 ELSE 0 END), 0), "
        "  COALESCE(SUM(CASE WHEN udf_number  IS NOT NULL THEN 1 ELSE 0 END), 0), "
        "  COALESCE(SUM(CASE WHEN udf_date    IS NOT NULL THEN 1 ELSE 0 END), 0), "
        "  COALESCE(SUM(CASE WHEN udf_code_id IS NOT NULL THEN 1 ELSE 0 END), 0) "
        "FROM UDFVALUE WHERE udf_type_id = ?",
        [udf_type_id],
    ).fetchone()
    text, number, dt, code = row
    if code > max(text, number, dt):
        # JOIN to UDFCODE happens in the pivot SQL.
        return ("code_name", None)
    if dt > max(text, number):
        return ("udf_date", None)
    if number > text:
        return ("udf_number", None)
    return ("udf_text", None)


def _resolve_column_names(
    con: duckdb.DuckDBPyConnection,
    types: list[tuple[int, str, str]],
) -> tuple[list[tuple[int, str, str, str, str, str | None]], list[dict]]:
    """For each (udf_type_id, label, logical_data_type) triple, derive
    a unique column name and resolve the source UDFVALUE column.

    Known logical_data_types use _TYPE_DISPATCH. Unknown types fall
    back to inspecting the data via _inspect_udf_storage and are
    recorded in the second return value so the caller can surface
    them (e.g. in manifest.json).

    Returns:
      (rows, unknown_log) where rows are
      (udf_type_id, label, logical_data_type, column_name,
       source_column, cast_to)
    """
    out: list[tuple[int, str, str, str, str, str | None]] = []
    used: set[str] = set()
    unknown_log: list[dict] = []
    for udf_id, label, dtype in types:
        if dtype in _TYPE_DISPATCH:
            source, cast_to = _TYPE_DISPATCH[dtype]
        else:
            source, cast_to = _inspect_udf_storage(con, udf_id)
            unknown_log.append({
                "udf_type_id": udf_id,
                "udf_type_label": label,
                "logical_data_type": dtype,
                "inferred_source_column": source,
            })
        base = _slugify_label(label)
        col = base if base not in used else f"{base}_{udf_id}"
        used.add(col)
        out.append((udf_id, label, dtype, col, source, cast_to))
    return out, unknown_log


def _pk_for_entity(entity: str) -> str:
    """Get the single-column PK name for the entity. Falls back to
    `fk_id` if the entity isn't in PRIMARY_KEYS or has a composite PK."""
    pk = PRIMARY_KEYS.get(entity)
    if pk and len(pk) == 1:
        return pk[0]
    return "fk_id"


def _build_pivot_sql(
    entity: str,
    pk_col: str,
    cols: list[tuple[int, str, str, str, str, str | None]],
    has_udfcode: bool,
) -> str:
    """Build CREATE OR REPLACE TABLE <entity>_UDF AS ... pivot SQL."""
    code_join = (
        "LEFT JOIN UDFCODE c USING (udf_code_id)" if has_udfcode else ""
    )
    code_select = (
        "c.udf_code_name AS code_name"
        if has_udfcode
        else "CAST(NULL AS VARCHAR) AS code_name"
    )

    case_exprs: list[str] = []
    for udf_id, _, _, col_name, source_col, cast_to in cols:
        value_expr = (
            f'CAST(v."{source_col}" AS {cast_to})' if cast_to else f'v."{source_col}"'
        )
        case_exprs.append(
            f'MAX(CASE WHEN v.udf_type_id = {udf_id} THEN {value_expr} END) '
            f'AS "{col_name}"'
        )

    udf_ids = ", ".join(str(c[0]) for c in cols)
    # If pk_col == 'proj_id' (PROJECT-scoped UDFs), don't double-emit it.
    pk_select = (
        f'v.fk_id AS "{pk_col}"'
        if pk_col != "proj_id"
        else 'v.fk_id AS "proj_id"'
    )
    extra_proj = "" if pk_col == "proj_id" else ", v.proj_id"
    group_by = (
        "GROUP BY v.fk_id"
        if pk_col == "proj_id"
        else "GROUP BY v.fk_id, v.proj_id"
    )

    return (
        f'CREATE OR REPLACE TABLE "{entity}_UDF" AS '
        f"WITH val AS ( "
        f"  SELECT v.*, {code_select} "
        f"  FROM UDFVALUE v {code_join} "
        f") "
        f"SELECT {pk_select}{extra_proj}, "
        f"{', '.join(case_exprs)} "
        f"FROM val v "
        f"WHERE v.udf_type_id IN ({udf_ids}) "
        f"{group_by}"
    )


def _materialize_column_map(
    con: duckdb.DuckDBPyConnection,
    rows: list[dict],
) -> int:
    """Persist the sidecar UDF_COLUMN_MAP so consumers can recover the
    original udf_type_label for any pivot column."""
    schema = pa.schema([
        ("entity_table", pa.string()),
        ("udf_type_id", pa.int64()),
        ("udf_type_label", pa.string()),
        ("logical_data_type", pa.string()),
        ("column_name", pa.string()),
        ("wide_table", pa.string()),
    ])
    arrow = pa.Table.from_pylist(rows, schema=schema)
    con.register("udf_column_map_raw", arrow)
    try:
        con.execute(
            "CREATE OR REPLACE TABLE UDF_COLUMN_MAP AS "
            "SELECT * FROM udf_column_map_raw"
        )
    finally:
        con.unregister("udf_column_map_raw")
    return len(rows)


def materialize_udf_wide(
    con: duckdb.DuckDBPyConnection,
) -> tuple[dict[str, int], list[dict]]:
    """Pivot UDFTYPE+UDFVALUE into typed wide tables.

    Returns:
      (counts, unknown_types) where counts is a row-count map keyed by
      created table name and unknown_types is a list of records for
      UDFTYPEs whose logical_data_type wasn't in _TYPE_DISPATCH and so
      had to be inferred by inspecting UDFVALUE.

    Skips silently if UDFTYPE or UDFVALUE isn't loaded (legacy XERs
    without UDFs).
    """
    loaded = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    if not {"UDFTYPE", "UDFVALUE"}.issubset(loaded):
        return {}, []

    has_udfcode = "UDFCODE" in loaded

    # Group UDFTYPE rows by their discriminator. Filter to types that
    # have at least one UDFVALUE row in this XER (an unused UDFTYPE
    # would generate an all-NULL column).
    types_by_entity: dict[str, list[tuple[int, str, str]]] = {}
    for entity, udf_id, label, dtype in con.execute("""
        SELECT t.table_name, t.udf_type_id, t.udf_type_label, t.logical_data_type
        FROM UDFTYPE t
        WHERE t.table_name IS NOT NULL
          AND t.udf_type_label IS NOT NULL
          AND EXISTS (SELECT 1 FROM UDFVALUE v WHERE v.udf_type_id = t.udf_type_id)
    """).fetchall():
        types_by_entity.setdefault(entity, []).append((udf_id, label, dtype))

    counts: dict[str, int] = {}
    map_rows: list[dict] = []
    unknown_types: list[dict] = []
    for entity, types in types_by_entity.items():
        if entity not in loaded:
            # UDFs declared on an entity table this XER didn't export.
            continue
        cols, unknown_for_entity = _resolve_column_names(con, types)
        for u in unknown_for_entity:
            u["entity_table"] = entity
        unknown_types.extend(unknown_for_entity)
        if not cols:
            continue
        pk_col = _pk_for_entity(entity)
        wide_table = f"{entity}_UDF"
        sql = _build_pivot_sql(entity, pk_col, cols, has_udfcode)
        con.execute(sql)
        n = con.execute(f'SELECT COUNT(*) FROM "{wide_table}"').fetchone()[0]
        counts[wide_table] = n
        for udf_id, label, dtype, col_name, _src, _cast in cols:
            map_rows.append({
                "entity_table": entity,
                "udf_type_id": udf_id,
                "udf_type_label": label,
                "logical_data_type": dtype,
                "column_name": col_name,
                "wide_table": wide_table,
            })

    if map_rows:
        counts["UDF_COLUMN_MAP"] = _materialize_column_map(con, map_rows)
    return counts, unknown_types
