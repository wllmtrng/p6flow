"""Runtime DDL generation.

The codegen module p6flow.schema provides every column EPPM knows about
at a given version. This module reads the actual %F field list from a parsed
XER block and emits DDL using only those columns, with types looked up from
COLUMN_TYPES and a VARCHAR fallback for anything the schema doesn't know about
(legacy P6 columns, Oracle-removed columns, etc.).

Two flavors:

    duckdb_ddl(table, fields)     for the local staging DB
    snowflake_ddl(table, fields)  for the remote target

Both return a single SQL string. PRIMARY KEY constraints are included when
every PK column is present in the field list. FOREIGN KEY constraints are
included when both the local and target columns are present. Self-FKs are
skipped because DuckDB enforces FKs at INSERT and offers no deferred mode.
"""

from __future__ import annotations

from .schema import COLUMN_TYPES, FOREIGN_KEYS, PRIMARY_KEYS, ColumnSpec

VARCHAR_FALLBACK = ColumnSpec(
    duckdb_type="VARCHAR",
    snowflake_type="VARCHAR",
    nullable=True,
    description="",
)

OverrideMap = dict[tuple[str, str], ColumnSpec]


def _spec(table: str, field: str, overrides: OverrideMap | None = None) -> ColumnSpec:
    if overrides and (table, field) in overrides:
        return overrides[(table, field)]
    return COLUMN_TYPES.get(table, {}).get(field, VARCHAR_FALLBACK)


def _sql_quote(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "''")


def _common_constraints(
    table: str,
    fields: list[str],
    available_tables: set[str] | None = None,
) -> list[str]:
    """Constraints both DDL flavors agree on (text only).

    `available_tables` filters FK references: only emit a FK if its target
    table is actually being loaded. Without this filter, DDL would reference
    parent tables that don't exist in DuckDB and the CREATE would fail.
    """
    field_set = set(fields)
    out: list[str] = []
    pk = PRIMARY_KEYS.get(table)
    if pk and all(c in field_set for c in pk):
        out.append(
            f"CONSTRAINT pk_{table.lower()} PRIMARY KEY ({', '.join(pk)})"
        )
    for fk in FOREIGN_KEYS.get(table, []):
        if fk.target_table == table:
            continue  # skip self-FKs
        if available_tables is not None and fk.target_table not in available_tables:
            continue
        if not all(c in field_set for c in fk.local_fields):
            continue
        target_cols = COLUMN_TYPES.get(fk.target_table, {})
        if not all(c in target_cols for c in fk.target_fields):
            continue
        out.append(
            f"CONSTRAINT {fk.name} FOREIGN KEY "
            f"({', '.join(fk.local_fields)}) REFERENCES "
            f"{fk.target_table} ({', '.join(fk.target_fields)})"
        )
    return out


def duckdb_ddl(
    table: str,
    fields: list[str],
    overrides: OverrideMap | None = None,
    available_tables: set[str] | None = None,
    enforce_fks: bool = False,
    enforce_not_null: bool = False,
) -> str:
    """CREATE TABLE + COMMENT ON statements for DuckDB staging.

    Defaults are permissive because XER is a partial export, not a self-
    contained snapshot. Real XERs routinely reference projects, calendars,
    and resources that aren't included in the export. NOT NULL is also
    relaxed because some columns marked NOT NULL in the v26 schema are
    optional in older P6 versions and arrive empty.

    FK validation belongs in a downstream SELECT-based step that reports
    violations as warnings, not as DDL constraints that block load.
    """
    col_lines: list[str] = []
    for f in fields:
        s = _spec(table, f, overrides)
        null_clause = " NOT NULL" if (enforce_not_null and not s.nullable) else ""
        # Quote every column name. Real XERs occasionally have malformed
        # %F lines where two field names get joined with a space (e.g.
        # "create_user update_user"); without quoting, that emits invalid
        # SQL. Quoting also future-proofs against names that collide with
        # SQL reserved words.
        col_lines.append(f'    "{f}" {s.duckdb_type}{null_clause}')
    constraints = _common_constraints(table, fields, available_tables)
    if not enforce_fks:
        constraints = [c for c in constraints if c.startswith("CONSTRAINT pk_")]
    col_lines.extend(f"    {c}" for c in constraints)

    out: list[str] = [
        f"CREATE OR REPLACE TABLE {table} (",
        ",\n".join(col_lines),
        ");",
    ]
    for f in fields:
        s = _spec(table, f, overrides)
        if s.description:
            out.append(
                f"COMMENT ON COLUMN {table}.\"{f}\" IS "
                f"'{_sql_quote(s.description)}';"
            )
    return "\n".join(out)


def snowflake_ddl(
    table: str,
    fields: list[str],
    overrides: OverrideMap | None = None,
    available_tables: set[str] | None = None,
) -> str:
    """CREATE TABLE with inline COMMENT clauses for Snowflake."""
    col_lines: list[str] = []
    for f in fields:
        s = _spec(table, f, overrides)
        null_clause = "" if s.nullable else " NOT NULL"
        comment = (
            f" COMMENT '{_sql_quote(s.description)}'" if s.description else ""
        )
        col_lines.append(f'    "{f}" {s.snowflake_type}{null_clause}{comment}')
    col_lines.extend(
        f"    {c}" for c in _common_constraints(table, fields, available_tables)
    )
    return "\n".join(
        [
            f"CREATE OR REPLACE TABLE {table} (",
            ",\n".join(col_lines),
            ");",
        ]
    )


def topo_sort_tables(blocks: list[tuple[str, list[str]]]) -> list[tuple[str, list[str]]]:
    """Order parsed blocks so every FK parent appears before its children.

    DuckDB needs this since it enforces FKs at INSERT time. blocks is a list
    of (table_name, %F field list). Returns the same items reordered.
    """
    by_name = {t: (t, fs) for t, fs in blocks}
    seen: set[str] = set()
    visiting: set[str] = set()
    out: list[tuple[str, list[str]]] = []

    def visit(name: str) -> None:
        if name in seen or name not in by_name:
            return
        if name in visiting:
            return  # cycle; let caller proceed
        visiting.add(name)
        for fk in FOREIGN_KEYS.get(name, []):
            if fk.target_table != name:
                visit(fk.target_table)
        visiting.discard(name)
        seen.add(name)
        out.append(by_name[name])

    for t, _ in blocks:
        visit(t)
    return out
