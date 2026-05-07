# p6flow

Parse Primavera P6 `.xer` schedule exports into typed tables. DuckDB for staging
and validation; Parquet output ready for Snowflake `COPY INTO`.

## Architecture

`%F` is ground truth. The parser reads the actual field list from each XER
block, looks up types in `src/p6flow/schema.py` (codegen'd from Oracle's
EPPM v26.4 database schema), and falls back to a heuristic + value sniffing
combo for columns the schema doesn't know about (legacy P6 columns, removed
columns, etc.). Disagreements between heuristic and sniffing fail fast.

## Install

```sh
uv sync
# or: pip install -e .[dev]
```

## Use

```sh
p6flow fixtures/sample.xer --out out/
```

Each input gets its own `out/<basename>/` containing one Parquet per table
plus `manifest.json`.

## Regenerate schema

When Oracle ships a new EPPM version:

```sh
python scripts/codegen.py --version 26.5
```
