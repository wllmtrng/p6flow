"""Generate a hand-crafted XER fixture that exercises every behavior
the integration tests rely on. Output: fixtures/synthetic_v2212.xer.

Replaces the real-world XER samples used during early development with
a self-contained, byte-deterministic fixture anyone can regenerate.
The synthetic file carries:

  - ERMHDR v22.12 with a generic export date and user
  - PROJECT, PROJWBS (incl. legacy plan_open_state), TASK
    (with crt_path_num legacy column + a critical-path task), TASKPRED
  - CALENDAR with a 5-day Mon-Fri 8h schedule + four 2025 US holidays
    encoded in clndr_data (Memorial Day, July 4th, Labor Day, Christmas)
  - ACTVTYPE row with a deliberately orphaned proj_id=429 to trigger
    one FK violation (matching the existing validator test expectation)
  - UDFTYPE + UDFVALUE pairs covering FT_TEXT labels including the
    canonical 'MSP Activity ID' and 'Notes' that the UDF pivot tests
    look for

Run: PYTHONPATH=src .venv/bin/python scripts/make_synthetic_fixture.py
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

P6_EPOCH = date(1899, 12, 30)


def _epoch(d: date) -> int:
    return (d - P6_EPOCH).days


# ---- CALENDAR.clndr_data --------------------------------------------

# 5-day Mon-Fri (P6 days 2..6) one segment each, 08:00-16:00.
# Plus four 2025 US holidays as non-work exceptions.
HOLIDAYS_2025 = [
    date(2025, 5, 26),   # Memorial Day
    date(2025, 7, 4),    # Independence Day
    date(2025, 9, 1),    # Labor Day
    date(2025, 12, 25),  # Christmas Day
]


def _build_clndr_data() -> str:
    days_block = "(0||DaysOfWeek()("
    for d in range(1, 8):
        if 2 <= d <= 6:
            days_block += f"(0||{d}()((0||0(s|08:00|f|16:00)())))"
        else:
            # Sun (1), Sat (7): no work
            days_block += f"(0||{d}()())"
    days_block += "))"

    exc_inner = "".join(
        f"(0||{i}(d|{_epoch(h)})())" for i, h in enumerate(HOLIDAYS_2025)
    )
    exc_block = f"(0||Exceptions()({exc_inner}))"

    return f"(0||CalendarData()({days_block}(0||VIEW(ShowTotal|N)()){exc_block}))"


# ---- XER body ------------------------------------------------------


def _t(name: str) -> str:
    return f"%T\t{name}\n"


def _f(*cols: str) -> str:
    return "%F\t" + "\t".join(cols) + "\n"


def _r(*cells: str) -> str:
    return "%R\t" + "\t".join(cells) + "\n"


def build() -> str:
    out: list[str] = []
    out.append(
        "ERMHDR\t22.12\t2025-07-15\tProject\tADMIN\tTest User\t"
        "dbxDatabaseNoName\tProject Management\n"
    )

    # PROJECT: minimal — proj_id is enough to satisfy FK targets and
    # PROJECT row presence checks.
    out.append(_t("PROJECT"))
    out.append(_f("proj_id", "proj_short_name", "plan_start_date", "fy_start_month_num"))
    out.append(_r("100", "SYNTH-DEMO", "2025-01-01 08:00", "1"))

    # ACTVTYPE: orphan FK — proj_id=429 has no parent in PROJECT.
    out.append(_t("ACTVTYPE"))
    out.append(_f("actv_code_type_id", "proj_id", "actv_code_type"))
    out.append(_r("1", "429", "SYNTH-CODE-TYPE"))

    # PROJWBS: hierarchy with two leaves under root. Includes the
    # `plan_open_state` legacy column (XER-only, not in pmSchema.xml).
    out.append(_t("PROJWBS"))
    out.append(_f(
        "wbs_id", "proj_id", "obs_id", "seq_num", "proj_node_flag",
        "wbs_short_name", "wbs_name", "parent_wbs_id", "plan_open_state",
    ))
    out.append(_r("1", "100", "1", "1", "Y", "ROOT", "Synthetic Root WBS", "", ""))
    out.append(_r("2", "100", "1", "2", "N", "L1A", "Phase A", "1", ""))
    out.append(_r("3", "100", "1", "3", "N", "L1B", "Phase B", "1", ""))

    # CALENDAR: one row, clndr_id=1, with the encoded weekly + holidays.
    clndr_data = _build_clndr_data()
    out.append(_t("CALENDAR"))
    out.append(_f(
        "clndr_id", "default_flag", "clndr_name", "clndr_type",
        "day_hr_cnt", "week_hr_cnt", "month_hr_cnt", "year_hr_cnt",
        "clndr_data",
    ))
    out.append(_r("1", "Y", "Synthetic 5-Day", "CA_Base",
                  "8.0", "40.0", "172.0", "2000.0", clndr_data))

    # TASK: three rows under the two leaf WBSs. crt_path_num is the
    # legacy column tested by test_load.py. Include one task with
    # total_float_hr_cnt = 0 (critical path) and one with > 0.
    out.append(_t("TASK"))
    out.append(_f(
        "task_id", "proj_id", "wbs_id", "clndr_id",
        "task_code", "task_name", "task_type", "duration_type",
        "status_code", "total_float_hr_cnt", "free_float_hr_cnt",
        "remain_drtn_hr_cnt", "target_drtn_hr_cnt",
        "target_start_date", "target_end_date", "crt_path_num",
    ))
    out.append(_r(
        "1001", "100", "2", "1",
        "A1010", "Pour foundation", "TT_Task", "DT_FixedDrtn",
        "TK_Active", "0", "0",
        "80", "80",
        "2025-07-15 08:00", "2025-07-25 16:00", "1",
    ))
    out.append(_r(
        "1002", "100", "2", "1",
        "A1020", "Frame walls", "TT_Task", "DT_FixedDrtn",
        "TK_Active", "16", "8",
        "120", "120",
        "2025-07-26 08:00", "2025-08-10 16:00", "2",
    ))
    out.append(_r(
        "1003", "100", "3", "1",
        "B2010", "Inspect site", "TT_Task", "DT_FixedDrtn",
        "TK_NotStart", "-8", "0",
        "16", "16",
        "2025-08-11 08:00", "2025-08-13 16:00", "3",
    ))

    # TASKPRED: predecessor links.
    out.append(_t("TASKPRED"))
    out.append(_f(
        "task_pred_id", "task_id", "pred_task_id", "proj_id",
        "pred_proj_id", "pred_type", "lag_hr_cnt",
    ))
    out.append(_r("9001", "1002", "1001", "100", "100", "PR_FS", "0"))
    out.append(_r("9002", "1003", "1002", "100", "100", "PR_FS", "0"))

    # UDFTYPE: one TASK-scoped UDF per label tested by test_udf.py.
    out.append(_t("UDFTYPE"))
    out.append(_f(
        "udf_type_id", "table_name", "udf_type_name",
        "udf_type_label", "logical_data_type", "super_flag",
    ))
    out.append(_r("201", "TASK", "user_field_201", "MSP Activity ID", "FT_TEXT", "N"))
    out.append(_r("202", "TASK", "user_field_202", "Notes", "FT_TEXT", "N"))
    out.append(_r("203", "TASK", "user_field_203", "Text_01", "FT_TEXT", "N"))

    # UDFVALUE: a few rows so the pivot has actual cells to surface.
    out.append(_t("UDFVALUE"))
    out.append(_f(
        "udf_type_id", "fk_id", "proj_id",
        "udf_text", "udf_number", "udf_date", "udf_code_id",
    ))
    out.append(_r("201", "1001", "100", "MSP-A1010", "", "", ""))
    out.append(_r("202", "1001", "100", "Critical path start", "", "", ""))
    out.append(_r("201", "1002", "100", "MSP-A1020", "", "", ""))
    out.append(_r("203", "1003", "100", "Phase B kickoff", "", "", ""))

    out.append("%E\n")
    return "".join(out)


def main() -> None:
    body = build()
    out_path = (
        Path(__file__).resolve().parent.parent
        / "fixtures" / "synthetic_v2212.xer"
    )
    out_path.write_text(body, encoding="cp1252")
    print(f"wrote {out_path} ({len(body)} bytes)")


if __name__ == "__main__":
    main()
