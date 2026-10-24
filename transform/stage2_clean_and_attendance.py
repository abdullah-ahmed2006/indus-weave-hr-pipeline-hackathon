"""PeopleOps hackathon: Stage 2 cleaning + Stage 3 attendance (SQLite).

Reads only the raw SQLite tables and writes derived stg_* tables. It does not
modify CSV/JSON files or raw_* tables. The DEV-SITE-01 -60 minute correction is
based on the sampled roster-boundary diagnostic recorded by the project owner;
review docs/clock_correction_evidence.csv before presenting it as conclusive.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, date, timedelta
from pathlib import Path

import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_DIR / "data" / "peopleops.sqlite"
DOCS_DIR = PROJECT_DIR / "docs"
DOCS_DIR.mkdir(parents=True, exist_ok=True)

PERIOD_START = pd.Timestamp("2026-04-01")
PERIOD_END = pd.Timestamp("2026-09-30")
CLOCK_CORRECTION_DEVICE = "DEV-SITE-01"
CLOCK_CORRECTION_MINUTES = -60

DATE_FORMATS = (
    "%Y-%m-%d", "%d/%m/%Y", "%d-%b-%Y", "%d-%B-%Y",
    "%d/%b/%Y", "%d/%B/%Y", "%Y/%m/%d",
)


def parse_one_date(value):
    if value is None or pd.isna(value):
        return pd.NaT
    text = str(value).strip()
    if not text or text.lower() in {"none", "null", "nan", "nat"}:
        return pd.NaT
    for fmt in DATE_FORMATS:
        try:
            return pd.Timestamp(datetime.strptime(text, fmt).date())
        except ValueError:
            pass
    # Last-resort parser for uncommon but valid values.
    parsed = pd.to_datetime(text, errors="coerce", format="mixed", dayfirst=True)
    return pd.Timestamp(parsed) if not pd.isna(parsed) else pd.NaT


def parse_dates(series):
    return series.map(parse_one_date)


def clean_text(df):
    df = df.copy()
    for col in df.columns:
        if df[col].dtype == "object" or isinstance(df[col].dtype, pd.StringDtype):
            df[col] = df[col].map(
                lambda x: x.strip() if isinstance(x, str) else x
            )
    return df


def norm(value):
    if value is None or pd.isna(value):
        return ""
    return " ".join(str(value).strip().casefold().split())


def iso_date(series):
    return series.map(lambda x: x.strftime("%Y-%m-%d") if not pd.isna(x) else None)


def iso_datetime(series):
    parsed = pd.to_datetime(series, errors="coerce", format="mixed")
    return parsed.map(lambda x: x.strftime("%Y-%m-%d %H:%M:%S") if not pd.isna(x) else None)


def _sqlite_safe_value(value):
    """Convert pandas/Python values into SQLite-compatible values."""
    import numpy as np

    if value is None:
        return None

    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat(sep=" ")

    if isinstance(value, datetime):
        return value.isoformat(sep=" ")

    if isinstance(value, date):
        return value.isoformat()

    if isinstance(value, pd.Timedelta):
        return str(value)

    try:
        missing = pd.isna(value)
        if isinstance(missing, (bool, np.bool_)) and missing:
            return None
    except (TypeError, ValueError):
        pass

    # Convert NumPy scalar values into standard Python scalars.
    if isinstance(value, np.generic):
        value = value.item()

    return value


def write_table(conn, name, df):
    """Write a DataFrame to SQLite using safe scalar values."""
    out = df.copy()

    for column in out.columns:
        out[column] = out[column].map(_sqlite_safe_value)

    out.to_sql(
        name,
        conn,
        if_exists="replace",
        index=False,
        chunksize=20000,
    )

def read_raw(conn, table):
    return pd.read_sql_query(f'SELECT * FROM "{table}"', conn)


def plant_code(value):
    s = norm(value)
    if s in {"head office", "ho", "head office (karachi)"}:
        return "HO"
    if s in {"korangi plant", "korangi", "kor"}:
        return "KOR"
    if s in {"site plant", "site", "site plant (karachi)"}:
        return "SITE"
    if s in {"faisalabad plant", "faisalabad", "fsd"}:
        return "FSD"
    return ""


def date_key(value):
    if value is None or pd.isna(value):
        return None
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def main():
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Raw SQLite database not found: {DB_PATH}")

    with sqlite3.connect(DB_PATH) as conn:
        print("=" * 68)
        print("PEOPLEOPS STAGE 2 + STAGE 3: CLEANING AND ATTENDANCE")
        print("Database:", DB_PATH)
        print("=" * 68)

        # --------------------------------------------------------------
        # A. Departments: normalize names, preserve IDs and rename metadata.
        # --------------------------------------------------------------
        departments = clean_text(read_raw(conn, "raw_departments"))
        departments["plant_normalized"] = departments["plant"].map(norm)
        departments["dept_name_normalized"] = departments["dept_name"].map(norm)
        departments["previous_name_normalized"] = departments["previous_name"].map(norm)
        if "renamed_on" in departments:
            departments["renamed_on"] = iso_date(parse_dates(departments["renamed_on"]))

        dept_alias = {}
        for row in departments.to_dict("records"):
            plant_n = row["plant_normalized"]
            current = row["dept_name_normalized"]
            previous = row["previous_name_normalized"]
            dept_alias.setdefault((plant_n, current), row["dept_id"])
            if previous:
                dept_alias.setdefault((plant_n, previous), row["dept_id"])
        write_table(conn, "stg_departments", departments)

        # --------------------------------------------------------------
        # B. Employees: latest updated_at wins; write a dedupe audit log.
        # --------------------------------------------------------------
        employees = clean_text(read_raw(conn, "raw_employees"))
        employees["_updated_at_parsed"] = pd.to_datetime(
            employees["updated_at"], errors="coerce", format="mixed"
        )
        employees = employees.sort_values(
            ["emp_id", "_updated_at_parsed", "_source_row_number"],
            na_position="first",
        )
        duplicate_mask = employees.duplicated("emp_id", keep="last")
        dropped_employees = employees.loc[duplicate_mask].copy()
        kept_employees = employees.loc[~duplicate_mask].copy()
        winner_info = kept_employees.set_index("emp_id")[["updated_at", "_source_row_number"]]
        if not dropped_employees.empty:
            dropped_employees["kept_updated_at"] = dropped_employees["emp_id"].map(winner_info["updated_at"])
            dropped_employees["dedupe_reason"] = "Same emp_id; kept latest updated_at"
        write_table(conn, "stg_employee_dedup_log", dropped_employees)

        for col in ["date_of_birth", "hire_date", "separation_date"]:
            kept_employees[col] = iso_date(parse_dates(kept_employees[col]))
        kept_employees["updated_at"] = iso_datetime(kept_employees["updated_at"])
        kept_employees["plant"] = kept_employees["plant"].map(
            lambda x: {"head office": "Head Office", "korangi plant": "Korangi Plant",
                       "site plant": "SITE Plant", "faisalabad plant": "Faisalabad Plant"}.get(norm(x), x)
        )
        kept_employees["plant_code"] = kept_employees["plant"].map(plant_code)
        kept_employees["department_normalized"] = kept_employees["department"].map(norm)
        kept_employees["dept_id_from_profile"] = kept_employees.apply(
            lambda r: dept_alias.get((norm(r["plant"]), norm(r["department"]))), axis=1
        )
        kept_employees = kept_employees.drop(columns=["_updated_at_parsed"])
        write_table(conn, "stg_employees", kept_employees)

        # --------------------------------------------------------------
        # C. Employment history: export effective_to is kept but not trusted.
        # --------------------------------------------------------------
        history = clean_text(read_raw(conn, "raw_employment_history"))
        history["effective_from_parsed"] = parse_dates(history["effective_from"])
        history["export_effective_to"] = history["effective_to"]
        history = history.sort_values(["emp_id", "effective_from_parsed", "_source_row_number"])
        same_day_duplicates = history.duplicated(["emp_id", "effective_from_parsed"], keep="last")
        history_dups = history.loc[same_day_duplicates].copy()
        if not history_dups.empty:
            history_dups["dedupe_reason"] = "Same emp_id and effective_from; kept last source row"
        write_table(conn, "stg_employment_history_dedup_log", history_dups)
        history = history.loc[~same_day_duplicates].copy()
        history = history.sort_values(["emp_id", "effective_from_parsed"])
        history["next_effective_from"] = history.groupby("emp_id")["effective_from_parsed"].shift(-1)
        history["valid_from"] = history["effective_from_parsed"]
        history["valid_to"] = history["next_effective_from"] - pd.Timedelta(days=1)
        history["effective_from"] = iso_date(history["valid_from"])
        history["valid_from"] = iso_date(history["valid_from"])
        history["valid_to"] = iso_date(history["valid_to"])
        history = history.drop(columns=["effective_from_parsed", "next_effective_from"])
        write_table(conn, "stg_employment_history", history)

        # --------------------------------------------------------------
        # D. Shift roster: carry previous shift through missing weeks.
        # --------------------------------------------------------------
        roster = clean_text(read_raw(conn, "raw_shift_roster"))
        roster["week_start"] = parse_dates(roster["week_start"])
        roster = roster.dropna(subset=["emp_id", "week_start"])
        roster = roster.sort_values(["emp_id", "week_start", "_source_row_number"])
        roster_dups = roster.duplicated(["emp_id", "week_start"], keep="last")
        roster_dedup_log = roster.loc[roster_dups].copy()
        write_table(conn, "stg_roster_duplicate_log", roster_dedup_log)
        roster = roster.loc[~roster_dups, ["emp_id", "week_start", "shift_code"]]
        min_week = roster["week_start"].min()
        max_week = roster["week_start"].max()
        all_weeks = pd.date_range(min_week, max_week, freq="7D")
        all_emps = roster["emp_id"].drop_duplicates().tolist()
        grid = pd.MultiIndex.from_product(
            [all_emps, all_weeks], names=["emp_id", "week_start"]
        ).to_frame(index=False)
        roster = grid.merge(roster, on=["emp_id", "week_start"], how="left")
        roster = roster.sort_values(["emp_id", "week_start"])
        roster["was_roster_week_missing"] = roster["shift_code"].isna()
        roster["shift_code"] = roster.groupby("emp_id")["shift_code"].ffill()
        roster["shift_carried_forward"] = roster["was_roster_week_missing"] & roster["shift_code"].notna()
        roster_for_db = roster.copy()
        roster_for_db["week_start"] = iso_date(roster_for_db["week_start"])
        write_table(conn, "stg_shift_roster", roster_for_db)

        # --------------------------------------------------------------
        # E. Shifts, holidays and device mapping.
        # --------------------------------------------------------------
        shifts = clean_text(read_raw(conn, "raw_shifts"))
        if "_raw_record_json" in shifts:
            shifts = shifts.drop(columns=["_raw_record_json"])
        shifts["start_minutes"] = shifts["start_time"].map(
            lambda x: int(str(x).split(":")[0]) * 60 + int(str(x).split(":")[1])
        )
        shifts["end_minutes"] = shifts["end_time"].map(
            lambda x: int(str(x).split(":")[0]) * 60 + int(str(x).split(":")[1])
        )
        shifts["grace_minutes"] = pd.to_numeric(shifts["grace_minutes"], errors="coerce")
        write_table(conn, "stg_shifts", shifts)

        holidays = clean_text(read_raw(conn, "raw_holidays"))
        holidays["holiday_date"] = iso_date(parse_dates(holidays["holiday_date"]))
        holidays["scope"] = holidays["scope"].str.upper()
        write_table(conn, "stg_holidays", holidays)
        holiday_keys = set(zip(holidays["holiday_date"], holidays["scope"]))

        mapping = clean_text(read_raw(conn, "raw_device_mapping"))
        mapping = mapping.drop_duplicates(["device_id", "device_user_id"], keep="last")
        write_table(conn, "stg_device_mapping", mapping)

        # --------------------------------------------------------------
        # F. Punches: join by both device fields, correct SITE clock, dedupe.
        # --------------------------------------------------------------
        punches = clean_text(read_raw(conn, "raw_biometric_punches"))
        punches["original_punch_time"] = pd.to_datetime(
            punches["punch_time"], errors="coerce", format="mixed"
        )
        punches = punches.merge(
            mapping[["device_id", "device_user_id", "emp_id"]],
            on=["device_id", "device_user_id"], how="left", validate="many_to_one"
        )
        rejected = punches.loc[punches["emp_id"].isna()].copy()
        rejected["rejection_reason"] = "No mapping for device_id + device_user_id; contractor/visitor per business rule"
        write_table(conn, "stg_rejected_punches", rejected)

        mapped_punches = punches.loc[punches["emp_id"].notna()].copy()
        mapped_punches["device_clock_correction_minutes"] = mapped_punches["device_id"].map(
            lambda d: CLOCK_CORRECTION_MINUTES if d == CLOCK_CORRECTION_DEVICE else 0
        )
        mapped_punches["punch_time"] = mapped_punches["original_punch_time"] + pd.to_timedelta(
            mapped_punches["device_clock_correction_minutes"], unit="m"
        )
        mapped_punches = mapped_punches.sort_values(["emp_id", "punch_time", "punch_id"])
        mapped_punches["seconds_since_previous_employee_punch"] = (
            mapped_punches.groupby("emp_id")["punch_time"].diff().dt.total_seconds()
        )
        duplicate_punch_mask = mapped_punches["seconds_since_previous_employee_punch"].between(0, 120, inclusive="both")
        duplicate_punches = mapped_punches.loc[duplicate_punch_mask].copy()
        duplicate_punches["rejection_reason"] = "Same employee has a previous punch within 2 minutes"
        write_table(conn, "stg_duplicate_punches", duplicate_punches)
        clean_punches = mapped_punches.loc[~duplicate_punch_mask].copy()
        clean_punches["original_punch_time"] = iso_datetime(clean_punches["original_punch_time"])
        clean_punches["punch_time"] = iso_datetime(clean_punches["punch_time"])
        write_table(conn, "stg_punches_clean", clean_punches)

        clock_evidence = pd.DataFrame([{
            "device_id": CLOCK_CORRECTION_DEVICE,
            "correction_minutes_applied": CLOCK_CORRECTION_MINUTES,
            "sample_median_distance_before_minutes": 41.93,
            "sample_median_distance_after_minutes": 35.89,
            "other_devices_best_tested_correction_minutes": 0,
            "method": "Sampled roster-based distance to shift boundaries; 10,000 punches per device where available",
            "decision_note": "DEV-SITE-01 was the only sampled outlier improved by -60 minutes; improvement is directional but modest, so validate in final demo and retain this evidence log.",
        }])
        write_table(conn, "stg_clock_correction_evidence", clock_evidence)
        clock_evidence.to_csv(DOCS_DIR / "clock_correction_evidence.csv", index=False)

        # --------------------------------------------------------------
        # G. Leave JSON: one request table, one approval table, one leave-day table.
        # --------------------------------------------------------------
        raw_leave = read_raw(conn, "raw_leave_requests")
        requests = []
        approval_rows = []
        for _, raw_row in raw_leave.iterrows():
            if "_raw_record_json" in raw_leave.columns and pd.notna(raw_row.get("_raw_record_json")):
                payload = json.loads(raw_row["_raw_record_json"])
            else:
                # Fallback for hand-built test databases; production raw ingestion
                # preserves the complete original JSON in _raw_record_json.
                def maybe_json(value):
                    if isinstance(value, str):
                        try:
                            return json.loads(value)
                        except json.JSONDecodeError:
                            return value
                    return value
                payload = {
                    "request_id": raw_row.get("request_id"),
                    "employee": maybe_json(raw_row.get("employee")) or {},
                    "leave": maybe_json(raw_row.get("leave")) or {},
                    "submitted_at": raw_row.get("submitted_at"),
                    "approvals": maybe_json(raw_row.get("approvals")) or [],
                    "cancelled_at": raw_row.get("cancelled_at"),
                }
            employee = payload.get("employee") or {}
            leave = payload.get("leave") or {}
            request_id = payload.get("request_id")
            cancelled_at = payload.get("cancelled_at")
            approvals = payload.get("approvals") or []
            for approval in approvals:
                approval_rows.append({
                    "request_id": request_id,
                    "step": approval.get("step"),
                    "approver_role": approval.get("approver_role"),
                    "approver_id": approval.get("approver_id"),
                    "action": approval.get("action"),
                    "acted_at": approval.get("acted_at"),
                })
            latest_role_action = {}
            by_role = {}
            for approval in approvals:
                role = norm(approval.get("approver_role"))
                if role not in {"manager", "hr"}:
                    continue
                by_role.setdefault(role, []).append(approval)
            for role, events in by_role.items():
                events.sort(key=lambda x: str(x.get("acted_at") or ""))
                latest_role_action[role] = str(events[-1].get("action", "")).strip().casefold()
            is_approved = (
                latest_role_action.get("manager") == "approved"
                and latest_role_action.get("hr") == "approved"
                and cancelled_at in (None, "")
            )
            requests.append({
                "request_id": request_id,
                "emp_id": employee.get("emp_id"),
                "plant": employee.get("plant"),
                "plant_code": plant_code(employee.get("plant")),
                "leave_type": leave.get("type"),
                "leave_from": date_key(parse_one_date(leave.get("from"))),
                "leave_to": date_key(parse_one_date(leave.get("to"))),
                "days_requested_calendar": leave.get("days_requested"),
                "submitted_at": payload.get("submitted_at"),
                "cancelled_at": cancelled_at,
                "manager_latest_action": latest_role_action.get("manager"),
                "hr_latest_action": latest_role_action.get("hr"),
                "is_approved": bool(is_approved),
            })

        leave_requests = pd.DataFrame(requests)
        leave_approvals = pd.DataFrame(approval_rows)
        write_table(conn, "stg_leave_requests", leave_requests)
        write_table(conn, "stg_leave_approval_steps", leave_approvals)

        leave_day_rows = []
        for request in requests:
            if not request["is_approved"] or not request["leave_from"] or not request["leave_to"]:
                continue
            start = pd.Timestamp(request["leave_from"])
            end = pd.Timestamp(request["leave_to"])
            if end < start:
                continue
            scope_code = request["plant_code"]
            for leave_day in pd.date_range(start, end, freq="D"):
                iso = leave_day.strftime("%Y-%m-%d")
                if leave_day.day_of_week == 6:
                    continue
                if (iso, "ALL") in holiday_keys or (iso, scope_code) in holiday_keys:
                    continue
                leave_day_rows.append({
                    "request_id": request["request_id"],
                    "emp_id": request["emp_id"],
                    "plant": request["plant"],
                    "leave_type": request["leave_type"],
                    "leave_date": iso,
                })
        leave_days = pd.DataFrame(
            leave_day_rows,
            columns=["request_id", "emp_id", "plant", "leave_type", "leave_date"],
        ).drop_duplicates(["request_id", "emp_id", "leave_date"])
        write_table(conn, "stg_leave_days", leave_days)

        # --------------------------------------------------------------
        # H. Overtime requests and payroll: typed numeric measures.
        # --------------------------------------------------------------
        ot = clean_text(read_raw(conn, "raw_overtime_requests"))
        ot["ot_date"] = iso_date(parse_dates(ot["ot_date"]))
        for col in ["requested_hours", "approved_hours"]:
            ot[col] = pd.to_numeric(ot[col], errors="coerce")
        ot["status"] = ot["status"].str.strip().str.title()
        write_table(conn, "stg_overtime_requests", ot)

        payroll = clean_text(read_raw(conn, "raw_payroll_lines"))
        payroll["payroll_month"] = payroll["payroll_month"].astype(str).str.strip()
        payroll["component"] = payroll["component"].str.strip().str.upper()
        payroll["component_type"] = payroll["component_type"].str.strip().str.upper()
        payroll["units"] = pd.to_numeric(payroll["units"], errors="coerce")
        payroll["amount"] = pd.to_numeric(payroll["amount"], errors="coerce")
        write_table(conn, "stg_payroll_lines", payroll)

        # --------------------------------------------------------------
        # I. Assign punches to shift dates (night shift punches start-date rule).
        # --------------------------------------------------------------
        clean_for_att = clean_punches.copy()
        clean_for_att["punch_time_dt"] = pd.to_datetime(clean_for_att["punch_time"], errors="coerce")
        clean_for_att["calendar_date"] = clean_for_att["punch_time_dt"].dt.normalize()

        roster_lookup = {}
        for row in roster.itertuples(index=False):
            if pd.notna(row.shift_code):
                roster_lookup[(row.emp_id, pd.Timestamp(row.week_start))] = row.shift_code

        def roster_shift(emp_id, work_date):
            work_date = pd.Timestamp(work_date).normalize()
            monday = work_date - pd.Timedelta(days=work_date.day_of_week)
            return roster_lookup.get((emp_id, monday), None)

        def assign_shift_date(row):
            ts = row.punch_time_dt
            if pd.isna(ts):
                return pd.NaT
            cur_date = ts.normalize()
            previous_date = cur_date - pd.Timedelta(days=1)
            previous_shift = roster_shift(row.emp_id, previous_date)
            current_shift = roster_shift(row.emp_id, cur_date)
            # Early-morning punch after a NIGHT shift belongs to previous start date.
            if ts.hour < 12 and previous_shift == "NIGHT":
                return previous_date
            # Late-evening punch on a NIGHT shift belongs to current start date.
            if ts.hour >= 18 and current_shift == "NIGHT":
                return cur_date
            return cur_date

        clean_for_att["shift_date"] = clean_for_att.apply(assign_shift_date, axis=1)
        clean_for_att["shift_date"] = pd.to_datetime(clean_for_att["shift_date"]).dt.normalize()
        punch_groups = clean_for_att.dropna(subset=["emp_id", "shift_date"]).groupby(
            ["emp_id", "shift_date"], as_index=False
        ).agg(
            punch_count=("punch_id", "count"),
            first_punch=("punch_time_dt", "min"),
            last_punch=("punch_time_dt", "max"),
        )

        # --------------------------------------------------------------
        # J. Attendance: one row per employed employee/day in the six-month period.
        # --------------------------------------------------------------
        emp_att = kept_employees[["emp_id", "plant", "plant_code", "hire_date", "separation_date"]].copy()
        emp_att["hire_date_dt"] = parse_dates(emp_att["hire_date"])
        emp_att["separation_date_dt"] = parse_dates(emp_att["separation_date"])
        emp_att = emp_att.loc[
            emp_att["hire_date_dt"].notna()
            & (emp_att["hire_date_dt"] <= PERIOD_END)
            & (emp_att["separation_date_dt"].isna() | (emp_att["separation_date_dt"] >= PERIOD_START))
        ].copy()

        calendar = pd.DataFrame({"work_date": pd.date_range(PERIOD_START, PERIOD_END, freq="D")})
        emp_att["_join_key"] = 1
        calendar["_join_key"] = 1
        daily = emp_att.merge(calendar, on="_join_key", how="inner").drop(columns=["_join_key"])
        daily = daily.loc[
            (daily["hire_date_dt"] <= daily["work_date"])
            & (daily["separation_date_dt"].isna() | (daily["work_date"] <= daily["separation_date_dt"]))
        ].copy()
        daily["week_start"] = daily["work_date"] - pd.to_timedelta(daily["work_date"].dt.day_of_week, unit="D")
        daily = daily.merge(
            roster[["emp_id", "week_start", "shift_code"]],
            on=["emp_id", "week_start"], how="left", suffixes=("", "_roster")
        )

        punch_groups = punch_groups.rename(columns={"shift_date": "work_date"})
        daily = daily.merge(punch_groups, on=["emp_id", "work_date"], how="left")
        daily["punch_count"] = daily["punch_count"].fillna(0).astype(int)

        leave_key_set = set(zip(leave_days.get("emp_id", []), leave_days.get("leave_date", [])))
        daily["work_date_iso"] = iso_date(daily["work_date"])
        daily["is_approved_leave"] = [
            (emp, day) in leave_key_set
            for emp, day in zip(daily["emp_id"], daily["work_date_iso"])
        ]
        daily["is_sunday"] = daily["work_date"].dt.day_of_week.eq(6)
        daily["plant_code"] = daily["plant_code"].fillna("")
        daily["is_holiday"] = [
            (day, "ALL") in holiday_keys or (day, code) in holiday_keys
            for day, code in zip(daily["work_date_iso"], daily["plant_code"])
        ]

        def get_status(row):
            count = int(row["punch_count"])
            if row["is_sunday"] and count == 0:
                return "Weekly Off"
            if row["is_holiday"] and count == 0:
                return "Holiday"
            if row["is_approved_leave"]:
                return "On Leave"
            if count >= 2:
                return "Present"
            if count == 1:
                return "Missing Punch"
            return "Absent"

        daily["attendance_status"] = daily.apply(get_status, axis=1)

        shift_lookup = shifts.set_index("shift_code").to_dict("index")
        shift_start_list = []
        shift_end_list = []
        for _, row in daily.iterrows():
            code = row.get("shift_code")
            info = shift_lookup.get(code) if code in shift_lookup else None
            if not info:
                shift_start_list.append(pd.NaT)
                shift_end_list.append(pd.NaT)
                continue
            start_min = int(info["start_minutes"])
            end_min = int(info["end_minutes"])
            start = row["work_date"] + pd.Timedelta(minutes=start_min)
            end = row["work_date"] + pd.Timedelta(minutes=end_min)
            crosses = str(info.get("crosses_midnight", "false")).strip().casefold() in {"true", "1", "yes"}
            if crosses or end <= start:
                end += pd.Timedelta(days=1)
            shift_start_list.append(start)
            shift_end_list.append(end)
        daily["shift_start"] = shift_start_list
        daily["shift_end"] = shift_end_list

        daily["check_in"] = daily["first_punch"]
        daily["check_out"] = daily.apply(
            lambda r: r["last_punch"] if r["punch_count"] >= 2 else pd.NaT, axis=1
        )
        daily["worked_hours"] = (
            (daily["check_out"] - daily["check_in"]).dt.total_seconds() / 3600
        ).where(daily["punch_count"] >= 2)

        late = (daily["check_in"] - daily["shift_start"]).dt.total_seconds() / 60
        early = (daily["shift_end"] - daily["check_out"]).dt.total_seconds() / 60
        eligible_working_present = (
            daily["attendance_status"].eq("Present")
            & ~daily["is_sunday"]
            & ~daily["is_holiday"]
        )
        daily["late_minutes"] = late.where(eligible_working_present & (late > 10), 0).clip(lower=0)
        daily["early_departure_minutes"] = early.where(eligible_working_present & (early > 10), 0).clip(lower=0)

        ot_minutes_workday = (
            (daily["check_out"] - daily["shift_end"]).dt.total_seconds() / 60
        ).clip(lower=0)
        ot_minutes_restday = (daily["worked_hours"] * 60).clip(lower=0)
        daily["overtime_hours"] = 0.0
        valid_present = daily["attendance_status"].eq("Present") & daily["check_out"].notna()
        rest_day = daily["is_sunday"] | daily["is_holiday"]
        daily.loc[valid_present & ~rest_day, "overtime_hours"] = (
            (ot_minutes_workday[valid_present & ~rest_day] // 30) * 0.5
        )
        daily.loc[valid_present & rest_day, "overtime_hours"] = (
            (ot_minutes_restday[valid_present & rest_day] // 30) * 0.5
        )

        # Store dates/timestamps as ISO text for portable SQLite tables.
        for col in ["work_date", "week_start"]:
            daily[col] = iso_date(daily[col])
        for col in ["hire_date_dt", "separation_date_dt"]:
            daily[col] = iso_date(daily[col])
        for col in ["shift_start", "shift_end", "check_in", "check_out", "first_punch", "last_punch"]:
            daily[col] = iso_datetime(daily[col])
        daily = daily.rename(columns={"work_date_iso": "work_date_key"})
        daily = daily.drop(columns=["hire_date", "separation_date", "is_approved_leave", "is_sunday", "is_holiday"])
        write_table(conn, "stg_attendance_daily", daily)

        # --------------------------------------------------------------
        # K. Basic stage counts and reconciliation evidence.
        # --------------------------------------------------------------
        recon = pd.DataFrame([
            {"entity": "biometric_punches", "raw_rows": len(punches),
             "clean_rows": len(clean_punches), "rejected_unmapped_rows": len(rejected),
             "deduplicated_rows": len(duplicate_punches),
             "accounted_for": len(clean_punches) + len(rejected) + len(duplicate_punches),
             "reconciles": len(punches) == len(clean_punches) + len(rejected) + len(duplicate_punches)},
            {"entity": "employees", "raw_rows": len(read_raw(conn, "raw_employees")),
             "clean_rows": len(kept_employees), "rejected_unmapped_rows": 0,
             "deduplicated_rows": len(dropped_employees),
             "accounted_for": len(kept_employees) + len(dropped_employees),
             "reconciles": len(read_raw(conn, "raw_employees")) == len(kept_employees) + len(dropped_employees)},
        ])
        write_table(conn, "stg_reconciliation", recon)
        recon.to_csv(DOCS_DIR / "staging_reconciliation.csv", index=False)

        summary = {
            "employees_raw": len(read_raw(conn, "raw_employees")),
            "employees_clean": len(kept_employees),
            "employee_duplicate_rows_logged": len(dropped_employees),
            "punches_raw": len(punches),
            "mapped_punches_clean": len(clean_punches),
            "unmapped_punches_rejected": len(rejected),
            "near_duplicate_punches_logged": len(duplicate_punches),
            "approved_leave_requests": int(leave_requests["is_approved"].sum()),
            "approved_leave_days": len(leave_days),
            "attendance_daily_rows": len(daily),
            "negative_worked_hours": int((pd.to_numeric(daily["worked_hours"], errors="coerce") < 0).sum()),
            "clock_correction_device": CLOCK_CORRECTION_DEVICE,
            "clock_correction_minutes": CLOCK_CORRECTION_MINUTES,
        }
        print("\n" + "=" * 68)
        print("STAGING + ATTENDANCE SUMMARY")
        print("=" * 68)
        for key, value in summary.items():
            print(f"{key}: {value}")
        print("\nCreated tables: stg_* (raw_* tables untouched)")
        print("Reports:", DOCS_DIR / "staging_reconciliation.csv")
        print("         ", DOCS_DIR / "clock_correction_evidence.csv")


if __name__ == "__main__":
    main()
