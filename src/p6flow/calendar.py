"""Parser for P6's CALENDAR.clndr_data paren-tree blob.

The format is a small custom tree language. Each node looks like:

    (<flags>||<name>(<attrs>)(<children>))

where <flags> is a fixed prefix we ignore (`0`), <name> identifies the
node type or index, <attrs> is pipe-separated key|value pairs, and
<children> contains zero or more sibling nodes. Inter-node whitespace
is `\\x7f\\x7f` plus regular ASCII whitespace, all stripped.

Top-level layout:

    CalendarData
      DaysOfWeek
        1, 2, 3, 4, 5, 6, 7        (1=Sunday, 7=Saturday)
          0, 1, ...                 (segment index, attrs s|HH:MM|f|HH:MM)
      VIEW                          (display metadata, ignored)
      Exceptions
        0, 1, ...                   (exception index, attrs d|<epoch_days>)
          0, 1, ...                 (optional segments overriding the day)

P6 epoch is Excel's 1899-12-30, validated empirically against
calendars whose exception sets contained recognizable US holidays
(Independence Day, Christmas, Memorial Day, Labor Day): every
candidate origin except 1899-12-30 placed those dates a day or
more off the actual calendar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time, timedelta

import duckdb
import pyarrow as pa

P6_EPOCH = date(1899, 12, 30)


@dataclass(frozen=True)
class WeeklySegment:
    day_of_week: int  # 1=Sunday, 7=Saturday
    segment_idx: int
    start_time: time
    end_time: time
    hours: float


@dataclass(frozen=True)
class ExceptionDay:
    exception_date: date
    segments: tuple[WeeklySegment, ...]  # may be empty (full non-work day)
    hours_worked: float


@dataclass(frozen=True)
class CalendarPayload:
    weekly: tuple[WeeklySegment, ...]
    exceptions: tuple[ExceptionDay, ...]


# ---- Tree parsing -------------------------------------------------


@dataclass(frozen=True)
class _Node:
    name: str
    attrs: dict[str, str]
    children: tuple[_Node, ...]


def _strip_whitespace(s: str) -> str:
    out = []
    for ch in s:
        if ch in "\x7f \t\r\n":
            continue
        out.append(ch)
    return "".join(out)


def _read_balanced(s: str, i: int) -> tuple[str, int]:
    """Read the body of a balanced (...) starting at s[i]=='('.
    Return (body_without_outer_parens, index_after_closing_paren)."""
    if s[i] != "(":
        raise ValueError(f"expected '(' at {i}, got {s[i]!r}")
    depth = 1
    j = i + 1
    while depth > 0:
        if j >= len(s):
            raise ValueError(f"unbalanced parens starting at {i}")
        c = s[j]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        if depth > 0:
            j += 1
    return s[i + 1 : j], j + 1


def _parse_attrs(body: str) -> dict[str, str]:
    """Parse `k1|v1|k2|v2|...` into a dict. Empty body -> empty dict."""
    if not body:
        return {}
    parts = body.split("|")
    if len(parts) % 2 != 0:
        raise ValueError(f"odd number of attr fields: {body!r}")
    return dict(zip(parts[0::2], parts[1::2], strict=True))


# Real P6 clndr_data nests at most ~5 levels (week / day / exception).
# A hard cap defangs maliciously deep blobs that would otherwise blow
# Python's default recursion limit (~1000) and could segfault on builds
# with smaller stacks.
_MAX_NESTING_DEPTH = 64


def _parse_node(s: str, i: int, depth: int = 0) -> tuple[_Node, int]:
    """Parse one node starting at s[i]=='('."""
    if depth > _MAX_NESTING_DEPTH:
        raise ValueError(
            f"clndr_data nesting exceeds depth limit ({_MAX_NESTING_DEPTH})"
        )
    if s[i] != "(":
        raise ValueError(f"expected '(' at {i}")
    i += 1
    # Header runs to the first inner '('.
    j = s.index("(", i)
    header = s[i:j]
    # Header format: "<flags>||<name>". We tolerate missing prefix.
    name = header.split("||", 1)[-1]
    i = j
    attrs_body, i = _read_balanced(s, i)
    children_body, i = _read_balanced(s, i)
    if s[i] != ")":
        raise ValueError(f"expected ')' at {i}, got {s[i]!r}")
    i += 1

    children: list[_Node] = []
    k = 0
    while k < len(children_body):
        if children_body[k] == "(":
            child, k = _parse_node(children_body, k, depth + 1)
            children.append(child)
        else:
            raise ValueError(
                f"unexpected character {children_body[k]!r} between sibling nodes"
            )
    return _Node(name, _parse_attrs(attrs_body), tuple(children)), i


def _parse_root(s: str) -> _Node:
    s = _strip_whitespace(s)
    if not s:
        raise ValueError("empty clndr_data after whitespace strip")
    root, end = _parse_node(s, 0)
    if end != len(s):
        raise ValueError(f"trailing data after root: {s[end:end+20]!r}")
    return root


# ---- Domain extraction --------------------------------------------


def _parse_time(hhmm: str) -> time:
    """`08:00` -> time(8, 0). P6 also emits `24:00` for midnight-rollover
    (a full-day segment finishing at end of day); normalize that to
    23:59:59.999 since `time` doesn't accept hour=24."""
    hh, mm = hhmm.split(":")
    h, m = int(hh), int(mm)
    if h == 24 and m == 0:
        return time(23, 59, 59, 999000)
    return time(h, m)


def _hours_between(start: time, end: time) -> float:
    """Decimal hours from start to end on the same day."""

    def _to_seconds(t: time) -> int:
        return t.hour * 3600 + t.minute * 60 + t.second + t.microsecond // 1000

    return (_to_seconds(end) - _to_seconds(start)) / 3600.0


def _extract_segments(parent: _Node) -> list[WeeklySegment]:
    """Walk a day or exception node's children to pull work segments.

    Caller fills in `day_of_week` after; we return per-segment tuples
    with day_of_week=0 as a placeholder.
    """
    out: list[WeeklySegment] = []
    for seg in parent.children:
        if "s" not in seg.attrs or "f" not in seg.attrs:
            continue
        start = _parse_time(seg.attrs["s"])
        end = _parse_time(seg.attrs["f"])
        out.append(
            WeeklySegment(
                day_of_week=0,
                segment_idx=int(seg.name),
                start_time=start,
                end_time=end,
                hours=_hours_between(start, end),
            )
        )
    return out


def _find_child(parent: _Node, name: str) -> _Node | None:
    for c in parent.children:
        if c.name == name:
            return c
    return None


def parse_clndr_data(blob: bytes | str) -> CalendarPayload:
    """Parse a CALENDAR.clndr_data blob into typed weekly + exception rows.

    Accepts bytes (decoded as latin-1 since the format is ASCII-only)
    or a pre-decoded string.
    """
    text = blob.decode("latin-1") if isinstance(blob, bytes) else blob
    root = _parse_root(text)

    weekly: list[WeeklySegment] = []
    days = _find_child(root, "DaysOfWeek")
    if days is not None:
        for day_node in days.children:
            day_of_week = int(day_node.name)
            for seg in _extract_segments(day_node):
                weekly.append(
                    WeeklySegment(
                        day_of_week=day_of_week,
                        segment_idx=seg.segment_idx,
                        start_time=seg.start_time,
                        end_time=seg.end_time,
                        hours=seg.hours,
                    )
                )

    exceptions: list[ExceptionDay] = []
    exc = _find_child(root, "Exceptions")
    if exc is not None:
        for ex_node in exc.children:
            if "d" not in ex_node.attrs:
                continue
            epoch_days = int(ex_node.attrs["d"])
            ex_date = P6_EPOCH + timedelta(days=epoch_days)
            segs = _extract_segments(ex_node)
            hours_worked = sum(s.hours for s in segs)
            exceptions.append(
                ExceptionDay(
                    exception_date=ex_date,
                    segments=tuple(segs),
                    hours_worked=hours_worked,
                )
            )

    return CalendarPayload(weekly=tuple(weekly), exceptions=tuple(exceptions))


# ---- DuckDB materialization ---------------------------------------


def materialize_calendar_derived(
    con: duckdb.DuckDBPyConnection,
) -> dict[str, int]:
    """Parse every CALENDAR.clndr_data blob and create two derived tables:

      CALENDAR_WEEKLY    (clndr_id, day_of_week, segment_idx, start_time,
                          end_time, hours)
      CALENDAR_EXCEPTION (clndr_id, exception_date, segment_idx, start_time,
                          end_time, hours_worked, is_full_segment)

    Exception rows mirror weekly: one row per work segment, plus one
    summary row per exception day with segment_idx=NULL when the day
    is a pure non-work day (so consumers can quickly filter holidays).

    Returns row counts for both tables. Skips silently if CALENDAR
    isn't loaded (legacy XER without it).
    """
    loaded = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    if "CALENDAR" not in loaded:
        return {}

    weekly_rows: list[dict] = []
    exception_rows: list[dict] = []

    for clndr_id, blob in con.execute(
        "SELECT clndr_id, clndr_data FROM CALENDAR WHERE clndr_data IS NOT NULL"
    ).fetchall():
        payload = parse_clndr_data(blob)
        for seg in payload.weekly:
            weekly_rows.append({
                "clndr_id": clndr_id,
                "day_of_week": seg.day_of_week,
                "segment_idx": seg.segment_idx,
                "start_time": seg.start_time,
                "end_time": seg.end_time,
                "hours": seg.hours,
            })
        for ex in payload.exceptions:
            if not ex.segments:
                # Pure non-work day. Single row with NULL segment fields.
                exception_rows.append({
                    "clndr_id": clndr_id,
                    "exception_date": ex.exception_date,
                    "segment_idx": None,
                    "start_time": None,
                    "end_time": None,
                    "hours_worked": 0.0,
                })
            else:
                for seg in ex.segments:
                    exception_rows.append({
                        "clndr_id": clndr_id,
                        "exception_date": ex.exception_date,
                        "segment_idx": seg.segment_idx,
                        "start_time": seg.start_time,
                        "end_time": seg.end_time,
                        "hours_worked": seg.hours,
                    })

    weekly_schema = pa.schema([
        ("clndr_id", pa.int64()),
        ("day_of_week", pa.int32()),
        ("segment_idx", pa.int32()),
        ("start_time", pa.time64("us")),
        ("end_time", pa.time64("us")),
        ("hours", pa.float64()),
    ])
    exception_schema = pa.schema([
        ("clndr_id", pa.int64()),
        ("exception_date", pa.date32()),
        ("segment_idx", pa.int32()),
        ("start_time", pa.time64("us")),
        ("end_time", pa.time64("us")),
        ("hours_worked", pa.float64()),
    ])

    weekly_table = pa.Table.from_pylist(weekly_rows, schema=weekly_schema)
    exception_table = pa.Table.from_pylist(exception_rows, schema=exception_schema)

    con.register("calendar_weekly_raw", weekly_table)
    con.register("calendar_exception_raw", exception_table)
    try:
        con.execute(
            "CREATE OR REPLACE TABLE CALENDAR_WEEKLY AS "
            "SELECT * FROM calendar_weekly_raw"
        )
        con.execute(
            "CREATE OR REPLACE TABLE CALENDAR_EXCEPTION AS "
            "SELECT * FROM calendar_exception_raw"
        )
    finally:
        con.unregister("calendar_weekly_raw")
        con.unregister("calendar_exception_raw")

    return {
        "CALENDAR_WEEKLY": len(weekly_rows),
        "CALENDAR_EXCEPTION": len(exception_rows),
    }
