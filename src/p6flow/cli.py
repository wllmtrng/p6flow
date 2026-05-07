"""p6flow: command-line entry point.

    p6flow INPUT [INPUT ...] --out DIR

Each input XER becomes a subdirectory under --out containing one Parquet
per table plus manifest.json. Multi-input runs do not merge: PKs collide
across XERs since separate projects can reuse internal numeric ID spaces.
"""

from __future__ import annotations

from pathlib import Path

import click
import duckdb

from .calendar import materialize_calendar_derived
from .loader import load_xer
from .output import write_parquet
from .tokenizer import parse_tables
from .udf import materialize_udf_wide
from .validate import FkViolation, validate_fks


def _print_violations(violations: list[FkViolation]) -> None:
    if not violations:
        click.echo("  fk-validation: clean")
        return
    click.echo(f"  fk-validation: {len(violations)} violation(s)")
    for v in violations:
        local = ".".join(v.local_fields)
        target = ".".join(v.target_fields)
        sample = ", ".join("(" + ",".join(k) + ")" for k in v.sample_keys[:3])
        click.echo(
            f"    [{v.kind}] {v.child_table}.{local} -> "
            f"{v.target_table}.{target}: {v.violating_rows} row(s); "
            f"sample: {sample}"
        )


@click.command(name="p6flow")
@click.argument(
    "inputs",
    nargs=-1,
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--out",
    "out_dir",
    required=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Output directory; one subdirectory per input XER.",
)
@click.option(
    "--validate/--no-validate",
    default=True,
    help="Run FK validation after load and persist into manifest.json.",
)
@click.option(
    "--add-dw-columns/--no-add-dw-columns",
    default=False,
    help="Append warehouse columns to every emitted parquet: "
         "source_xer, source_sha256, flow_published_at, created_at, _raw. "
         "Use for cross-XER loads where P6 IDs collide between exports.",
)
@click.option(
    "--quiet/--no-quiet",
    default=False,
    help="Suppress per-table progress output.",
)
def parse(
    inputs: tuple[Path, ...],
    out_dir: Path,
    validate: bool,
    add_dw_columns: bool,
    quiet: bool,
) -> None:
    """Parse XER files into typed Parquet."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for src in inputs:
        sub = out_dir / src.stem
        if not quiet:
            click.echo(f"parsing {src.name}")
        tables = list(parse_tables(src))
        con = duckdb.connect(":memory:")
        counts = load_xer(con, tables, add_dw_columns=add_dw_columns)
        counts.update(materialize_calendar_derived(con))
        udf_counts, udf_unknown = materialize_udf_wide(con)
        counts.update(udf_counts)
        violations = validate_fks(con) if validate else None
        manifest = write_parquet(
            con,
            sub,
            src,
            counts,
            violations=violations,
            udf_unknown_types=udf_unknown,
            add_dw_columns=add_dw_columns,
        )
        if not quiet:
            total = sum(counts.values())
            click.echo(
                f"  -> {sub} ({len(counts)} tables, {total} rows, "
                f"P6 v{manifest['p6_version']})"
            )
            if udf_unknown:
                click.echo(
                    f"  udf-unknown-types: {len(udf_unknown)} "
                    f"(see manifest.json#udf_unknown_types)"
                )
            if violations is not None:
                _print_violations(violations)


if __name__ == "__main__":
    parse()
