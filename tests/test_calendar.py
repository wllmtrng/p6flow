"""Calendar parser tests.

Two layers: synthetic blob exercises the parser in isolation; real
fixture exercises the full materialization path including epoch
decoding, split shifts, and holiday surfacing.
"""

from __future__ import annotations

from datetime import date, time
from pathlib import Path

import duckdb
import pytest

from p6flow.calendar import (
    P6_EPOCH,
    materialize_calendar_derived,
    parse_clndr_data,
)
from p6flow.loader import load_xer
from p6flow.tokenizer import parse_tables

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "synthetic_v2212.xer"


# ---- Synthetic ----------------------------------------------------


def test_parses_minimal_blob():
    """5-day calendar, single 8-hour segment per workday, two holidays."""
    blob = (
        b"(0||CalendarData()("
        b"(0||DaysOfWeek()("
        b"(0||1()())"
        b"(0||2()((0||0(s|08:00|f|16:00)())))"
        b"(0||3()((0||0(s|08:00|f|16:00)())))"
        b"(0||4()((0||0(s|08:00|f|16:00)())))"
        b"(0||5()((0||0(s|08:00|f|16:00)())))"
        b"(0||6()((0||0(s|08:00|f|16:00)())))"
        b"(0||7()())"
        b"))"
        b"(0||VIEW(ShowTotal|N)())"
        b"(0||Exceptions()("
        b"(0||0(d|38537)())"
        b"(0||1(d|38502)())"
        b"))"
        b"))"
    )
    payload = parse_clndr_data(blob)
    assert len(payload.weekly) == 5
    assert {s.day_of_week for s in payload.weekly} == {2, 3, 4, 5, 6}
    for s in payload.weekly:
        assert s.start_time == time(8, 0)
        assert s.end_time == time(16, 0)
        assert s.hours == 8.0
        assert s.segment_idx == 0
    assert len(payload.exceptions) == 2
    # 38536 = 1899-12-30 + 38536d = 2005-07-04 (US Independence Day)
    assert payload.exceptions[0].exception_date == date(2005, 7, 4)
    assert payload.exceptions[0].segments == ()
    assert payload.exceptions[0].hours_worked == 0.0


def test_split_shift_segments():
    """Day with two segments (e.g. AM + PM around lunch)."""
    blob = (
        "(0||CalendarData()("
        "(0||DaysOfWeek()("
        "(0||2()("
        "(0||0(s|08:00|f|12:00)())"
        "(0||1(s|13:00|f|17:00)())"
        "))"
        "))"
        "))"
    )
    payload = parse_clndr_data(blob)
    assert len(payload.weekly) == 2
    assert payload.weekly[0].segment_idx == 0
    assert payload.weekly[0].hours == 4.0
    assert payload.weekly[1].segment_idx == 1
    assert payload.weekly[1].hours == 4.0


def test_p6_epoch_origin():
    """Locked at 1899-12-30 (Excel epoch). Day 38537 must be 2005-07-04
    (validated against multiple US 5-day calendars in the fixture)."""
    from datetime import timedelta
    assert P6_EPOCH == date(1899, 12, 30)
    assert P6_EPOCH + timedelta(days=38537) == date(2005, 7, 4)


def test_malformed_attrs_raises():
    """Odd attr field count is a hard error, not a silent skip."""
    blob = "(0||X(a|1|b)())"
    with pytest.raises(ValueError, match="odd number of attr fields"):
        parse_clndr_data(blob)


def test_unbalanced_parens_raises():
    """Open paren never closes -> ValueError, not IndexError."""
    blob = "(0||X("
    with pytest.raises(ValueError, match="unbalanced parens"):
        parse_clndr_data(blob)


# ---- Fixture integration ------------------------------------------


@pytest.fixture(scope="module")
def loaded_db():
    if not FIXTURE.exists():
        pytest.skip(f"fixture not found: {FIXTURE}")
    con = duckdb.connect(":memory:")
    load_xer(con, list(parse_tables(FIXTURE)))
    counts = materialize_calendar_derived(con)
    return con, counts


def test_derived_tables_created(loaded_db):
    con, counts = loaded_db
    tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    assert "CALENDAR_WEEKLY" in tables
    assert "CALENDAR_EXCEPTION" in tables
    assert counts["CALENDAR_WEEKLY"] > 0
    assert counts["CALENDAR_EXCEPTION"] > 0


def test_us_5day_calendar_shape(loaded_db):
    """The synthetic 5-day calendar (clndr_id=1) is Mon-Fri 08:00-16:00,
    one segment per day, 8h each."""
    con, _ = loaded_db
    rows = con.execute(
        "SELECT day_of_week, segment_idx, start_time, end_time, hours "
        "FROM CALENDAR_WEEKLY WHERE clndr_id=1 "
        "ORDER BY day_of_week, segment_idx"
    ).fetchall()
    assert len(rows) == 5
    days = [r[0] for r in rows]
    assert days == [2, 3, 4, 5, 6]  # Mon-Fri
    for _, seg_idx, start, end, hours in rows:
        assert seg_idx == 0
        assert start == time(8, 0)
        assert end == time(16, 0)
        assert hours == 8.0


def test_us_5day_calendar_holidays_decode(loaded_db):
    """The synthetic calendar carries four 2025 US holidays as non-work
    exceptions; each must round-trip through the epoch decoder."""
    con, _ = loaded_db
    holidays = {
        r[0] for r in con.execute(
            "SELECT exception_date FROM CALENDAR_EXCEPTION WHERE clndr_id=1"
        ).fetchall()
    }
    assert date(2025, 5, 26) in holidays   # Memorial Day
    assert date(2025, 7, 4) in holidays    # Independence Day
    assert date(2025, 9, 1) in holidays    # Labor Day
    assert date(2025, 12, 25) in holidays  # Christmas Day


def test_weekly_total_matches_declared_for_clean_calendars(loaded_db):
    """For the synthetic 5-day calendar, week_hr_cnt is declared 40 and
    the parsed segments must sum to exactly that."""
    con, _ = loaded_db
    parsed_total, declared = con.execute(
        "SELECT SUM(w.hours), MAX(c.week_hr_cnt) "
        "FROM CALENDAR_WEEKLY w "
        "JOIN CALENDAR c USING (clndr_id) "
        "WHERE clndr_id=1"
    ).fetchone()
    assert parsed_total == declared == 40.0


def test_non_work_exception_has_null_segment(loaded_db):
    """Pure non-work holidays emit a single row with NULL segment_idx
    and hours_worked=0, so consumers can quickly count holidays."""
    con, _ = loaded_db
    n = con.execute(
        "SELECT COUNT(*) FROM CALENDAR_EXCEPTION "
        "WHERE clndr_id=1 AND segment_idx IS NULL AND hours_worked=0"
    ).fetchone()[0]
    assert n > 0
