"""Parquet writer and manifest builder.

Writes one Parquet file per loaded DuckDB table plus a manifest.json
that captures provenance, row counts, and schema fingerprints. The
manifest is the cheap "did anything change" diff target across runs.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from .tokenizer import Header, read_header
from .validate import FkViolation


def _sha256(path: Path, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def _schema_fingerprint(con: duckdb.DuckDBPyConnection, table: str) -> str:
    """Hash of (column_name, type) tuples in declaration order. Stable
    across runs as long as DDL doesn't drift."""
    rows = con.execute(f'DESCRIBE "{table}"').fetchall()
    payload = "|".join(f"{r[0]}:{r[1]}" for r in rows)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _violation_to_dict(v: FkViolation) -> dict[str, Any]:
    return {
        "child_table": v.child_table,
        "fk_name": v.fk_name,
        "target_table": v.target_table,
        "local_fields": list(v.local_fields),
        "target_fields": list(v.target_fields),
        "kind": v.kind,
        "violating_rows": v.violating_rows,
        "sample_keys": [list(k) for k in v.sample_keys],
    }


def _sql_str_lit(s: str) -> str:
    """Single-quote a value for safe inlining into a DuckDB SQL literal."""
    return "'" + s.replace("'", "''") + "'"


def _dw_select(
    table: str,
    cols_in_table: set[str],
    src_tag: str,
    sha: str,
    export_date_lit: str,
) -> str:
    """Build the SELECT projection that adds DW columns to one table.

    Source tables (parsed from %T blocks) already carry `_raw` from
    load_xer. Derived tables (CALENDAR_WEEKLY, *_UDF, UDF_COLUMN_MAP)
    don't, so we emit `_raw = NULL` to keep every parquet's DW column
    shape uniform across the corpus.
    """
    raw_clause = (
        '"_raw"' if "_raw" in cols_in_table else "CAST(NULL AS VARCHAR) AS _raw"
    )
    base = (
        'SELECT * EXCLUDE ("_raw")'
        if "_raw" in cols_in_table
        else "SELECT *"
    )
    return (
        f'{base}, '
        f'{_sql_str_lit(src_tag)} AS source_xer, '
        f'{_sql_str_lit(sha)} AS source_sha256, '
        f'CAST((CURRENT_TIMESTAMP AT TIME ZONE \'UTC\') AS TIMESTAMP) '
        f'AS flow_published_at, '
        f'{export_date_lit} AS exported_at, '
        f'{raw_clause} '
        f'FROM "{table}"'
    )


def write_parquet(
    con: duckdb.DuckDBPyConnection,
    out_dir: Path,
    source_xer: Path,
    counts: dict[str, int],
    violations: list[FkViolation] | None = None,
    udf_unknown_types: list[dict] | None = None,
    add_dw_columns: bool = False,
) -> dict[str, Any]:
    """Write each DuckDB table to a Parquet file in out_dir, plus manifest.json.

    Returns the manifest dict for inspection in callers/tests. If
    violations is None, the manifest's "fk_violations" key is omitted
    (validation was not requested for this run). udf_unknown_types is
    only emitted when non-empty.

    When add_dw_columns is True, every emitted parquet gets data-
    warehouse columns suitable for cross-XER joins:

      source_xer         basename of the input file (no extension)
      source_sha256      hex digest matching the manifest entry
      flow_published_at  TIMESTAMP at COPY time (ingestion clock)
      exported_at         TIMESTAMP from ERMHDR export_date
      _raw               reconstructed `%R\\t...` line per row
                         (added in load_xer; only on source tables —
                         derived tables like CALENDAR_WEEKLY do not
                         have an upstream %R line).

    The natural cross-XER join key becomes (source_xer, <p6_id>) since
    P6 IDs are stable inside one export but collide across exports.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    header: Header = read_header(source_xer)
    sha = _sha256(source_xer)
    src_tag = source_xer.stem
    export_date_lit = (
        f"CAST({_sql_str_lit(header.export_date)} AS TIMESTAMP)"
        if header.export_date
        else "CAST(NULL AS TIMESTAMP)"
    )

    table_entries: list[dict[str, Any]] = []
    for table in sorted(counts):
        parquet_path = out_dir / f"{table}.parquet"
        if add_dw_columns:
            cols_in_table = {
                r[0] for r in con.execute(f'DESCRIBE "{table}"').fetchall()
            }
            select_sql = _dw_select(
                table, cols_in_table, src_tag, sha, export_date_lit
            )
            con.execute(
                f"COPY ({select_sql}) TO '{parquet_path}' "
                "(FORMAT PARQUET, COMPRESSION ZSTD)"
            )
        else:
            con.execute(
                f"COPY \"{table}\" TO '{parquet_path}' "
                "(FORMAT PARQUET, COMPRESSION ZSTD)"
            )
        table_entries.append(
            {
                "name": table,
                "rows": counts[table],
                "parquet": parquet_path.name,
                "schema_fingerprint": _schema_fingerprint(con, table),
            }
        )

    manifest: dict[str, Any] = {
        "source_xer": str(source_xer.resolve()),
        "source_sha256": _sha256(source_xer),
        "parsed_at": datetime.now(tz=UTC).isoformat(),
        "p6_version": header.version,
        "export_date": header.export_date,
        "project_label": header.project_label,
        "tables": table_entries,
    }
    if violations is not None:
        manifest["fk_violations"] = [_violation_to_dict(v) for v in violations]
    if udf_unknown_types:
        manifest["udf_unknown_types"] = udf_unknown_types
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest
