#!/usr/bin/env python3
"""Codegen: build src/p6flow/schema.py from Oracle's EPPM schema XML.

The XER mapping doc is intentionally NOT consulted; it disagrees with reality
both ways (omits columns real XERs emit, lists columns that aren't emitted)
and lags real-world P6 versions by years. Real %F lines are the only reliable
source for what's in any given XER, and the parser reads them at runtime.

What this script does at codegen time:

    1. Download eppm_schema.zip for the requested EPPM version.
    2. Parse pmSchema.xml.
    3. Emit a single Python module exposing:
        COLUMN_TYPES: dict[str, dict[str, ColumnSpec]]
            (table, column) → DuckDB type, Snowflake type, nullable, description.
            Carries every column known to EPPM at this version.
        PRIMARY_KEYS: dict[str, tuple[str, ...]]
        FOREIGN_KEYS: dict[str, list[ForeignKey]]
            Composite-aware. Self-FKs preserved; consumers can filter.

What runtime code does (in src/p6flow/ddl.py):

    Reads the actual %F field list from the XER, looks up each (table, col)
    in COLUMN_TYPES, falls back to VARCHAR with no comment if absent. Emits
    DDL per XER on demand. No pre-baked SQL files.

Re-run when Primavera ships a new EPPM version:

    python scripts/codegen.py --version 26.5

Self-contained; uses only the Python stdlib.
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path

DOCS_BASE = "https://docs.oracle.com/cd/G48897_01/English/Mapping_and_Schema"
SCHEMA_ZIP_URL_FMT = (
    f"{DOCS_BASE}/eppm_schema_documentation_{{compact}}/eppm_schema.zip"
)

SNOWFLAKE_TYPE_MAP = {
    "integer": "NUMBER(19)",
    "string": "VARCHAR",
    "date": "TIMESTAMP_NTZ",
    "double": "FLOAT",
    "blob": "BINARY",
    "number": "NUMBER",
}
DUCKDB_TYPE_MAP = {
    "integer": "BIGINT",
    "string": "VARCHAR",
    "date": "TIMESTAMP",
    "double": "DOUBLE",
    "blob": "BLOB",
    "number": "DECIMAL",
}


@dataclass(frozen=True)
class ColumnSpec:
    duckdb_type: str
    snowflake_type: str
    nullable: bool
    description: str


@dataclass(frozen=True)
class ForeignKey:
    name: str
    local_fields: tuple[str, ...]
    target_table: str
    target_fields: tuple[str, ...]


def fetch(url: str, cache_path: Path, refresh: bool = False) -> bytes:
    if cache_path.exists() and not refresh:
        return cache_path.read_bytes()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"  fetching {url}")
    with urllib.request.urlopen(url) as resp:
        data = resp.read()
    cache_path.write_bytes(data)
    return data


def column_spec(field: ET.Element) -> ColumnSpec:
    datatype = field.get("DATATYPE", "string")
    cl_raw = field.get("CHARLENGTH")
    cl = int(cl_raw) if cl_raw and cl_raw.isdigit() and int(cl_raw) > 0 else None
    snowflake = SNOWFLAKE_TYPE_MAP.get(datatype, "VARCHAR")
    if datatype == "string" and cl:
        snowflake = f"VARCHAR({cl})"
    duckdb = DUCKDB_TYPE_MAP.get(datatype, "VARCHAR")
    return ColumnSpec(
        duckdb_type=duckdb,
        snowflake_type=snowflake,
        nullable=field.get("NOTNULL") != "Y",
        description=re.sub(r"\s+", " ", (field.get("DESC") or "")).strip(),
    )


def collect(
    root: ET.Element,
) -> tuple[
    dict[str, dict[str, ColumnSpec]],
    dict[str, tuple[str, ...]],
    dict[str, list[ForeignKey]],
]:
    columns: dict[str, dict[str, ColumnSpec]] = {}
    pks: dict[str, tuple[str, ...]] = {}
    fks: dict[str, list[ForeignKey]] = {}

    for table in root.findall("TABLE"):
        tname = table.get("NAME") or ""
        if not tname:
            continue
        cols: dict[str, ColumnSpec] = {}
        for f in table.findall("FIELD"):
            n = f.get("NAME")
            if n:
                cols[n] = column_spec(f)
        if not cols:
            continue
        columns[tname] = cols

        for c in table.findall("CONSTRAINT"):
            ctype = c.get("TYPE")
            cfields = tuple(f for f in (c.get("FIELDS") or "").split(",") if f)
            if ctype == "PRIMARY" and cfields:
                pks[tname] = cfields
            elif ctype == "FOREIGN":
                tgt = c.get("TARGETTABLE") or ""
                tgt_fields = tuple(
                    f for f in (c.get("TARGETFIELDS") or "").split(",") if f
                )
                if cfields and tgt and len(cfields) == len(tgt_fields):
                    fks.setdefault(tname, []).append(
                        ForeignKey(
                            name=c.get("NAME") or f"fk_{tname.lower()}_{tgt.lower()}",
                            local_fields=cfields,
                            target_table=tgt,
                            target_fields=tgt_fields,
                        )
                    )
    return columns, pks, fks


def emit_module(
    columns: dict[str, dict[str, ColumnSpec]],
    pks: dict[str, tuple[str, ...]],
    fks: dict[str, list[ForeignKey]],
    out: Path,
    version: str,
) -> None:
    lines: list[str] = [
        f'"""Generated from EPPM v{version}. Do not edit; re-run scripts/codegen.py.',
        "",
        "Source: " + DOCS_BASE,
        "",
        "Carries every column from pmSchema.xml. Real-world XERs from older P6",
        "versions may emit columns absent here; runtime DDL falls back to VARCHAR.",
        '"""',
        "from __future__ import annotations",
        "",
        "from dataclasses import dataclass",
        "",
        "",
        "@dataclass(frozen=True)",
        "class ColumnSpec:",
        "    duckdb_type: str",
        "    snowflake_type: str",
        "    nullable: bool",
        "    description: str",
        "",
        "",
        "@dataclass(frozen=True)",
        "class ForeignKey:",
        "    name: str",
        "    local_fields: tuple[str, ...]",
        "    target_table: str",
        "    target_fields: tuple[str, ...]",
        "",
        "",
        f'EPPM_VERSION = "{version}"',
        "",
        "COLUMN_TYPES: dict[str, dict[str, ColumnSpec]] = {",
    ]
    for tname in sorted(columns):
        lines.append(f"    {tname!r}: {{")
        for cname, spec in columns[tname].items():
            lines.append(
                f"        {cname!r}: ColumnSpec("
                f"{spec.duckdb_type!r}, "
                f"{spec.snowflake_type!r}, "
                f"{spec.nullable!r}, "
                f"{spec.description!r}),"
            )
        lines.append("    },")
    lines.append("}")
    lines.append("")
    lines.append("PRIMARY_KEYS: dict[str, tuple[str, ...]] = {")
    for tname in sorted(pks):
        lines.append(f"    {tname!r}: {pks[tname]!r},")
    lines.append("}")
    lines.append("")
    lines.append("FOREIGN_KEYS: dict[str, list[ForeignKey]] = {")
    for tname in sorted(fks):
        lines.append(f"    {tname!r}: [")
        for fk in fks[tname]:
            lines.append(
                f"        ForeignKey({fk.name!r}, "
                f"{fk.local_fields!r}, "
                f"{fk.target_table!r}, "
                f"{fk.target_fields!r}),"
            )
        lines.append("    ],")
    lines.append("}")
    lines.append("")
    out.write_text("\n".join(lines))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--version", default="26.4", help="EPPM version, e.g. 26.4 or 26.5")
    p.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "cache",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "src" / "p6flow" / "schema.py",
    )
    p.add_argument("--refresh", action="store_true")
    args = p.parse_args()

    compact = args.version.replace(".", "")
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)

    print(f"codegen for EPPM v{args.version}")
    zip_url = SCHEMA_ZIP_URL_FMT.format(compact=compact)
    zip_path = args.cache_dir / f"eppm_schema_{compact}.zip"
    fetch(zip_url, zip_path, refresh=args.refresh)
    extract_dir = args.cache_dir / f"eppm_schema_{compact}"
    if args.refresh or not extract_dir.exists():
        extract_dir.mkdir(exist_ok=True)
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(extract_dir)
    xml_files = list(extract_dir.rglob("pmSchema.xml"))
    if not xml_files:
        print(f"error: pmSchema.xml not found in {extract_dir}", file=sys.stderr)
        return 1

    root = ET.parse(xml_files[0]).getroot()
    columns, pks, fks = collect(root)
    total_cols = sum(len(c) for c in columns.values())
    print(f"  {len(columns)} tables, {total_cols} columns")
    print(f"  {len(pks)} primary keys, {sum(len(v) for v in fks.values())} foreign keys")

    emit_module(columns, pks, fks, args.out, args.version)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
