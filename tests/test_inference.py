"""Type inference unit tests.

The absorption matrix in infer_unknown is the heart of the legacy-XER
recovery story. A regression here mistypes columns silently. These
tests pin every cell of the (heuristic, sniff) decision matrix plus
the boundary cases (empty values, no heuristic match, conflict).
"""

from __future__ import annotations

import pytest

from p6flow.inference import (
    TypeMismatch,
    heuristic_bucket,
    infer_unknown,
    resolve_overrides,
    sniff_bucket,
)

# ---- heuristic_bucket --------------------------------------------


@pytest.mark.parametrize(
    "field,expected",
    [
        # Compound suffix wins over its tail (_pct_type -> string, NOT double).
        ("complete_pct_type", "string"),
        # Plain _pct still maps to double.
        ("complete_pct", "double"),
        # _hr_cnt is double, NOT integer (overrides _cnt).
        ("total_float_hr_cnt", "double"),
        # Plain _cnt is integer.
        ("task_cnt", "integer"),
        # Common ID/sequence columns.
        ("task_id", "integer"),
        ("seq_num", "integer"),
        # Money / quantity / rate / weight / value / cost / duration -> double.
        ("base_qty", "double"),
        ("target_rate", "double"),
        ("relation_wt", "double"),
        ("act_value", "double"),
        ("budgt_cost", "double"),
        ("act_drtn", "double"),
        # Dates.
        ("plan_start_date", "date"),
        # Flags.
        ("locked_flag", "flag"),
        # String-y suffixes.
        ("clndr_type", "string"),
        ("user_field_name", "string"),
        ("audit_user", "string"),
        ("file_path", "string"),
        ("project_state", "string"),
        ("row_guid", "string"),
        # No matching suffix.
        ("freeform", None),
        # Bare suffix as the entire name (matches the `field == suffix.lstrip('_')`
        # branch).
        ("date", "date"),
    ],
)
def test_heuristic_bucket(field, expected):
    assert heuristic_bucket(field) == expected


# ---- sniff_bucket -------------------------------------------------


@pytest.mark.parametrize(
    "values,expected",
    [
        # Pure integers.
        (["1", "2", "100"], "integer"),
        # Floats with decimal points.
        (["1.5", "2.0", "3.14"], "double"),
        # Mixed int and float -> all parse as float, so double (sniff doesn't
        # know there's no fractional info loss).
        (["1", "2.5"], "double"),
        # Y/N flags.
        (["Y", "N", "Y"], "flag"),
        # ISO dates.
        (["2025-01-01", "2026-05-06"], "date"),
        # Date with time.
        (["2025-01-01 09:30", "2026-05-06 18:45:00"], "date"),
        # String fallback.
        (["Hello", "World"], "string"),
        # Mixed digit and string -> string (digit count breaks at "Foo").
        (["123", "Foo"], "string"),
        # All empty -> None (no signal).
        (["", "", ""], None),
        # Mostly empty, one int -> integer (sniff ignores empty cells).
        (["", "", "42"], "integer"),
    ],
)
def test_sniff_bucket(values, expected):
    assert sniff_bucket(values) == expected


def test_sniff_bucket_respects_max_samples():
    """Sniffing 10K values with max_samples=5 only inspects the first 5."""
    values = ["1"] * 5 + ["NotAnInt"] * 9995
    assert sniff_bucket(values, max_samples=5) == "integer"


# ---- infer_unknown absorption matrix -----------------------------


# Each row is a (heuristic_bucket, sniff_bucket, expected_outcome) cell.
# expected_outcome is either a bucket name (resolves cleanly) or
# TypeMismatch (raises).
_ABSORPTION_CELLS = [
    # heuristic=string absorbs everything (string wins on disagreement)
    ("clndr_type",  ["abc"],          "string"),    # string + string -> string
    ("clndr_type",  ["1"],            "string"),    # string + integer -> string
    ("clndr_type",  ["1.5"],          "string"),    # string + double  -> string
    ("clndr_type",  ["Y"],            "string"),    # string + flag    -> string
    ("clndr_type",  ["2025-01-01"],   "string"),    # string + date    -> string
    # heuristic=double absorbs integer (ints fit in doubles, no loss)
    ("base_qty",    ["1.5"],          "double"),
    ("base_qty",    ["1", "2"],       "double"),    # heuristic wins, stays double
    # heuristic=double does NOT absorb string/flag/date
    ("base_qty",    ["abc"],          TypeMismatch),
    ("base_qty",    ["Y"],            TypeMismatch),
    ("base_qty",    ["2025-01-01"],   TypeMismatch),
    # heuristic=integer is strict — only absorbs integer
    ("task_cnt",    ["1"],            "integer"),
    ("task_cnt",    ["1.5"],          TypeMismatch),  # int can't hold float
    ("task_cnt",    ["abc"],          TypeMismatch),
    # heuristic=flag is strict
    ("locked_flag", ["Y"],            "flag"),
    ("locked_flag", ["abc"],          TypeMismatch),
    # heuristic=date is strict
    ("start_date",  ["2025-01-01"],   "date"),
    ("start_date",  ["abc"],          TypeMismatch),
]


@pytest.mark.parametrize("field,values,expected", _ABSORPTION_CELLS)
def test_absorption_matrix(field, values, expected):
    if expected is TypeMismatch:
        with pytest.raises(TypeMismatch):
            infer_unknown("X", field, values)
    else:
        spec = infer_unknown("X", field, values)
        bucket_to_duckdb = {
            "string": "VARCHAR",
            "integer": "BIGINT",
            "double": "DOUBLE",
            "flag": "VARCHAR(1)",
            "date": "TIMESTAMP",
        }
        assert spec.duckdb_type == bucket_to_duckdb[expected]


def test_no_heuristic_uses_sniff():
    """No suffix opinion -> sniff result is taken as-is."""
    spec = infer_unknown("X", "freeform", ["1", "2", "3"])
    assert spec.duckdb_type == "BIGINT"


def test_no_sniff_uses_heuristic():
    """All-empty column with a heuristic -> heuristic wins, no error."""
    spec = infer_unknown("X", "task_cnt", ["", "", ""])
    assert spec.duckdb_type == "BIGINT"


def test_no_evidence_falls_back_to_string():
    """No suffix match AND all empty -> string fallback."""
    spec = infer_unknown("X", "freeform", ["", ""])
    assert spec.duckdb_type == "VARCHAR"


# ---- resolve_overrides --------------------------------------------


def test_resolve_overrides_skips_known_columns():
    """Columns already in COLUMN_TYPES are not in the overrides dict."""
    blocks = [("ACCOUNT", ["acct_id", "acct_name"], iter([["1", "Foo"]]))]
    assert resolve_overrides(blocks) == {}


def test_resolve_overrides_picks_up_unknown_columns():
    """An unknown column gets typed via heuristic+sniff."""
    blocks = [("ACCOUNT", ["acct_id", "made_up_cnt"], iter([["1", "5"], ["2", "9"]]))]
    overrides = resolve_overrides(blocks)
    assert ("ACCOUNT", "made_up_cnt") in overrides
    assert overrides[("ACCOUNT", "made_up_cnt")].duckdb_type == "BIGINT"


def test_resolve_overrides_fails_fast_on_conflict():
    """Conflicting evidence raises TypeMismatch and stops, so the operator
    notices instead of getting silently mis-typed data."""
    blocks = [
        ("ACCOUNT", ["acct_id", "made_up_cnt"], iter([["1", "abc"]])),  # cnt + string
    ]
    with pytest.raises(TypeMismatch, match="made_up_cnt"):
        resolve_overrides(blocks)
