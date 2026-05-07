"""XER file tokenizer.

The grammar:

    file  = ERMHDR :: table* :: '%E'?
    table = '%T'<name> :: '%F'<col>+ :: ('%R'<val>+)*

Parsing is a single iterative loop. For each table:
    1. Grab the name from '%T' and the columns from the '%F' line that follows.
    2. While the next symbol is '%R', append the row.
    3. Return when the terminator is '%E'; otherwise it's the next '%T' and
       the loop continues with that as its opener.

Encoding fallback chain: utf-8-sig -> cp1252 -> latin-1.

utf-8-sig comes first because it transparently strips a UTF-8 BOM
when present. cp1252 is the dominant in-the-wild encoding for older
Primavera exports. latin-1 is the always-succeeds fallback (every
byte 0x00-0xFF maps to a code point) and exists so we never raise
on a file we can't classify.
"""

from __future__ import annotations

import io
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

ENCODINGS = ("utf-8-sig", "cp1252", "latin-1")

# %T table names flow unsanitized into SQL identifiers and Parquet output
# paths; a strict whitelist is the simplest defense against injection and
# path traversal. Real P6 tables (per Oracle's pmSchema.xml, 347 of them)
# are all uppercase identifiers, so this admits every legitimate name and
# rejects anything weaponizable.
_TABLE_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,62}$")


@dataclass
class Table:
    name: str
    fields: list[str]
    rows: list[list[str]]
    # Byte-faithful raw `%R\t...` line per row, captured before
    # _split_tab. Parallel to `rows` (raws[i] produced rows[i]).
    # Newline-stripped. Empty list when the table has no rows.
    raws: list[str] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.raws is None:
            self.raws = []


@dataclass(frozen=True)
class Header:
    version: str
    export_date: str
    project_label: str
    user_login: str | None
    user_full_name: str | None
    db_name: str | None
    module: str | None
    currency: str | None
    raw: tuple[str, ...]


# ---------- I/O ---------------------------------------------------------------


def _open_text(path: Path) -> io.StringIO:
    """Decode path's full contents and return a seekable StringIO.

    The encoding is selected by attempting full-file decode in order;
    we don't probe a prefix because real XERs frequently have ASCII
    headers and non-ASCII data deeper in the file (cp1252-encoded
    descriptions, scattered curly quotes). A prefix sniff picks
    utf-8-sig on those, then crashes mid-stream when an invalid UTF-8
    byte appears — exactly the failure mode this exists to prevent.

    latin-1 is the always-succeeds fallback since every byte 0x00-0xFF
    is mappable, so this never raises on a real file.
    """
    raw = path.read_bytes()
    for enc in ENCODINGS:
        try:
            return io.StringIO(raw.decode(enc), newline="")
        except UnicodeDecodeError:
            continue
    # Should be unreachable since latin-1 accepts every byte.
    raise UnicodeDecodeError(  # type: ignore[call-arg]
        "xer", raw, 0, 1, f"could not decode {path} with any of {ENCODINGS}"
    )


def _split_tab(line: str) -> list[str]:
    return line.rstrip("\r\n").split("\t")


def _dedup_fields(fields: list[str]) -> list[str]:
    """Suffix duplicate column names with `_2`, `_3`, etc.

    Some real XERs emit the same column name twice in one `%F` line.
    The cells in `%R` rows are positional, so keeping both columns
    means we can preserve the data; renaming the second occurrence is
    the cheapest way to satisfy DuckDB's "column must be unique"
    constraint without losing values.
    """
    seen: dict[str, int] = {}
    out: list[str] = []
    for f in fields:
        if f in seen:
            seen[f] += 1
            out.append(f"{f}_{seen[f]}")
        else:
            seen[f] = 1
            out.append(f)
    return out


# ---------- Header ------------------------------------------------------------


def read_header(path: Path) -> Header:
    with _open_text(path) as f:
        first = f.readline()
    parts = _split_tab(first)
    if not parts or parts[0] != "ERMHDR":
        raise ValueError(f"{path}: missing ERMHDR (got {parts[0]!r})")
    fields = parts[1:]
    pad = fields + [""] * (8 - len(fields))
    return Header(
        version=pad[0],
        export_date=pad[1],
        project_label=pad[2],
        user_login=pad[3] or None,
        user_full_name=pad[4] or None,
        db_name=pad[5] or None,
        module=pad[6] or None,
        currency=pad[7] or None,
        raw=tuple(parts),
    )


# ---------- Block parser ------------------------------------------------------


def parse_tables(path: Path) -> Iterator[Table]:
    """Yield every Table in the file.

    Skips ERMHDR and any pre-table noise inline. Delegates table parsing
    to _parse_tables once a '%T' is seen. Raises ValueError if the file
    does not end with a '%E' marker (the format's mandatory EOF symbol).
    """
    with _open_text(path) as stream:
        for line in stream:
            if line.startswith("%T\t"):
                terminator = yield from _parse_tables(line, stream)
                if not terminator.startswith("%E"):
                    raise ValueError(f"{path}: file ended without %E marker")
                return
            if line.startswith("%E"):
                return  # empty XER (header only) is valid
        raise ValueError(f"{path}: file ended without %E marker")


def _parse_tables(opener: str, stream: Iterator[str]) -> Iterator[Table]:
    """Yield tables, returning the line that terminated the sequence.

    The return value (visible to callers via `yield from`) is the final
    line read from the stream: '%E' on a well-formed file, or '' on EOF
    without a marker. parse_tables uses it to validate the file contract.

    Each iteration is three steps:
        1. Grab the table name from the '%T' line.
        2. Read the next line and parse the '%F' column list.
        3. Pull rows until the next non-'%R' line; that line becomes the
           opener for the next iteration. Loop exits when the line is '%E'.
    """
    line = opener
    while line and not line.startswith("%E"):
        # 1. Grab table name from %T
        name = _split_tab(line)[1].strip()
        if not _TABLE_NAME_RE.match(name):
            raise ValueError(
                f"invalid %T table name {name!r}: must match "
                f"{_TABLE_NAME_RE.pattern}"
            )

        # 2. Read %F line and parse column list. Real XERs sometimes
        #    emit %T immediately followed by another %T (or %E) when
        #    the table is empty in this export; treat that as a zero-
        #    column, zero-row table and continue.
        f_line = next(stream, "")
        if f_line.startswith("%T\t") or f_line.startswith("%E"):
            yield Table(name=name, fields=[], rows=[])
            line = f_line
            continue
        if not f_line.startswith("%F\t"):
            raise ValueError(f"expected %F after %T\\t{name}, got {f_line!r}")
        fields = _dedup_fields(
            [c.strip() for c in _split_tab(f_line)[1:] if c.strip()]
        )

        # 3. While the next line doesn't start with %E, parse one row line.
        #    A '%T' line means the current table is done and the next one
        #    starts; we stop the row loop and let the outer loop pick it up.
        #    For each %R, capture the raw line text alongside the parsed
        #    cells so callers can recover byte-faithful provenance later.
        rows: list[list[str]] = []
        raws: list[str] = []
        line = next(stream, "")
        while line and not line.startswith("%E"):
            if line.startswith("%T\t"):
                break
            elif line.startswith("%R\t"):
                raws.append(line.rstrip("\r\n"))
                rows.append(_split_tab(line)[1:])
            else:
                raise ValueError(
                    f"unexpected line in {name} rows: {line!r}"
                )
            line = next(stream, "")

        yield Table(name=name, fields=fields, rows=rows, raws=raws)
    return line
