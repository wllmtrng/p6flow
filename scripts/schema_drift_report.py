"""Analyze schema drift across a batch of parsed XER manifests.

Walks all manifest.json files under --root, groups schema fingerprints
per table, and reports:

  - Per-table fingerprint distribution: how many distinct shapes does
    each table have across the corpus?
  - Per-fingerprint actual column list (read from one representative
    parquet) to show what physically differs between shapes.
  - P6 version → fingerprint correlation: do fingerprint clusters
    track P6 version, source database, both, or neither?
  - UDF type usage distribution: which logical_data_types appear and
    how often.
  - Outliers: tables with extreme row counts, files with extreme table
    counts.

Output: stdout markdown report, optionally a JSON dump for tooling.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import duckdb


def _load_manifests(root: Path) -> list[dict]:
    out: list[dict] = []
    for mp in root.rglob("manifest.json"):
        try:
            out.append({"_path": str(mp), **json.loads(mp.read_text())})
        except (OSError, json.JSONDecodeError) as e:
            print(f"skipping {mp}: {e}", file=sys.stderr)
    return out


def _fingerprint_to_columns(
    manifests: list[dict], table: str, fingerprint: str
) -> list[tuple[str, str]] | None:
    """Read one parquet matching the fingerprint to recover its actual
    column list. Returns [(col_name, duckdb_type), ...] or None."""
    con = duckdb.connect(":memory:")
    for m in manifests:
        for t in m["tables"]:
            if t["name"] == table and t["schema_fingerprint"] == fingerprint:
                pq = Path(m["_path"]).parent / t["parquet"]
                if not pq.exists():
                    continue
                try:
                    desc = con.execute(
                        f"DESCRIBE SELECT * FROM read_parquet('{pq}')"
                    ).fetchall()
                    return [(r[0], r[1]) for r in desc]
                except Exception:
                    continue
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=None,
                    help="Write JSON report to this path (default stdout only).")
    ap.add_argument("--show-cols", action="store_true",
                    help="For tables with >1 fingerprint, print the column "
                         "lists to compare physical drift.")
    args = ap.parse_args()

    manifests = _load_manifests(args.root)
    if not manifests:
        print(f"no manifests under {args.root}", file=sys.stderr)
        return 2

    print("# Schema drift report\n")
    print(f"Corpus: {len(manifests)} parsed XERs under `{args.root}`.\n")

    # P6 version distribution.
    versions = Counter(m["p6_version"] for m in manifests)
    print("## P6 version distribution\n")
    print("| version | files |")
    print("|---|---|")
    for v, c in versions.most_common(15):
        print(f"| {v} | {c} |")
    if len(versions) > 15:
        rest = sum(versions.values()) - sum(c for _, c in versions.most_common(15))
        more = len(versions) - 15
        print(f"| ... | {rest} more in {more} versions |")
    print()

    # Table presence + fingerprint count.
    tables_to_files: dict[str, list[dict]] = defaultdict(list)
    for m in manifests:
        for t in m["tables"]:
            tables_to_files[t["name"]].append({"manifest": m, "table": t})

    print("## Table coverage\n")
    print("| table | files | distinct fingerprints | min rows | max rows |")
    print("|---|---|---|---|---|")
    table_drift: dict[str, set[str]] = {}
    for name in sorted(tables_to_files, key=lambda n: -len(tables_to_files[n])):
        entries = tables_to_files[name]
        fps = {e["table"]["schema_fingerprint"] for e in entries}
        rows = [e["table"]["rows"] for e in entries]
        table_drift[name] = fps
        print(f"| {name} | {len(entries)} | {len(fps)} | {min(rows)} | {max(rows)} |")
    print()

    # Tables with the most schema drift.
    print("## Tables with the most schema drift (top 10)\n")
    drift_ranked = sorted(table_drift.items(), key=lambda x: -len(x[1]))[:10]
    print("| table | distinct shapes | files |")
    print("|---|---|---|")
    for name, fps in drift_ranked:
        print(f"| {name} | {len(fps)} | {len(tables_to_files[name])} |")
    print()

    # P6 version vs fingerprint for the most-drifted table.
    if drift_ranked:
        worst_table = drift_ranked[0][0]
        print(f"## Drift breakdown for `{worst_table}`\n")
        version_fp: dict[str, Counter] = defaultdict(Counter)
        for entry in tables_to_files[worst_table]:
            ver = entry["manifest"]["p6_version"]
            fp = entry["table"]["schema_fingerprint"]
            version_fp[fp][ver] += 1
        print("| fingerprint | files | top P6 version(s) |")
        print("|---|---|---|")
        for fp, vers in sorted(version_fp.items(), key=lambda x: -sum(x[1].values())):
            top = ", ".join(f"{v}({c})" for v, c in vers.most_common(3))
            print(f"| `{fp[:8]}…` | {sum(vers.values())} | {top} |")
        print()

        if args.show_cols:
            print(f"### Actual column lists for top 3 fingerprints of `{worst_table}`\n")
            top_fps = sorted(version_fp.keys(),
                             key=lambda fp: -sum(version_fp[fp].values()))[:3]
            cols_by_fp = {
                fp: _fingerprint_to_columns(manifests, worst_table, fp)
                for fp in top_fps
            }
            all_cols = set()
            for cols in cols_by_fp.values():
                if cols:
                    all_cols.update(c[0] for c in cols)
            print(f"`{worst_table}` has {len(all_cols)} distinct columns "
                  f"across these 3 fingerprints.")
            for fp, cols in cols_by_fp.items():
                if cols is None:
                    print(f"\n- `{fp[:8]}…`: (parquet not readable)")
                    continue
                col_names = {c[0] for c in cols}
                missing = sorted(all_cols - col_names)
                print(f"\n- `{fp[:8]}…`: {len(col_names)} cols, "
                      f"missing {len(missing)} relative to union: "
                      f"{missing[:8]}{'...' if len(missing) > 8 else ''}")
            print()

    # Unknown UDF types across the corpus.
    udf_types: Counter = Counter()
    udf_examples: dict[str, str] = {}
    for m in manifests:
        for u in m.get("udf_unknown_types", []):
            t = u["logical_data_type"]
            udf_types[t] += 1
            udf_examples.setdefault(t, u.get("inferred_source_column", "?"))
    print("## Unknown UDF types (inferred at parse time)\n")
    if not udf_types:
        print("None.\n")
    else:
        print("| logical_data_type | occurrences | inferred storage |")
        print("|---|---|---|")
        for t, c in udf_types.most_common():
            print(f"| `{t}` | {c} | `{udf_examples[t]}` |")
        print()

    # FK violation totals.
    total_violations = 0
    files_with_violations = 0
    for m in manifests:
        v = m.get("fk_violations") or []
        if v:
            files_with_violations += 1
            total_violations += sum(x["violating_rows"] for x in v)
    print("## FK violations\n")
    print(f"- Files with at least one violation: **{files_with_violations} of "
          f"{len(manifests)}**")
    print(f"- Sum of violating rows across the corpus: **{total_violations:,}**\n")

    # Top 10 files by total row count (largest XERs).
    print("## Largest XERs by row count\n")
    by_rows = sorted(
        manifests,
        key=lambda m: -sum(t["rows"] for t in m["tables"]),
    )[:10]
    print("| project_label | rows | tables |")
    print("|---|---|---|")
    for m in by_rows:
        rows = sum(t["rows"] for t in m["tables"])
        label = (m.get("project_label") or "")[:40]
        print(f"| {label} | {rows:,} | {len(m['tables'])} |")
    print()

    if args.out:
        report = {
            "corpus_size": len(manifests),
            "p6_versions": dict(versions),
            "tables": {
                name: {
                    "files": len(tables_to_files[name]),
                    "fingerprints": list(table_drift[name]),
                }
                for name in tables_to_files
            },
            "unknown_udf_types": dict(udf_types),
            "files_with_fk_violations": files_with_violations,
            "total_fk_violations": total_violations,
        }
        args.out.write_text(json.dumps(report, indent=2))
        print(f"JSON report written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
