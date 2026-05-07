"""Batch-process every .xer under a source directory tree, preserving
the directory hierarchy under --out.

For each input XER:
  1. Run the in-process parse pipeline (parse_tables -> load_xer ->
     materialize_calendar_derived -> materialize_udf_wide -> validate_fks
     -> write_parquet).
  2. Validate row counts against the source by re-tokenizing.
  3. Record manifest summary, FK violations, unknown UDF types, and
     any exception in a per-file row of an aggregate report.

Output:
  <out>/<relative-path-without-extension>/<table>.parquet
  <out>/<relative-path-without-extension>/manifest.json
  <out>/_batch_report.json   <- aggregate row per source file
  <out>/_batch_report.csv    <- same, flat CSV for quick scanning

Usage:
  PYTHONPATH=src .venv/bin/python scripts/batch_process.py \\
    --src '/path/to/xer-files' \\
    --out out/active-projects/
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import traceback
from pathlib import Path

import duckdb

from p6flow.calendar import materialize_calendar_derived
from p6flow.loader import load_xer
from p6flow.output import write_parquet
from p6flow.tokenizer import parse_tables
from p6flow.udf import materialize_udf_wide
from p6flow.validate import validate_fks


def _independent_text_counts(src: Path) -> dict[str, int]:
    """Count rows per %T block by reading the XER as raw text.

    Deliberately NOT using p6flow.tokenizer so this functions as an
    independent oracle: a regression in the tokenizer would not silently
    propagate into the validator.
    """
    raw = src.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    text = None
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return {}
    counts: dict[str, int] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("%T\t"):
            parts = line.split("\t", 2)
            current = parts[1].strip() if len(parts) >= 2 else None
            if current:
                counts.setdefault(current, 0)
        elif line.startswith("%R\t") and current:
            counts[current] += 1
        elif line.startswith("%E"):
            current = None
    return counts


def _validate_against_parquet(
    out_dir: Path, source_counts: dict[str, int]
) -> tuple[bool, list[dict]]:
    """Independent cross-check: source-text counts vs parquet COUNT(*)
    for every source table. Returns (all_match, per_table_report)."""
    import duckdb as _duckdb
    con = _duckdb.connect(":memory:")
    report: list[dict] = []
    all_match = True
    for table, src_n in source_counts.items():
        pq_path = out_dir / f"{table}.parquet"
        pq_n = None
        if pq_path.exists():
            pq_n = con.execute(
                f"SELECT COUNT(*) FROM read_parquet('{pq_path}')"
            ).fetchone()[0]
        # Empty %T blocks (zero columns, zero rows) don't get a parquet
        # file because the loader skips them. Treat that as a match
        # when the source row count is zero — there's nothing to lose.
        if pq_n is None and src_n == 0:
            match = True
        else:
            match = pq_n == src_n
        if not match:
            all_match = False
        report.append({
            "table": table,
            "source_rows": src_n,
            "parquet_rows": pq_n,
            "match": match,
        })
    return all_match, report


def _process_one(src: Path, out_dir: Path, add_dw_columns: bool = False) -> dict:
    """Parse one XER and return a report row. Never raises; failures
    are captured into the row's `error` field so batch keeps moving."""
    row: dict = {
        "source": str(src),
        "out_dir": str(out_dir),
        "p6_version": None,
        "table_count": 0,
        "total_rows": 0,
        "fk_violations": 0,
        "unknown_udf_types": 0,
        "row_count_match": None,
        "error": None,
    }
    try:
        out_dir.mkdir(parents=True, exist_ok=True)

        tables = list(parse_tables(src))
        src_counts = {t.name: len(t.rows) for t in tables}

        con = duckdb.connect(":memory:")
        counts = load_xer(con, tables)
        counts.update(materialize_calendar_derived(con))
        udf_counts, udf_unknown = materialize_udf_wide(con)
        counts.update(udf_counts)
        violations = validate_fks(con)
        manifest = write_parquet(
            con,
            out_dir,
            src,
            counts,
            violations=violations,
            udf_unknown_types=udf_unknown,
            add_dw_columns=add_dw_columns,
        )

        # Row-count match: every source table count must equal the
        # manifest count for that table. An empty %T (zero rows) that
        # didn't make it into the manifest is fine; the loader skips
        # creating a column-less table.
        manifest_counts = {t["name"]: t["rows"] for t in manifest["tables"]}
        match = all(
            manifest_counts.get(name, 0) == n if n == 0 else manifest_counts.get(name) == n
            for name, n in src_counts.items()
        )

        row["p6_version"] = manifest["p6_version"]
        row["table_count"] = len(manifest["tables"])
        row["total_rows"] = sum(t["rows"] for t in manifest["tables"])
        row["fk_violations"] = len(violations)
        row["unknown_udf_types"] = len(udf_unknown)
        row["row_count_match"] = match

        # Independent text-parse validation: does NOT use p6flow.
        text_counts = _independent_text_counts(src)
        text_total = sum(text_counts.values())
        text_match, per_table = _validate_against_parquet(out_dir, text_counts)
        row["text_parse_total_rows"] = text_total
        row["text_parse_match"] = text_match
        if not text_match:
            row["text_parse_mismatches"] = [
                p for p in per_table if not p["match"]
            ]
    except Exception as e:
        row["error"] = f"{type(e).__name__}: {e}"
        row["traceback"] = traceback.format_exc()
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", type=Path, default=None,
                    help="Source root containing .xer files (recursive).")
    ap.add_argument("--input-list", type=Path, default=None,
                    help="File with one absolute .xer path per line. "
                         "Use instead of --src to process a precomputed shard.")
    ap.add_argument("--src-root", type=Path, default=None,
                    help="With --input-list: base path used to compute relative "
                         "out_dir paths. Defaults to common parent of inputs.")
    ap.add_argument("--out", required=True, type=Path,
                    help="Output root; relative path of each input is preserved.")
    ap.add_argument("--limit", type=int, default=None,
                    help="Process at most N files (useful for smoke testing).")
    ap.add_argument("--match", default="*.xer",
                    help="Glob pattern (default *.xer).")
    ap.add_argument("--skip-existing", action="store_true",
                    help="Skip files whose output manifest.json already exists.")
    ap.add_argument("--exclude", action="append", default=[],
                    help="Substring to exclude from source path (repeatable).")
    ap.add_argument("--report-path", type=Path, default=None,
                    help="Where to write the aggregate JSON report. "
                         "Defaults to <out>/_batch_report.json.")
    ap.add_argument("--add-dw-columns", action="store_true",
                    help="Append warehouse columns to every emitted parquet: "
                         "source_xer, source_sha256, flow_published_at, "
                         "created_at, _raw.")
    args = ap.parse_args()

    out_root: Path = args.out.resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    if args.input_list is not None:
        if not args.input_list.exists():
            print(f"input-list not found: {args.input_list}", file=sys.stderr)
            return 2
        inputs = [
            Path(line).resolve()
            for line in args.input_list.read_text().splitlines()
            if line.strip()
        ]
        if args.src_root is not None:
            src_root = args.src_root.resolve()
        else:
            # Common parent of all inputs (longest shared prefix path).
            common = Path(__import__("os").path.commonpath([str(p) for p in inputs]))
            src_root = common
    else:
        if args.src is None:
            print("must pass --src or --input-list", file=sys.stderr)
            return 2
        src_root = args.src.resolve()
        if not src_root.exists():
            print(f"src not found: {src_root}", file=sys.stderr)
            return 2
        inputs = sorted(src_root.rglob(args.match))

    if args.exclude:
        inputs = [p for p in inputs if not any(x in str(p) for x in args.exclude)]
    if args.limit:
        inputs = inputs[: args.limit]

    print(f"Found {len(inputs)} files matching {args.match!r} under {src_root}")
    rows: list[dict] = []
    skipped = 0
    for i, src in enumerate(inputs, 1):
        rel = src.relative_to(src_root).with_suffix("")
        out_dir = out_root / rel
        if args.skip_existing and (out_dir / "manifest.json").exists():
            skipped += 1
            continue
        print(f"[{i}/{len(inputs)}] {rel}", flush=True)
        row = _process_one(src, out_dir, add_dw_columns=args.add_dw_columns)
        rows.append(row)
        if row["error"]:
            print(f"    ERROR: {row['error']}", flush=True)
        else:
            print(
                f"    OK: P6 v{row['p6_version']}, {row['table_count']} tables, "
                f"{row['total_rows']} rows, {row['fk_violations']} fk-violations, "
                f"{row['unknown_udf_types']} unknown udf types, "
                f"row-count match={row['row_count_match']}, "
                f"text-parse match={row.get('text_parse_match')}",
                flush=True,
            )

    # Aggregate report.
    json_path = (args.report_path or (out_root / "_batch_report.json")).resolve()
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(rows, indent=2))

    csv_path = json_path.with_suffix(".csv")
    with csv_path.open("w", newline="") as f:
        fieldnames = [
            "source", "out_dir", "p6_version", "table_count", "total_rows",
            "fk_violations", "unknown_udf_types", "row_count_match",
            "text_parse_match", "text_parse_total_rows", "error",
        ]
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    # Summary line.
    n_ok = sum(1 for r in rows if r["error"] is None)
    n_fail = len(rows) - n_ok
    n_match = sum(1 for r in rows if r["row_count_match"] is True)
    print()
    print(f"Done. {n_ok}/{len(rows)} parsed cleanly, {n_fail} errored, "
          f"{n_match} with exact row-count match. {skipped} skipped (already done).")
    print(f"Report: {json_path}")
    print(f"Report: {csv_path}")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
