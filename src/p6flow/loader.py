"""Load parsed XER tables into a DuckDB connection.

Pipeline:
    1. Materialize all tables (rows in memory, since type inference and
       Arrow registration both need the full row set).
    2. Resolve type overrides for unknown columns via p6flow.inference.
       Fail fast if naming heuristic and value sniffing disagree.
    3. Topo-sort tables so FK parents are inserted before children.
    4. CREATE OR REPLACE TABLE for each one with constraints from schema.py.
    5. INSERT data with per-column CAST(NULLIF(col, '') AS <type>) so empty
       XER cells become SQL NULL.

Returns a per-table row count map for downstream validation.
"""

from __future__ import annotations

import duckdb
import pyarrow as pa

from .ddl import duckdb_ddl, topo_sort_tables
from .inference import resolve_overrides
from .schema import COLUMN_TYPES
from .tokenizer import Table  # parse_tables imported lazily by callers


class RowArityMismatch(ValueError):
    """A `%R` row had MORE fields than `%F` declared. Format-level violation."""


def _normalize_rows(
    table: str,
    fields: list[str],
    rows: list[list[str]],
) -> list[list[str]]:
    """Force every row to len(fields). Pad short rows with '', drop
    trailing empty cells before checking arity, error on real overflow.

    Short rows are common in P6 exports (trailing optional fields dropped).
    Some exporters also emit a trailing tab on every row, producing an
    extra empty cell that's not a real field. We tolerate that quietly
    and only raise when extra cells carry actual data.
    """
    n = len(fields)
    out: list[list[str]] = []
    for i, row in enumerate(rows):
        # Drop trailing empties before arity check; they're an exporter
        # artifact, not real data.
        while len(row) > n and row[-1] == "":
            row = row[:-1]
        if len(row) > n:
            raise RowArityMismatch(
                f"{table} row {i}: {len(row)} fields but %F declared {n}; "
                f"extra cells: {row[n:]!r}"
            )
        if len(row) < n:
            row = row + [""] * (n - len(row))
        out.append(row)
    return out


def _materialize(
    tables: list[Table],
) -> list[tuple[str, list[str], list[list[str]], list[str]]]:
    """Snapshot each parsed table into (name, fields, rows, raws) tuples
    used by downstream type inference, Arrow registration, and the DW
    column path. Trailing empty cells are dropped from rows but raws is
    left untouched (its job is byte-faithful capture)."""
    out = []
    for t in tables:
        rows = _normalize_rows(t.name, t.fields, list(t.rows))
        out.append((t.name, t.fields, rows, list(t.raws)))
    return out


def _cast_select(table: str, fields: list[str], overrides) -> str:
    """Build a SELECT list that casts each string column to its target type.

    Uses NULLIF(col, '') to convert XER's empty-string convention to SQL NULL.
    The cast type comes from per-XER overrides first, then schema.py, with
    VARCHAR as final fallback.
    """
    parts: list[str] = []
    for f in fields:
        spec = None
        if overrides and (table, f) in overrides:
            spec = overrides[(table, f)]
        if spec is None:
            spec = COLUMN_TYPES.get(table, {}).get(f)
        ddb_type = spec.duckdb_type if spec else "VARCHAR"
        if ddb_type == "VARCHAR" or ddb_type.startswith("VARCHAR("):
            # No cast needed; just NULLIF empty strings. Don't NULLIF
            # 'null' because a VARCHAR column might legitimately hold
            # the literal string "null" (e.g. notes/descriptions).
            parts.append(f'NULLIF("{f}", \'\') AS "{f}"')
        elif ddb_type == "BLOB":
            # CAST(varchar AS BLOB) interprets the string as hex-escaped
            # bytes and rejects raw non-ASCII (e.g. UTF-8 BOM survivors,
            # curly quotes in description fields). encode() takes the
            # string's UTF-8 byte representation directly.
            parts.append(f'encode(NULLIF("{f}", \'\')) AS "{f}"')
        else:
            # Typed casts (BIGINT/DOUBLE/TIMESTAMP/etc.) must treat both
            # '' and the literal string 'null'/'NULL' as SQL NULL. Some
            # P6 exporters emit the string "null" in lieu of empty cells
            # for missing numerics; without this, the cast fails.
            parts.append(
                f"CAST(NULLIF(NULLIF(LOWER(\"{f}\"), 'null'), '') "
                f"AS {ddb_type}) AS \"{f}\""
            )
    return ", ".join(parts)


def load_xer(
    con: duckdb.DuckDBPyConnection,
    tables: list[Table],
    add_dw_columns: bool = False,
) -> dict[str, int]:
    """Load every parsed table into the DuckDB connection. Return row counts per table.

    When add_dw_columns=True, every loaded table gains an `_raw` VARCHAR
    column containing the byte-faithful `%R\\t...` line captured by the
    tokenizer (newline-stripped). Used by the warehouse path so
    consumers can recover the exact source line of any parsed row for
    debugging. Other DW columns (source_xer, source_sha256,
    flow_published_at, exported_at) are constants per file and get
    added at COPY time in output.write_parquet.
    """
    materialized = _materialize(tables)
    rows_by_name = {t: (fs, rs, raws) for t, fs, rs, raws in materialized}
    available = {t for t, _, _, _ in materialized}

    # Type inference for unknown columns. Fails fast on conflicting evidence.
    # resolve_overrides only needs (name, fields, row_iter); raws are unused.
    overrides = resolve_overrides(
        [(t, fs, rs) for t, fs, rs, _ in materialized]
    )

    # Topo sort by FK dependencies (parents first; DuckDB enforces FKs at INSERT).
    ordered_pairs = topo_sort_tables([(t, fs) for t, fs, _, _ in materialized])

    counts: dict[str, int] = {}
    for table, fields in ordered_pairs:
        _, rows, raws = rows_by_name[table]

        # An empty %T block (zero columns, zero rows) is a real XER
        # quirk; treat it as "not present in this export" rather than
        # creating a column-less table that DuckDB rejects.
        if not fields:
            continue

        emit_fields = list(fields)
        emit_rows = rows
        if add_dw_columns:
            # Append _raw alongside the parsed cells using the raw line
            # captured at tokenization time. VARCHAR by default since
            # it isn't in COLUMN_TYPES.
            #
            # Defensive: if for some reason raws is shorter than rows
            # (shouldn't happen in well-formed XERs, but guard against
            # future tokenizer changes), fall back to empty string.
            emit_fields = [*fields, "_raw"]
            emit_rows = [
                [*r, raws[i] if i < len(raws) else ""]
                for i, r in enumerate(rows)
            ]

        # CREATE OR REPLACE the staging table; only emit FKs to tables we'll create.
        con.execute(
            duckdb_ddl(table, emit_fields, overrides, available_tables=available)
        )

        if not emit_rows:
            counts[table] = 0
            continue

        # Pivot rows into columns, build an Arrow table of all-string cells,
        # register it, INSERT with casts.
        cols = list(zip(*emit_rows, strict=True))
        arrow_table = pa.table({f: list(cols[i]) for i, f in enumerate(emit_fields)})
        con.register("xer_raw", arrow_table)
        try:
            select_list = _cast_select(table, emit_fields, overrides)
            con.execute(f"INSERT INTO {table} SELECT {select_list} FROM xer_raw")
        finally:
            con.unregister("xer_raw")

        counts[table] = len(emit_rows)
    return counts
