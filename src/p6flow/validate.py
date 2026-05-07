"""Foreign-key validation as a downstream SELECT pass.

XER is a partial export: a child row's parent may live in a different
file or the same file at a different point in time. The DDL pipeline
intentionally *declares* FKs without enforcing them at INSERT
(loader.py drops INSERT-time FK checks via DuckDB's lack of deferred
mode + only emitting FKs whose target is also loaded). This module
runs the deferred check after the load completes, returns violations,
and lets the caller decide whether to warn, fail, or persist a report.

Two violation kinds are reported:

  - "orphan":              child row has a non-null FK value whose
                           referenced parent row is absent. Standard
                           SQL FK semantics.

  - "missing_required_fk": child row has NULL in an FK column that
                           pmSchema.xml declared NOT NULL. Schema-
                           invariant violation, distinct from orphan.

NULL in a nullable FK column is NOT a violation (XER convention:
NULL = "no parent applicable", e.g. root WBS has parent_wbs_id NULL).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import duckdb

from .schema import COLUMN_TYPES, FOREIGN_KEYS, ForeignKey

ViolationKind = Literal["orphan", "missing_required_fk"]


@dataclass(frozen=True)
class FkViolation:
    """One foreign-key relation that failed validation.

    A single FK relation can produce up to two FkViolation records if
    it has both orphaned rows and rows with NULL in a non-nullable FK
    column. Aggregated per (child_table, fk_name, kind); we count
    violating rows and capture a small sample of offending key values
    for inspection.
    """

    child_table: str
    fk_name: str
    target_table: str
    local_fields: tuple[str, ...]
    target_fields: tuple[str, ...]
    kind: ViolationKind
    violating_rows: int
    sample_keys: tuple[tuple[str, ...], ...]


def _required_local_fields(child_table: str, fk: ForeignKey) -> tuple[str, ...]:
    """Subset of fk.local_fields whose ColumnSpec is non-nullable.

    Columns absent from COLUMN_TYPES (legacy XER columns not in
    pmSchema.xml) have unknown nullability and are excluded — we can't
    assert "must not be NULL" without a schema declaration backing it.
    """
    specs = COLUMN_TYPES.get(child_table, {})
    return tuple(f for f in fk.local_fields if f in specs and not specs[f].nullable)


def _loaded_tables(con: duckdb.DuckDBPyConnection) -> set[str]:
    return {r[0] for r in con.execute("SHOW TABLES").fetchall()}


def _columns_of(con: duckdb.DuckDBPyConnection, table: str) -> set[str]:
    return {r[0] for r in con.execute(f'DESCRIBE "{table}"').fetchall()}


def _fetch_samples(
    con: duckdb.DuckDBPyConnection, sql: str
) -> tuple[tuple[str, ...], ...]:
    return tuple(tuple(str(c) for c in row) for row in con.execute(sql).fetchall())


def _check_orphans(
    con: duckdb.DuckDBPyConnection,
    child_table: str,
    fk: ForeignKey,
    sample_limit: int,
) -> FkViolation | None:
    """Case A: child rows with non-null FK values pointing nowhere.

    LEFT JOIN ... WHERE parent.key IS NULL pattern. Excludes rows with
    any NULL local field (those are Case B legit roots or Case C
    schema violations, handled separately).
    """
    local, target = fk.local_fields, fk.target_fields
    join_pred = " AND ".join(
        f'c."{lf}" = p."{tf}"' for lf, tf in zip(local, target, strict=True)
    )
    not_null_pred = " AND ".join(f'c."{lf}" IS NOT NULL' for lf in local)
    # All target columns are NULL after a missed LEFT JOIN; checking
    # the first one suffices and lets DuckDB use any column for the probe.
    where = f"{not_null_pred} AND p.\"{target[0]}\" IS NULL"

    base_from = (
        f'FROM "{child_table}" c '
        f'LEFT JOIN "{fk.target_table}" p ON {join_pred} '
        f'WHERE {where}'
    )
    n = con.execute(f"SELECT COUNT(*) {base_from}").fetchone()[0]
    if n == 0:
        return None

    sample_cols = ", ".join(f'CAST(c."{lf}" AS VARCHAR)' for lf in local)
    samples = _fetch_samples(
        con,
        f"SELECT DISTINCT {sample_cols} {base_from} LIMIT {sample_limit}",
    )
    return FkViolation(
        child_table=child_table,
        fk_name=fk.name,
        target_table=fk.target_table,
        local_fields=local,
        target_fields=target,
        kind="orphan",
        violating_rows=n,
        sample_keys=samples,
    )


def _check_missing_required(
    con: duckdb.DuckDBPyConnection,
    child_table: str,
    fk: ForeignKey,
    required_fields: tuple[str, ...],
    sample_limit: int,
) -> FkViolation | None:
    """Case C: child rows with NULL in a declared-NOT-NULL FK column."""
    null_pred = " OR ".join(f'"{f}" IS NULL' for f in required_fields)
    where = f"({null_pred})"

    n = con.execute(
        f'SELECT COUNT(*) FROM "{child_table}" WHERE {where}'
    ).fetchone()[0]
    if n == 0:
        return None

    sample_cols = ", ".join(
        f"COALESCE(CAST(\"{f}\" AS VARCHAR), '<NULL>')" for f in fk.local_fields
    )
    samples = _fetch_samples(
        con,
        f'SELECT DISTINCT {sample_cols} FROM "{child_table}" '
        f"WHERE {where} LIMIT {sample_limit}",
    )
    return FkViolation(
        child_table=child_table,
        fk_name=fk.name,
        target_table=fk.target_table,
        local_fields=fk.local_fields,
        target_fields=fk.target_fields,
        kind="missing_required_fk",
        violating_rows=n,
        sample_keys=samples,
    )


def validate_fks(
    con: duckdb.DuckDBPyConnection,
    sample_limit: int = 5,
) -> list[FkViolation]:
    """Walk every declared FK, return all violations across both kinds.

    A single FK relation can contribute up to two FkViolation entries
    (one orphan, one missing-required) when both conditions trip.

    Skips relations whose child/parent table isn't loaded, OR whose FK
    columns aren't actually emitted in this XER. Mirrors DDL emission
    policy in ddl.py: legacy XER versions drop columns the schema still
    declares; treating that as a validator error would be wrong.
    """
    loaded = _loaded_tables(con)
    columns_cache: dict[str, set[str]] = {}

    def cols(t: str) -> set[str]:
        if t not in columns_cache:
            columns_cache[t] = _columns_of(con, t)
        return columns_cache[t]

    violations: list[FkViolation] = []
    for child_table, fks in FOREIGN_KEYS.items():
        if child_table not in loaded:
            continue
        for fk in fks:
            if fk.target_table not in loaded:
                continue
            if not set(fk.local_fields).issubset(cols(child_table)):
                continue
            if not set(fk.target_fields).issubset(cols(fk.target_table)):
                continue

            orphan = _check_orphans(con, child_table, fk, sample_limit)
            if orphan is not None:
                violations.append(orphan)

            required = _required_local_fields(child_table, fk)
            if required:
                missing = _check_missing_required(
                    con, child_table, fk, required, sample_limit
                )
                if missing is not None:
                    violations.append(missing)
    return violations
