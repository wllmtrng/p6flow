# p6flow

Parse Primavera P6 `.xer` schedule exports into typed, validated tables.
DuckDB stages and checks the data in memory; the result is one Parquet file per table, ready for Snowflake `COPY INTO` or any other Parquet consumer.

```sh
pip install p6flow
p6flow MySchedule.xer --out out/
```

```
parsing MySchedule.xer
  -> out/MySchedule (12 tables, 33 rows, P6 v22.12)
  fk-validation: 1 violation(s)
    [orphan] ACTVTYPE.proj_id -> PROJECT.proj_id: 1 row(s); sample: (429)
```

## Who this is for

- **Data engineers** who receive XER files from schedulers and need them in a warehouse with real column types, not 300 tab-separated text blocks.
- **Analytics and BI teams** building dashboards over schedule progress, float, critical path, resource loading, or earned value across many projects.
- **Construction tech and owner's-rep teams** reconciling the P6 baseline against field data, cost systems, or BIM, where every activity needs a stable, typed key.
- **Schedule analysts and forensic delay consultants** who want to query a schedule with SQL instead of clicking through P6, or who need to diff two exports of the same project.
- **Anyone integrating with P6 without a database login.** The XER file is often the only thing a contractor will send you, and `p6flow` turns it into something you can load, join, and version.

If you already have direct access to the P6 EPPM database, you probably do not need this.
If what you have is a folder of `.xer` files, you do.

## What it is used for

- **Warehouse ingestion.** Convert each export into Parquet with the same column types Oracle declares for its own database, then `COPY INTO` Snowflake, BigQuery, Redshift, or DuckDB.
- **Cross-project portfolio reporting.** Batch-process hundreds of exports and union them on `(source_xer, task_id)` so internal P6 IDs that collide between exports never collide in your warehouse.
- **Schedule-over-time analysis.** Keep every weekly export, fingerprint its schema, and query how dates, float, and percent complete moved from update to update.
- **Calendar-aware duration math.** P6 stores working time in an opaque blob inside the `CALENDAR` table. `p6flow` decodes it into plain rows so you can compute working days between two dates in SQL.
- **User-defined field analysis.** P6 stores custom fields as a tall entity-attribute-value table. `p6flow` pivots them into wide, typed tables such as `TASK_UDF` with one column per field, named after the label the scheduler gave it.
- **Data quality checks on what you were sent.** Every run reports orphaned foreign keys and missing required keys, so you know whether the export is self-contained before anything downstream depends on it.

## Install

Requires Python 3.11 or newer.

```sh
pip install p6flow
```

For development:

```sh
git clone https://github.com/wllmtrng/p6flow
cd p6flow
uv sync --extra dev
# or: pip install -e .[dev]
```

## Use

```sh
p6flow INPUT [INPUT ...] --out DIR
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--out DIR` | required | Output root. Each input gets its own `DIR/<basename>/`. |
| `--validate / --no-validate` | on | Run foreign-key validation after load and persist the report into `manifest.json`. |
| `--add-dw-columns / --no-add-dw-columns` | off | Append warehouse provenance columns to every Parquet. Use this for cross-XER loads. |
| `--quiet / --no-quiet` | off | Suppress per-table progress output. |

Multiple inputs are processed independently and never merged.
Separate projects reuse the same internal numeric ID space, so merging would silently collide primary keys.
Pass `--add-dw-columns` and union on `(source_xer, <id>)` downstream instead.

Try it on the bundled fixture:

```sh
p6flow fixtures/synthetic_v2212.xer --out out/
```

## What you get

Each input produces a directory like this:

```
out/MySchedule/
├── PROJECT.parquet
├── PROJWBS.parquet
├── TASK.parquet
├── TASKPRED.parquet
├── CALENDAR.parquet
├── CALENDAR_WEEKLY.parquet      # derived
├── CALENDAR_EXCEPTION.parquet   # derived
├── UDFTYPE.parquet
├── UDFVALUE.parquet
├── TASK_UDF.parquet             # derived
├── UDF_COLUMN_MAP.parquet       # derived
└── manifest.json
```

### Source tables

One Parquet per `%T` block in the file, named exactly as P6 names it (`TASK`, `PROJWBS`, `RSRC`, `TASKRSRC`, and so on).
Columns are the ones that actually appear in the file's `%F` line, in that order, with these conversions applied:

- Empty cells become SQL `NULL`.
- The literal strings `null` and `NULL` in typed columns also become `NULL`, because some exporters emit them for missing numerics.
- Types come from Oracle's schema for the 5,140 columns it knows about, and from inference for the rest (see below).
- Parquet is written with ZSTD compression.

### Derived tables

| Table | Source | What it holds |
| --- | --- | --- |
| `CALENDAR_WEEKLY` | `CALENDAR.clndr_data` | One row per working segment per weekday: `clndr_id`, `day_of_week` (1 = Sunday), `segment_idx`, `start_time`, `end_time`, `hours`. |
| `CALENDAR_EXCEPTION` | `CALENDAR.clndr_data` | One row per exception-day segment, or a single row with `segment_idx = NULL` and `hours_worked = 0` for a full non-working day such as a holiday. |
| `<ENTITY>_UDF` | `UDFTYPE` + `UDFVALUE` | One wide table per entity that has user-defined fields (`TASK_UDF`, `PROJECT_UDF`, `RSRC_UDF`, ...). One row per entity row, one typed column per field. |
| `UDF_COLUMN_MAP` | `UDFTYPE` | Maps each pivoted column name back to its human label, logical type, and wide table. |

Derived tables are skipped quietly when their source tables are absent from the export.

### manifest.json

The manifest is the cheap diff target across runs.
It records provenance and a schema fingerprint per table so you can detect a changed export without opening any Parquet.

```json
{
  "source_xer": "/abs/path/MySchedule.xer",
  "source_sha256": "1d7bd29d...",
  "parsed_at": "2026-11-02T17:25:33.976849+00:00",
  "p6_version": "22.12",
  "export_date": "2025-07-15",
  "project_label": "Project",
  "tables": [
    {
      "name": "TASK",
      "rows": 3,
      "parquet": "TASK.parquet",
      "schema_fingerprint": "fd8dcff0aa6c72c6"
    }
  ],
  "fk_violations": [
    {
      "child_table": "ACTVTYPE",
      "fk_name": "fk_actvtype_project",
      "target_table": "PROJECT",
      "local_fields": ["proj_id"],
      "target_fields": ["proj_id"],
      "kind": "orphan",
      "violating_rows": 1,
      "sample_keys": [["429"]]
    }
  ]
}
```

`fk_violations` is omitted when you pass `--no-validate`.
`udf_unknown_types` appears only when a user-defined field used a logical type the pivot did not recognise and had to infer from the data.

### Warehouse columns

With `--add-dw-columns`, every Parquet gains five trailing columns:

| Column | Type | Value |
| --- | --- | --- |
| `source_xer` | VARCHAR | Input file basename without extension. |
| `source_sha256` | VARCHAR | SHA-256 of the input file, matching the manifest. |
| `flow_published_at` | TIMESTAMP | UTC time the Parquet was written. |
| `exported_at` | TIMESTAMP | Export date from the file's `ERMHDR` line. |
| `_raw` | VARCHAR | The exact `%R` line this row was parsed from. `NULL` on derived tables. |

The natural cross-export join key becomes `(source_xer, <p6_id>)`.
The `_raw` column lets you prove what the source said for any row without going back to the file.

## How it works

The pipeline runs entirely in memory, one file at a time, in six stages.

### 1. Tokenize

An XER file is a flat text format with a tiny grammar:

```
ERMHDR  22.12  2025-07-15  Project  ADMIN  Test User  dbxDatabaseNoName  Project Management
%T  PROJECT
%F  proj_id  proj_short_name  plan_start_date  fy_start_month_num
%R  100      SYNTH-DEMO       2025-01-01 08:00 1
%T  TASK
%F  task_id  proj_id  wbs_id  ...
%R  1001     100      2       ...
%E
```

`%T` opens a table, `%F` declares its columns, each `%R` is a row, and `%E` ends the file.
The tokenizer in `src/p6flow/tokenizer.py` is a single iterative loop over lines that yields one `Table` per block.
It captures the raw `%R` line alongside the split cells so provenance survives to the warehouse.

Real exports are messier than the grammar suggests, and the tokenizer handles each case deliberately:

- **Encoding.** Files are decoded by trying `utf-8-sig`, then `cp1252`, then `latin-1` on the whole file. The whole-file attempt matters because a prefix sniff picks UTF-8 on an ASCII header and then crashes on a curly quote in a description three megabytes in.
- **Tabs inside values.** P6 quotes a text cell that contains a tab and doubles any embedded quote. Quote-aware splitting is applied only to rows that overflow the column count, because a stray `"` in plain text such as `6" pipe` must not be treated as a quote.
- **Duplicate column names.** Some exports list the same column twice in one `%F` line. The second occurrence is renamed `<name>_2` so no data is dropped.
- **Empty tables.** A `%T` followed directly by another `%T` is treated as a table that is not present in this export.
- **Short and long rows.** Rows with trailing optional fields missing are padded with empties. Trailing empty cells beyond the declared width are dropped. A row with extra non-empty cells is a format violation and raises.
- **Table-name whitelist.** Table names flow into SQL identifiers and file paths, so anything other than an uppercase identifier is rejected before it can reach either.

### 2. Resolve types

`%F` is ground truth for which columns exist.
Oracle's EPPM database schema is ground truth for what type each one is.
`src/p6flow/schema.py` is generated from Oracle's published `pmSchema.xml` and carries:

| What | Count (EPPM 26.4) |
| --- | --- |
| Tables | 347 |
| Columns with DuckDB and Snowflake types, nullability, and description | 5,140 |
| Primary keys | 297 |
| Foreign keys, composite-aware | 363 |

Columns in the file that the schema does not know (legacy columns, columns Oracle has since removed, exporter quirks) go through a two-vote inference in `src/p6flow/inference.py`:

1. A **naming heuristic** buckets the column by suffix. `_id`, `_cnt`, `_num`, and `_seq` are integers. `_pct`, `_qty`, `_rate`, `_cost`, `_drtn`, and `_hr_cnt` are doubles. `_date` is a timestamp. `_flag` is a one-character flag. `_name`, `_type`, `_code`, and friends are strings.
2. A **value sniffer** samples up to 1,000 non-empty values and classifies them as date, flag, integer, double, or string.

If both agree, that type wins.
If naming says double and the data is all integers, double wins because the data fits without loss.
If they disagree in a way that would lose information, such as naming saying integer while the data has decimals, the run fails with a `TypeMismatch` naming the column.
That is deliberate: a confidently wrong type in a warehouse is worse than a loud failure at ingest.
If neither has an opinion, the column is `VARCHAR`.

### 3. Build DDL and load into DuckDB

`src/p6flow/ddl.py` emits a `CREATE TABLE` per block using only the columns that appeared in the file.
Primary keys are declared when every key column is present.
Foreign keys and `NOT NULL` are declared in the schema but **not enforced** at load time, because an XER is a partial export.
A task's calendar or a WBS node's parent project routinely lives in a different file, and a load that rejected those rows would reject most real exports.

Tables are topologically sorted so parents load before children, then each one is inserted through a single `INSERT ... SELECT` with a `CAST(NULLIF(col, '') AS <type>)` per column.
Rows go through Apache Arrow into DuckDB, so a large export loads in seconds.

### 4. Decode calendars

`CALENDAR.clndr_data` is a nested paren-tree blob that looks like this:

```
(0||CalendarData()((0||DaysOfWeek()((0||2()((0||0(s|08:00|f|16:00)())))...))(0||Exceptions()((0||0(d|45803)())...))))
```

`src/p6flow/calendar.py` parses it with a small recursive-descent parser, with a depth cap so a malformed blob cannot exhaust the stack.
Weekday segments become `CALENDAR_WEEKLY` rows.
Exception days are stored as a day count from P6's epoch of 1899-12-30, which was confirmed empirically against calendars whose exceptions were recognisable US holidays.
Both `08:00` and `8:00 AM` time formats are accepted, and `24:00` is normalised to end of day.

### 5. Pivot user-defined fields

P6 stores custom fields as metadata in `UDFTYPE` and values in `UDFVALUE`, one row per `(entity_row, field)`.
`src/p6flow/udf.py` groups fields by the entity they belong to and builds one `MAX(CASE WHEN ...)` pivot per entity.
Each field's `logical_data_type` selects which `UDFVALUE` column carries the value and what to cast it to:

| Logical type | Source column | Output type |
| --- | --- | --- |
| `FT_TEXT`, `FT_STATICTYPE` | `udf_text` | VARCHAR |
| `FT_NUMBER`, `FT_MONEY`, `FT_TYPE_COST`, `FT_FLOAT_2_DECIMALS` | `udf_number` | DOUBLE |
| `FT_INT`, `FT_TYPE_INTEGER` | `udf_number` | BIGINT |
| `FT_DATE`, `FT_START_DATE`, `FT_END_DATE` | `udf_date` | TIMESTAMP |
| `FT_TYPE_CODE` | `UDFCODE.udf_code_name` via join | VARCHAR |

A type not in that table is resolved by inspecting which `UDFVALUE` column actually holds data, and the decision is recorded in `manifest.json` under `udf_unknown_types`.
Column names are the field's label, lowercased and slugified with a `udf_` prefix, so `MSP Activity ID` becomes `udf_msp_activity_id`.
Fields with no values in this export are skipped rather than emitted as all-null columns.

### 6. Validate and write

`src/p6flow/validate.py` walks every declared foreign key whose child and parent tables and columns are all present, and reports two kinds of violation:

- **orphan**: the child has a non-null key with no matching parent row.
- **missing_required_fk**: the child has `NULL` in a key column that Oracle's schema declares `NOT NULL`.

`NULL` in a nullable key is not a violation.
A root WBS node legitimately has no parent.
Each violation carries a row count and up to five sample keys, prints to the console, and persists into the manifest.

Finally `src/p6flow/output.py` writes each table with DuckDB's `COPY ... TO ... (FORMAT PARQUET, COMPRESSION ZSTD)` and builds the manifest.

## Failure modes

`p6flow` sorts every irregularity it meets into one of three tiers.
The rule behind the sorting: anything that could put a wrong value in the warehouse stops the run, anything that is a property of the export rather than a parsing error is reported and kept, and anything that is a known exporter quirk with one unambiguous reading is absorbed.

### Stops the run

These raise an exception, the CLI exits non-zero, and nothing is written for that file.
Any inputs listed after it on the command line are not processed.
The batch script is the exception: it catches the error, records it in the per-file row of `_batch_report.json`, and moves on.

| Condition | Error | Why it is fatal |
| --- | --- | --- |
| First line is not `ERMHDR` | `ValueError: missing ERMHDR` | The header carries the P6 version and export date the manifest depends on. Detected when the manifest is written, after the load. |
| File does not end with `%E` | `ValueError: file ended without %E marker` | A truncated download or a partial copy looks exactly like this. Loading a truncated table as if it were complete is the worst silent outcome. |
| `%T` name is not an uppercase identifier | `ValueError: invalid %T table name` | Table names become SQL identifiers and output paths. |
| `%T` not followed by `%F` | `ValueError: expected %F after %T` | The grammar has no reading for it. |
| A line inside a table that is not `%R`, `%T`, or `%E` | `ValueError: unexpected line in ... rows` | Same. |
| A row has more non-empty cells than `%F` declared, even after quote-aware re-splitting | `RowArityMismatch` | The extra cells carry data with no column to go in. |
| An unknown column's name suffix and its values disagree on type | `TypeMismatch: PROJECT.foo_cnt: naming says 'integer', sniffing says 'double'` | Picking either side could truncate or mis-type real values. |
| A schema-typed column holds an uncastable value, such as `abc` in `proj_id` or `not-a-date` in `plan_start_date` | DuckDB `ConversionException` | The schema says what the type is. A value that does not fit means the export is not what it claims to be. |
| Duplicate primary key within one table | DuckDB `ConstraintException` | Primary keys are the one constraint enforced at load. A duplicate here breaks every downstream join. |
| A malformed `clndr_data` blob: unbalanced parentheses, an odd number of attribute fields, trailing data, or nesting deeper than 64 levels | `ValueError` from the calendar parser | A half-decoded calendar would produce wrong working-day arithmetic for every activity on it. |

Encoding is never fatal.
`latin-1` maps every byte, so the decode chain always succeeds.

### Reported, then kept

These are printed to the console, persisted in `manifest.json`, and the Parquet is still written.
They describe the export, not a parsing problem, and the right response depends on what you are loading it for.

| Condition | Where it shows up | What it means |
| --- | --- | --- |
| Orphan foreign key: a child row points at a parent that is not in this file | `fk-validation` console block and `manifest.fk_violations` with `kind = "orphan"` | Usually a partial export. The referenced calendar, resource, or project was not included. Harmless for single-project analytics, a problem if you expected a self-contained snapshot. |
| Missing required foreign key: `NULL` in a key column Oracle declares `NOT NULL` | Same, with `kind = "missing_required_fk"` | Either an older P6 version where the column was optional, or a row that was mid-edit at export time. |
| A user-defined field with a `logical_data_type` the pivot does not recognise | `udf-unknown-types` console line and `manifest.udf_unknown_types` | The column was still produced by inspecting which value column held data. The record tells you which field and what was inferred, so you can confirm the type. |

Each foreign-key violation carries the row count and up to five sample keys.
Check `fk_violations` is empty before loading if your pipeline assumes referential integrity.

### Absorbed silently

These are handled with no message because each one has exactly one sensible interpretation and real exports produce them constantly.
If a run prints nothing but the summary line, some of these almost certainly happened.

| Condition | What happens |
| --- | --- |
| File is `cp1252` or has a UTF-8 byte-order mark | Decoded correctly. The encoding chosen is not recorded. |
| Lines before the first `%T` that are not the header | Skipped. |
| A row has fewer cells than `%F` declared | Padded with `NULL`. Trailing optional columns are routinely omitted. |
| A row has trailing empty cells beyond the declared width | Dropped. Some exporters end every line with a tab. |
| A text value contains a tab, quoted and escaped P6-style | Re-split quote-aware so it stays in one cell. |
| The same column name appears twice in a `%F` line | Second occurrence renamed `<name>_2` so both are kept. |
| A `%T` with no `%F` and no rows | Treated as not present in this export. It gets no Parquet and no manifest entry. |
| An empty cell, or the literal string `null` or `NULL`, in a typed column | Becomes SQL `NULL`. In `VARCHAR` columns only the empty cell converts, because `null` can be legitimate text. |
| A column the schema does not know, where naming and values agree or only one has an opinion | Typed by inference. Which columns were inferred is not recorded, though the schema fingerprint in the manifest will differ from a file where the column is absent. |
| A column Oracle declares `NOT NULL` arrives empty | Loaded as `NULL`. `NOT NULL` is not enforced because older P6 versions treat several of these as optional. |
| A foreign key whose parent table or key columns are not in this file | Not validated. There is nothing to check against. |
| `CALENDAR`, or both `UDFTYPE` and `UDFVALUE`, are absent | The corresponding derived tables are not produced. |
| A `CALENDAR` row with empty `clndr_data` | Skipped. Derived tables are still produced for the other calendars. |
| A calendar segment missing its start or finish, or an exception missing its date | That segment or exception is skipped. |
| A calendar time written as `8:00 AM` or ending at `24:00` | Converted to 24-hour time, with `24:00` normalised to end of day. |
| A user-defined field declared on an entity table not in this export, or with no values in this export | No column is produced for it. |
| Two user-defined fields whose labels slugify to the same column name | The second gets the field's numeric ID appended. `UDF_COLUMN_MAP` records both. |

If you need to know whether any of the silent cases fired for a given file, the `_raw` column written with `--add-dw-columns` holds the original line for every row and is the fastest way to find out.

## Loading into Snowflake

The Parquet column types are chosen to map cleanly onto Snowflake's: `BIGINT` to `NUMBER(19)`, `DOUBLE` to `FLOAT`, `TIMESTAMP` to `TIMESTAMP_NTZ`, `BLOB` to `BINARY`.

Generate a matching `CREATE TABLE` from the columns that actually appeared in a given file:

```python
from pathlib import Path
from p6flow.tokenizer import parse_tables
from p6flow.ddl import snowflake_ddl

for table in parse_tables(Path("MySchedule.xer")):
    if table.name == "TASK":
        print(snowflake_ddl(table.name, table.fields))
```

Then stage and copy:

```sql
PUT file://out/MySchedule/TASK.parquet @p6_stage/MySchedule/;

COPY INTO TASK
FROM @p6_stage/MySchedule/TASK.parquet
FILE_FORMAT = (TYPE = PARQUET)
MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE;
```

For a multi-project warehouse, run with `--add-dw-columns`, include `source_xer` in every target table, and treat `(source_xer, <p6_id>)` as the primary key.

## Using it as a library

The CLI is a thin wrapper over six functions that you can call directly, for example to keep the DuckDB connection open and query it in place.

```python
from pathlib import Path
import duckdb

from p6flow.tokenizer import parse_tables
from p6flow.loader import load_xer
from p6flow.calendar import materialize_calendar_derived
from p6flow.udf import materialize_udf_wide
from p6flow.validate import validate_fks
from p6flow.output import write_parquet

src = Path("MySchedule.xer")
con = duckdb.connect(":memory:")

counts = load_xer(con, list(parse_tables(src)))
counts.update(materialize_calendar_derived(con))
udf_counts, udf_unknown = materialize_udf_wide(con)
counts.update(udf_counts)
violations = validate_fks(con)

# Query in place before writing anything.
print(con.execute("""
    SELECT t.task_code, t.task_name, u.udf_msp_activity_id
    FROM TASK t LEFT JOIN TASK_UDF u USING (task_id)
    WHERE t.status_code = 'TK_Active'
""").fetchall())

manifest = write_parquet(
    con, Path("out") / src.stem, src, counts,
    violations=violations, udf_unknown_types=udf_unknown,
)
```

`read_header(path)` returns the `ERMHDR` fields (P6 version, export date, user, database, currency) without parsing the rest of the file.
`parse_clndr_data(blob)` decodes a single calendar blob into typed dataclasses if you only need that.

## Scripts

These are not installed as console commands.
Run them from a checkout with `PYTHONPATH=src`.

| Script | Purpose |
| --- | --- |
| `scripts/batch_process.py --src DIR --out DIR` | Process every `.xer` under a directory tree, preserving the hierarchy under `--out`, and write `_batch_report.json` and `_batch_report.csv` with one row per file: counts, violations, unknown UDF types, and any exception. Row counts are cross-checked by an independent text scan that does not share code with the tokenizer. Supports `--input-list`, `--match`, `--exclude`, `--limit`, `--skip-existing`, and `--add-dw-columns`. |
| `scripts/schema_drift_report.py --root DIR` | Read every `manifest.json` under a batch output and report how many distinct schema shapes each table has, which columns differ between shapes, whether shapes track P6 version, and which UDF types appear. Useful before designing warehouse DDL for a corpus spanning years of P6 releases. |
| `scripts/codegen.py --version 26.4` | Download Oracle's `eppm_schema.zip` for that EPPM version, parse `pmSchema.xml`, and regenerate `src/p6flow/schema.py`. Stdlib only. |
| `scripts/make_synthetic_fixture.py` | Regenerate `fixtures/synthetic_v2212.xer`, a byte-deterministic file that exercises calendars, legacy columns, user-defined fields, and one deliberate FK orphan. |

### Regenerate the schema

When Oracle ships a new EPPM version:

```sh
python scripts/codegen.py --version 26.5
```

The XER mapping document Oracle also publishes is intentionally not consulted.
It omits columns that real exports contain and lists columns that are never emitted, and it lags real P6 versions by years.
The database schema plus the file's own `%F` line is the only combination that has held up.

## Limitations

- **No merging across files.** Each export is an island by design. Union downstream on `(source_xer, <id>)`.
- **Partial exports pass.** Foreign keys are reported, not enforced. If you need a self-contained export, check `fk_violations` is empty before loading.
- **Type inference fails loudly.** A column the schema does not know whose name and values disagree stops the run. That is the intended behaviour, and the error names the column so you can decide. See Failure modes for the full list of what stops, what warns, and what is absorbed.
- **UDF type coverage is partly unverified.** Text, number, and money fields are exercised by real exports. Date, integer, and code fields are implemented from Oracle's documentation and are the ones most worth eyeballing on first use.
- **Everything is in memory.** A single export is held fully in RAM in DuckDB. Multi-gigabyte XER files are rare, but if you have one, process it on a machine with headroom.
- **Only `.xer`.** The XML export format (`.xml` from P6 Professional or the EPPM web client) is a different format and is not supported.

## Development

```sh
make install     # uv sync --extra dev
make test        # pytest
make lint        # ruff
make typecheck   # mypy, strict on src/
make codegen     # regenerate schema.py from EPPM 26.4
```

CI runs lint, type-check, and the test suite on Python 3.11 through 3.14 for every pull request.
Tagging `vX.Y.Z` on `main` runs the same matrix, verifies the tag matches `pyproject.toml`, builds, and publishes to PyPI through trusted publishing.

The test suite covers the tokenizer's edge cases, type inference agreement and disagreement, the calendar blob parser across time formats and epochs, UDF pivoting including label collisions, FK validation, and a set of robustness tests that feed hostile input to every stage.

## License

LGPL-3.0-or-later.
See `LICENSE` and `LICENSE-GPL`.
