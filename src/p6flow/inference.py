"""Type resolution for XER columns.

Three-tier fallback:

    1. pmSchema.xml (via p6flow.schema.COLUMN_TYPES). If known, use that.
    2. Naming heuristic + value sniffing must agree. Raise on mismatch.
    3. If neither has an opinion, fall back to VARCHAR.

Tier 2 only runs for columns absent from pmSchema.xml. The heuristic alone
is fast but wrong for the long tail; the sniffer alone has small-sample
ambiguity (an all-zeros column could be int or double). Requiring both to
agree catches confidently-wrong inferences and surfaces them as errors.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from .schema import COLUMN_TYPES, ColumnSpec

# Order matters: longer/more specific suffixes first.
_SUFFIX_BUCKETS: tuple[tuple[str, str], ...] = (
    # Compound suffixes that override their tail
    ("_pct_type", "string"),
    ("_hr_cnt", "double"),
    # Integers
    ("_id", "integer"),
    ("_cnt", "integer"),
    ("_num", "integer"),
    ("_seq", "integer"),
    # Doubles
    ("_pct", "double"),
    ("_qty", "double"),
    ("_rate", "double"),
    ("_wt", "double"),
    ("_value", "double"),
    ("_cost", "double"),
    ("_drtn", "double"),  # duration
    # Dates
    ("_date", "date"),
    # Flags / single-char codes
    ("_flag", "flag"),
    # Strings
    ("_type", "string"),
    ("_code", "string"),
    ("_name", "string"),
    ("_user", "string"),
    ("_short_name", "string"),
    ("_path", "string"),
    ("_state", "string"),
    ("_guid", "string"),
)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}( \d{2}:\d{2}(:\d{2})?)?$")

_BUCKET_TO_DUCKDB = {
    "integer": "BIGINT",
    "double": "DOUBLE",
    "date": "TIMESTAMP",
    "flag": "VARCHAR(1)",
    "string": "VARCHAR",
}
_BUCKET_TO_SNOWFLAKE = {
    "integer": "NUMBER(19)",
    "double": "FLOAT",
    "date": "TIMESTAMP_NTZ",
    "flag": "VARCHAR(1)",
    "string": "VARCHAR",
}


class TypeMismatch(Exception):
    """Naming heuristic and value sniffing disagree on a column's type."""


def heuristic_bucket(field: str) -> str | None:
    """Return a type bucket from the column name suffix, or None."""
    for suffix, bucket in _SUFFIX_BUCKETS:
        if field == suffix.lstrip("_") or field.endswith(suffix):
            return bucket
    return None


def sniff_bucket(values: Iterable[str], max_samples: int = 1000) -> str | None:
    """Return a type bucket from observed values, or None if all empty.

    Samples up to max_samples non-empty values to avoid O(n) scans of huge tables.
    """
    non_empty: list[str] = []
    for v in values:
        if v != "" and v is not None:
            non_empty.append(v)
            if len(non_empty) >= max_samples:
                break
    if not non_empty:
        return None

    # Date check first; "2025-01-01" parses as nothing else useful.
    if all(_DATE_RE.match(v) for v in non_empty):
        return "date"
    # Y/N flags
    if all(v in ("Y", "N") for v in non_empty):
        return "flag"
    # Integers (no decimal point, parseable)
    if all(_is_int(v) for v in non_empty):
        return "integer"
    # Floats (parseable as float, possibly with decimal)
    if all(_is_float(v) for v in non_empty):
        return "double"
    return "string"


def _is_int(v: str) -> bool:
    try:
        int(v)
        return True
    except ValueError:
        return False


def _is_float(v: str) -> bool:
    try:
        float(v)
        return True
    except ValueError:
        return False


# What each predicted bucket can cleanly hold. Asymmetric: a column predicted
# `double` accepts integer-valued data (ints fit in doubles), but a column
# predicted `integer` does NOT accept floating-point data (would lose precision).
_ABSORBS: dict[str, frozenset[str]] = {
    "string": frozenset({"string", "flag", "integer", "double", "date"}),
    "double": frozenset({"double", "integer"}),
    "integer": frozenset({"integer"}),
    "flag": frozenset({"flag"}),
    "date": frozenset({"date"}),
}


def infer_unknown(table: str, field: str, values: Iterable[str]) -> ColumnSpec:
    """Resolve type for a column NOT in COLUMN_TYPES.

    Raises TypeMismatch if the naming heuristic and value sniffing disagree
    in a way that would lose information. Compatible disagreements (naming
    says double but data is integer) resolve to the wider type. If neither
    has an opinion (no suffix match, all values empty), default to string.
    """
    h = heuristic_bucket(field)
    s = sniff_bucket(values)
    if h is None and s is None:
        bucket = "string"
    elif h is None:
        bucket = s
    elif s is None:
        bucket = h
    elif s in _ABSORBS.get(h, frozenset()):
        bucket = h
    else:
        raise TypeMismatch(
            f"{table}.{field}: naming says {h!r}, sniffing says {s!r}"
        )
    return ColumnSpec(
        duckdb_type=_BUCKET_TO_DUCKDB[bucket],
        snowflake_type=_BUCKET_TO_SNOWFLAKE[bucket],
        nullable=True,
        description="",
    )


def resolve_overrides(
    blocks: Iterable[tuple[str, list[str], Iterable[list[str]]]],
) -> dict[tuple[str, str], ColumnSpec]:
    """Build per-XER type overrides for every column not in COLUMN_TYPES.

    blocks: iterable of (table_name, field_names, row_iter) where each row is
    a list of string cell values aligned with field_names.

    Returns: {(table, field): ColumnSpec} for every unknown column. Raises
    TypeMismatch on the first disagreement and stops; this is intentional
    fail-fast behavior so the caller learns about new ambiguous columns
    instead of silently mis-typing them.
    """
    overrides: dict[tuple[str, str], ColumnSpec] = {}
    for table, fields, rows in blocks:
        unknown_idx = [
            (i, f)
            for i, f in enumerate(fields)
            if f not in COLUMN_TYPES.get(table, {})
        ]
        if not unknown_idx:
            continue
        # Materialize columns we care about.
        cols: dict[int, list[str]] = {i: [] for i, _ in unknown_idx}
        for row in rows:
            for i in cols:
                if i < len(row):
                    cols[i].append(row[i])
        for i, f in unknown_idx:
            if (table, f) in overrides:
                continue
            overrides[(table, f)] = infer_unknown(table, f, cols[i])
    return overrides
