"""PeopleOps hackathon Stage 4-6: SQLite warehouse, 10 analytics queries, and DQ checks.

Run only after transform/stage2_clean_and_attendance.py completes successfully.
Raw/staging tables are not modified. Warehouse/analytics outputs are rebuilt.
"""
from __future__ import annotations

import calendar
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_DIR / "data" / "peopleops.sqlite"
DDL_DIR = PROJECT_DIR / "warehouse" / "ddl"
ANALYTICS_DIR = PROJECT_DIR / "analytics"
RESULTS_DIR = ANALYTICS_DIR / "results"
QUALITY_DIR = PROJECT_DIR / "quality"
PRESENTATION_DIR = PROJECT_DIR / "presentation"
DOCS_DIR = PROJECT_DIR / "docs"

PERIOD_START = "2026-04-01"
PERIOD_END = "2026-09-30"
FUTURE_DATE = "9999-12-31"
MONTHS = [f"2026-{month:02d}" for month in range(4, 10)]
VALID_ATTENDANCE = {"Present", "Absent", "On Leave", "Holiday", "Weekly Off", "Missing Punch"}
VALID_PAY_COMPONENTS = {
    "BASIC", "HOUSE_RENT", "MEDICAL", "CONVEYANCE", "OVERTIME",
    "INCOME_TAX", "EOBI", "ABSENCE_DEDUCTION", "LOAN_INSTALLMENT",
}


def s(value):
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    return text if text else None


def iso_date(value):
    text = s(value)
    if not text:
        return None

    # Keep ISO dates in YYYY-MM-DD format.
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        try:
            return datetime.strptime(
                text[:10], "%Y-%m-%d"
            ).date().isoformat()
        except ValueError:
            pass

    parsed = pd.to_datetime(
        text, errors="coerce", format="mixed", dayfirst=True
    )
    if pd.isna(parsed):
        return None

    return parsed.strftime("%Y-%m-%d")



def num(value):
    try:
        if value is None or pd.isna(value) or str(value).strip() == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def read_table(conn, name):
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    if not exists:
        raise RuntimeError(f"Required table '{name}' is missing. Run Stage 2 + Stage 3 first.")
    return pd.read_sql_query(f'SELECT * FROM "{name}"', conn)


def columns(conn, name):
    return {row[1] for row in conn.execute(f'PRAGMA table_info("{name}")')}


def ensure_required_tables(conn):
    required = [
        "stg_employees", "stg_employment_history", "stg_departments", "stg_shifts",
        "stg_shift_roster", "stg_roster_original", "stg_roster_duplicate_log", "stg_rejected_roster_rows",
        "stg_device_mapping", "stg_device_mapping_duplicate_log", "stg_rejected_device_mapping",
        "stg_applications", "stg_holidays", "stg_punches_clean", "stg_rejected_punches",
        "stg_duplicate_punches", "stg_leave_requests", "stg_leave_days",
        "stg_overtime_requests", "stg_payroll_lines", "stg_attendance_daily",
    ]
    missing = [name for name in required if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()]
    if missing:
        raise RuntimeError(
            "Stage 2/3 tables are missing: " + ", ".join(missing)
            + ". Run transform/stage2_clean_and_attendance.py first."
        )


def create_dimension_data(conn):
    employees = read_table(conn, "stg_employees")
    history = read_table(conn, "stg_employment_history")
    departments = read_table(conn, "stg_departments")
    shifts = read_table(conn, "stg_shifts")

    profiles = {}
    for row in employees.to_dict("records"):
        profiles[s(row.get("emp_id"))] = row

    department_rows = {}
    for row in departments.to_dict("records"):
        dept_id = s(row.get("dept_id"))
        if not dept_id:
            continue
        department_rows[dept_id] = {
            "dept_id": dept_id,
            "dept_name": s(row.get("dept_name")) or dept_id,
            "plant": s(row.get("plant")),
            "cost_center": s(row.get("cost_center")),
            "previous_name": s(row.get("previous_name")),
            "renamed_on": iso_date(row.get("renamed_on")),
        }

    dim_emp_rows = []
    history_ids = set()
    for row in history.to_dict("records"):
        emp_id = s(row.get("emp_id"))
        valid_from = iso_date(row.get("valid_from") or row.get("effective_from"))
        if not emp_id or not valid_from:
            continue
        profile = profiles.get(emp_id, {})
        history_ids.add(emp_id)
        dept_id = s(row.get("dept_id")) or s(profile.get("dept_id_from_profile"))
        if dept_id and dept_id not in department_rows:
            department_rows[dept_id] = {
                "dept_id": dept_id,
                "dept_name": f"Unmapped department ({dept_id})",
                "plant": s(profile.get("plant")),
                "cost_center": None,
                "previous_name": None,
                "renamed_on": None,
            }
        valid_to = iso_date(row.get("valid_to")) or FUTURE_DATE
        if valid_to < valid_from:
            # Keep data, but avoid creating an invalid SCD range silently.
            valid_to = valid_from
        dim_emp_rows.append({
            "emp_id": emp_id,
            "full_name": s(profile.get("full_name")),
            "gender": s(profile.get("gender")),
            "plant": s(profile.get("plant")),
            "plant_code": s(profile.get("plant_code")),
            "hire_date": iso_date(profile.get("hire_date")),
            "separation_date": iso_date(profile.get("separation_date")),
            "annual_leave_entitlement": num(profile.get("annual_leave_entitlement")),
            "employment_type": s(profile.get("employment_type")),
            "status": s(profile.get("status")),
            "dept_id": dept_id,
            "designation": s(row.get("designation")) or s(profile.get("designation")),
            "grade": s(row.get("grade")) or s(profile.get("grade")),
            "basic_salary": num(row.get("basic_salary")),
            "valid_from": valid_from,
            "valid_to": valid_to,
            "is_current": int(valid_to == FUTURE_DATE),
            "change_reason": s(row.get("change_reason")),
        })

    # Profile-only employees still receive an SCD row with clearly marked fallback provenance.
    for emp_id, profile in profiles.items():
        if not emp_id or emp_id in history_ids:
            continue
        dept_id = s(profile.get("dept_id_from_profile"))
        if dept_id and dept_id not in department_rows:
            department_rows[dept_id] = {
                "dept_id": dept_id,
                "dept_name": f"Unmapped department ({dept_id})",
                "plant": s(profile.get("plant")),
                "cost_center": None,
                "previous_name": None,
                "renamed_on": None,
            }
        dim_emp_rows.append({
            "emp_id": emp_id,
            "full_name": s(profile.get("full_name")),
            "gender": s(profile.get("gender")),
            "plant": s(profile.get("plant")),
            "plant_code": s(profile.get("plant_code")),
            "hire_date": iso_date(profile.get("hire_date")),
            "separation_date": iso_date(profile.get("separation_date")),
            "annual_leave_entitlement": num(profile.get("annual_leave_entitlement")),
            "employment_type": s(profile.get("employment_type")),
            "status": s(profile.get("status")),
            "dept_id": dept_id,
            "designation": s(profile.get("designation")),
            "grade": s(profile.get("grade")),
            "basic_salary": None,
            "valid_from": iso_date(profile.get("hire_date")) or "1900-01-01",
            "valid_to": FUTURE_DATE,
            "is_current": 1,
            "change_reason": "Profile fallback; no employment_history row",
        })

    # Ensure all fact records can reference a dimension row even when an input key is missing.
    if any(row["dept_id"] is None for row in dim_emp_rows):
        department_rows.setdefault("UNKNOWN", {
            "dept_id": "UNKNOWN", "dept_name": "Unknown department", "plant": None,
            "cost_center": None, "previous_name": None, "renamed_on": None,
        })
        for row in dim_emp_rows:
            if row["dept_id"] is None:
                row["dept_id"] = "UNKNOWN"

    dim_emp_rows.sort(key=lambda r: (r["emp_id"], r["valid_from"]))
    for idx, row in enumerate(dim_emp_rows, start=1):
        row["employee_sk"] = idx

    # Store shift definitions by shift_code from the provided shift JSON.
    dim_shift_rows = []
    for row in shifts.to_dict("records"):
        code = s(row.get("shift_code"))
        if not code:
            continue
        start_minutes = num(row.get("start_minutes"))
        end_minutes = num(row.get("end_minutes"))
        if start_minutes is None and s(row.get("start_time")):
            parts = str(row["start_time"]).split(":")
            start_minutes = int(parts[0]) * 60 + int(parts[1])
        if end_minutes is None and s(row.get("end_time")):
            parts = str(row["end_time"]).split(":")
            end_minutes = int(parts[0]) * 60 + int(parts[1])
        dim_shift_rows.append({
            "shift_code": code,
            "shift_name": s(row.get("shift_name")) or code,
            "start_time": s(row.get("start_time")),
            "end_time": s(row.get("end_time")),
            "crosses_midnight": s(row.get("crosses_midnight")),
            "grace_minutes": num(row.get("grace_minutes")),
            "start_minutes": start_minutes,
            "end_minutes": end_minutes,
        })

    return list(department_rows.values()), dim_emp_rows, dim_shift_rows


def create_dimensions(conn, dept_rows, emp_rows, shift_rows):
    for table in [
        "fact_payroll_line", "fact_overtime_approved", "fact_leave_day",
        "fact_attendance_daily", "employee_month_snapshot",
        "dim_employee", "dim_department", "dim_shift", "dim_date",
    ]:
        conn.execute(f'DROP TABLE IF EXISTS "{table}"')

    conn.executescript("""
    CREATE TABLE dim_department (
        dept_id TEXT PRIMARY KEY,
        dept_name TEXT NOT NULL,
        plant TEXT,
        cost_center TEXT,
        previous_name TEXT,
        renamed_on TEXT
    );
    CREATE TABLE dim_shift (
        shift_code TEXT PRIMARY KEY,
        shift_name TEXT NOT NULL,
        start_time TEXT,
        end_time TEXT,
        crosses_midnight TEXT,
        grace_minutes REAL,
        start_minutes INTEGER,
        end_minutes INTEGER
    );
    CREATE TABLE dim_date (
        date_key TEXT PRIMARY KEY,
        year INTEGER NOT NULL,
        month INTEGER NOT NULL,
        month_name TEXT NOT NULL,
        quarter INTEGER NOT NULL,
        day_of_month INTEGER NOT NULL,
        day_of_week INTEGER NOT NULL,
        day_name TEXT NOT NULL,
        is_weekend INTEGER NOT NULL,
        month_start TEXT NOT NULL,
        month_end TEXT NOT NULL
    );
    CREATE TABLE dim_employee (
        employee_sk INTEGER PRIMARY KEY,
        emp_id TEXT NOT NULL,
        full_name TEXT,
        gender TEXT,
        plant TEXT,
        plant_code TEXT,
        hire_date TEXT,
        separation_date TEXT,
        annual_leave_entitlement REAL,
        employment_type TEXT,
        status TEXT,
        dept_id TEXT NOT NULL,
        designation TEXT,
        grade TEXT,
        basic_salary REAL,
        valid_from TEXT NOT NULL,
        valid_to TEXT NOT NULL,
        is_current INTEGER NOT NULL,
        change_reason TEXT,
        FOREIGN KEY (dept_id) REFERENCES dim_department(dept_id),
        UNIQUE (emp_id, valid_from)
    );
    CREATE INDEX idx_dim_employee_natural_valid ON dim_employee(emp_id, valid_from, valid_to);
    CREATE INDEX idx_dim_employee_dept ON dim_employee(dept_id);
    """)

    conn.executemany(
        "INSERT INTO dim_department VALUES (?, ?, ?, ?, ?, ?)",
        [tuple(row[k] for k in ["dept_id", "dept_name", "plant", "cost_center", "previous_name", "renamed_on"])
         for row in sorted(dept_rows, key=lambda x: x["dept_id"])],
    )
    conn.executemany(
        """INSERT INTO dim_shift
        (shift_code, shift_name, start_time, end_time, crosses_midnight, grace_minutes, start_minutes, end_minutes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [tuple(row[k] for k in ["shift_code", "shift_name", "start_time", "end_time", "crosses_midnight", "grace_minutes", "start_minutes", "end_minutes"])
         for row in shift_rows],
    )
    emp_cols = [
        "employee_sk", "emp_id", "full_name", "gender", "plant", "plant_code", "hire_date",
        "separation_date", "annual_leave_entitlement", "employment_type", "status", "dept_id",
        "designation", "grade", "basic_salary", "valid_from", "valid_to", "is_current", "change_reason",
    ]
    conn.executemany(
        f"INSERT INTO dim_employee ({', '.join(emp_cols)}) VALUES ({', '.join('?' for _ in emp_cols)})",
        [tuple(row[k] for k in emp_cols) for row in emp_rows],
    )

    # Date dimension includes all of 2026 so month-end and after-separation checks resolve safely.
    date_rows = []
    cursor = date(2026, 1, 1)
    end = date(2026, 12, 31)
    while cursor <= end:
        month_start = cursor.replace(day=1)
        month_end = cursor.replace(day=calendar.monthrange(cursor.year, cursor.month)[1])
        date_rows.append((
            cursor.isoformat(), cursor.year, cursor.month, cursor.strftime("%B"),
            (cursor.month - 1) // 3 + 1, cursor.day, cursor.weekday(),
            cursor.strftime("%A"), int(cursor.weekday() >= 5),
            month_start.isoformat(), month_end.isoformat(),
        ))
        cursor += timedelta(days=1)
    conn.executemany("INSERT INTO dim_date VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", date_rows)
    conn.commit()


def lookup_version(version_map, emp_id, on_date):
    if not emp_id or not on_date:
        return None
    versions = version_map.get(emp_id, [])
    for version in reversed(versions):
        if version["valid_from"] <= on_date <= version["valid_to"]:
            return version
    # Source rule defines a record through the day before the next record. This fallback
    # is only used to make exceptions visible while keeping the SCD2 join reproducible.
    preceding = [v for v in versions if v["valid_from"] <= on_date]
    return preceding[-1] if preceding else None


def build_employee_month_snapshot(conn, emp_rows):
    profiles_df = read_table(conn, "stg_employees")
    profiles = {s(row.get("emp_id")): row for row in profiles_df.to_dict("records") if s(row.get("emp_id"))}
    version_map = {}
    for row in emp_rows:
        version_map.setdefault(row["emp_id"], []).append(row)
    for versions in version_map.values():
        versions.sort(key=lambda r: r["valid_from"])

    snapshot_rows = []
    for payroll_month in MONTHS:
        year, month = map(int, payroll_month.split("-"))
        month_start = date(year, month, 1).isoformat()
        month_end = date(year, month, calendar.monthrange(year, month)[1]).isoformat()
        for emp_id, profile in profiles.items():
            hire_date = iso_date(profile.get("hire_date"))
            separation_date = iso_date(profile.get("separation_date"))
            if not hire_date:
                continue
            active_start = hire_date <= month_start and (not separation_date or separation_date >= month_start)
            active_end = hire_date <= month_end and (not separation_date or separation_date > month_end)
            separated_in_month = bool(separation_date and month_start <= separation_date <= month_end)
            attribution_date = separation_date if separated_in_month else month_end
            version_start = lookup_version(version_map, emp_id, month_start)
            version_end = lookup_version(version_map, emp_id, month_end)
            version_attr = lookup_version(version_map, emp_id, attribution_date)
            dept_start = (version_start or version_end or version_attr or {}).get("dept_id")
            dept_end = (version_end or version_attr or version_start or {}).get("dept_id")
            dept_attr = (version_attr or version_end or version_start or {}).get("dept_id")
            employee_sk_attr = (version_attr or version_end or version_start or {}).get("employee_sk")
            plant = s(profile.get("plant"))
            if dept_attr:
                found = conn.execute("SELECT plant FROM dim_department WHERE dept_id=?", (dept_attr,)).fetchone()
                if found and found[0]:
                    plant = found[0]
            snapshot_rows.append((
                emp_id, payroll_month, month_start, month_end, dept_start, dept_end, dept_attr,
                plant, hire_date, separation_date, num(profile.get("annual_leave_entitlement")),
                int(active_start), int(active_end), int(separated_in_month), employee_sk_attr,
            ))

    conn.execute("""CREATE TABLE employee_month_snapshot (
        emp_id TEXT NOT NULL,
        payroll_month TEXT NOT NULL,
        month_start TEXT NOT NULL,
        month_end TEXT NOT NULL,
        dept_id_month_start TEXT,
        dept_id_month_end TEXT,
        dept_id_attribution TEXT,
        plant TEXT,
        hire_date TEXT,
        separation_date TEXT,
        annual_leave_entitlement REAL,
        active_at_month_start INTEGER NOT NULL,
        active_at_month_end INTEGER NOT NULL,
        separated_in_month INTEGER NOT NULL,
        employee_sk_attribution INTEGER,
        PRIMARY KEY (emp_id, payroll_month),
        FOREIGN KEY (dept_id_month_start) REFERENCES dim_department(dept_id),
        FOREIGN KEY (dept_id_month_end) REFERENCES dim_department(dept_id),
        FOREIGN KEY (dept_id_attribution) REFERENCES dim_department(dept_id),
        FOREIGN KEY (employee_sk_attribution) REFERENCES dim_employee(employee_sk)
    )""")
    conn.executemany("INSERT INTO employee_month_snapshot VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", snapshot_rows)
    conn.execute("CREATE INDEX idx_ems_month_dept ON employee_month_snapshot(payroll_month, dept_id_attribution)")
    conn.commit()


def create_fact_tables_and_load(conn):
    conn.executescript("""
    CREATE TABLE fact_attendance_daily (
        attendance_sk INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_id TEXT NOT NULL,
        employee_sk INTEGER NOT NULL,
        dept_id TEXT,
        shift_code TEXT,
        date_key TEXT NOT NULL,
        attendance_status TEXT NOT NULL,
        punch_count INTEGER NOT NULL,
        check_in TEXT,
        check_out TEXT,
        worked_hours REAL,
        late_minutes REAL,
        early_departure_minutes REAL,
        overtime_hours REAL,
        UNIQUE (emp_id, date_key),
        FOREIGN KEY (employee_sk) REFERENCES dim_employee(employee_sk),
        FOREIGN KEY (dept_id) REFERENCES dim_department(dept_id),
        FOREIGN KEY (shift_code) REFERENCES dim_shift(shift_code),
        FOREIGN KEY (date_key) REFERENCES dim_date(date_key)
    );
    CREATE INDEX idx_fact_attendance_date ON fact_attendance_daily(date_key, dept_id);
    CREATE INDEX idx_fact_attendance_emp_date ON fact_attendance_daily(emp_id, date_key);

    CREATE TABLE fact_leave_day (
        leave_day_sk INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id TEXT,
        emp_id TEXT NOT NULL,
        employee_sk INTEGER NOT NULL,
        dept_id TEXT,
        date_key TEXT NOT NULL,
        leave_type TEXT,
        FOREIGN KEY (employee_sk) REFERENCES dim_employee(employee_sk),
        FOREIGN KEY (dept_id) REFERENCES dim_department(dept_id),
        FOREIGN KEY (date_key) REFERENCES dim_date(date_key),
        UNIQUE (request_id, emp_id, date_key)
    );
    CREATE INDEX idx_fact_leave_date ON fact_leave_day(date_key, leave_type);

    CREATE TABLE fact_overtime_approved (
        request_id TEXT PRIMARY KEY,
        emp_id TEXT NOT NULL,
        employee_sk INTEGER NOT NULL,
        dept_id TEXT,
        date_key TEXT NOT NULL,
        requested_hours REAL,
        approved_hours REAL,
        status TEXT NOT NULL,
        FOREIGN KEY (employee_sk) REFERENCES dim_employee(employee_sk),
        FOREIGN KEY (dept_id) REFERENCES dim_department(dept_id),
        FOREIGN KEY (date_key) REFERENCES dim_date(date_key)
    );
    CREATE INDEX idx_fact_ot_date ON fact_overtime_approved(date_key, dept_id);

    CREATE TABLE fact_payroll_line (
        line_id TEXT PRIMARY KEY,
        emp_id TEXT NOT NULL,
        employee_sk INTEGER NOT NULL,
        dept_id TEXT,
        date_key TEXT NOT NULL,
        payroll_month TEXT NOT NULL,
        component TEXT NOT NULL,
        component_type TEXT NOT NULL,
        units REAL,
        amount REAL,
        attribution_date TEXT NOT NULL,
        FOREIGN KEY (employee_sk) REFERENCES dim_employee(employee_sk),
        FOREIGN KEY (dept_id) REFERENCES dim_department(dept_id),
        FOREIGN KEY (date_key) REFERENCES dim_date(date_key)
    );
    CREATE INDEX idx_fact_payroll_month ON fact_payroll_line(payroll_month, dept_id, component_type);
    """)

    # Fail early instead of silently dropping unmatched warehouse keys.
    missing_att_emp = conn.execute("""
        SELECT COUNT(*) FROM stg_attendance_daily a
        WHERE NOT EXISTS (
          SELECT 1 FROM dim_employee e
          WHERE e.emp_id=a.emp_id AND e.valid_from <= COALESCE(NULLIF(a.work_date_key,''), a.work_date)
          ORDER BY e.valid_from DESC LIMIT 1
        )
    """).fetchone()[0]
    if missing_att_emp:
        raise RuntimeError(f"{missing_att_emp} attendance rows have no SCD employee version. Review history dates.")

    attendance_cols = columns(conn, "stg_attendance_daily")
    date_expr = "COALESCE(NULLIF(a.work_date_key,''), a.work_date)" if "work_date_key" in attendance_cols else "a.work_date"
    conn.execute(f"""
      INSERT INTO fact_attendance_daily
        (emp_id, employee_sk, dept_id, shift_code, date_key, attendance_status, punch_count,
         check_in, check_out, worked_hours, late_minutes, early_departure_minutes, overtime_hours)
      SELECT a.emp_id,
        (SELECT e.employee_sk FROM dim_employee e WHERE e.emp_id=a.emp_id AND e.valid_from <= {date_expr} ORDER BY e.valid_from DESC LIMIT 1),
        (SELECT e.dept_id FROM dim_employee e WHERE e.emp_id=a.emp_id AND e.valid_from <= {date_expr} ORDER BY e.valid_from DESC LIMIT 1),
        NULLIF(a.shift_code, ''), {date_expr}, a.attendance_status, CAST(COALESCE(a.punch_count,0) AS INTEGER),
        a.check_in, a.check_out, CAST(a.worked_hours AS REAL), CAST(a.late_minutes AS REAL),
        CAST(a.early_departure_minutes AS REAL), CAST(a.overtime_hours AS REAL)
      FROM stg_attendance_daily a
    """)

    # Leave fact: valid day rows are already expanded and approved by Stage 2.
    conn.execute("""
      INSERT INTO fact_leave_day (request_id, emp_id, employee_sk, dept_id, date_key, leave_type)
      SELECT l.request_id, l.emp_id,
        (SELECT e.employee_sk FROM dim_employee e WHERE e.emp_id=l.emp_id AND e.valid_from <= l.leave_date ORDER BY e.valid_from DESC LIMIT 1),
        (SELECT e.dept_id FROM dim_employee e WHERE e.emp_id=l.emp_id AND e.valid_from <= l.leave_date ORDER BY e.valid_from DESC LIMIT 1),
        l.leave_date, l.leave_type
      FROM stg_leave_days l
      WHERE EXISTS (SELECT 1 FROM dim_employee e WHERE e.emp_id=l.emp_id AND e.valid_from <= l.leave_date)
    """)

    # Approved OT requests only. The source rule says approved_hours is the amount approved.
    conn.execute("""
      INSERT INTO fact_overtime_approved
        (request_id, emp_id, employee_sk, dept_id, date_key, requested_hours, approved_hours, status)
      SELECT o.request_id, o.emp_id,
        (SELECT e.employee_sk FROM dim_employee e WHERE e.emp_id=o.emp_id AND e.valid_from <= o.ot_date ORDER BY e.valid_from DESC LIMIT 1),
        (SELECT e.dept_id FROM dim_employee e WHERE e.emp_id=o.emp_id AND e.valid_from <= o.ot_date ORDER BY e.valid_from DESC LIMIT 1),
        o.ot_date, CAST(o.requested_hours AS REAL), CAST(o.approved_hours AS REAL), o.status
      FROM stg_overtime_requests o
      WHERE LOWER(TRIM(o.status))='approved'
        AND EXISTS (SELECT 1 FROM dim_employee e WHERE e.emp_id=o.emp_id AND e.valid_from <= o.ot_date)
    """)

    # Monthly payroll attribution: department on month end, or last working day if separated in that month.
    conn.execute("""
      INSERT INTO fact_payroll_line
        (line_id, emp_id, employee_sk, dept_id, date_key, payroll_month, component,
         component_type, units, amount, attribution_date)
      SELECT p.line_id, p.emp_id,
        (SELECT e.employee_sk FROM dim_employee e WHERE e.emp_id=p.emp_id AND e.valid_from <= (
          CASE WHEN NULLIF(ep.separation_date,'') IS NOT NULL
                    AND ep.separation_date >= (p.payroll_month || '-01')
                    AND ep.separation_date <= date(p.payroll_month || '-01','+1 month','-1 day')
               THEN ep.separation_date ELSE date(p.payroll_month || '-01','+1 month','-1 day') END
        ) ORDER BY e.valid_from DESC LIMIT 1),
        (SELECT e.dept_id FROM dim_employee e WHERE e.emp_id=p.emp_id AND e.valid_from <= (
          CASE WHEN NULLIF(ep.separation_date,'') IS NOT NULL
                    AND ep.separation_date >= (p.payroll_month || '-01')
                    AND ep.separation_date <= date(p.payroll_month || '-01','+1 month','-1 day')
               THEN ep.separation_date ELSE date(p.payroll_month || '-01','+1 month','-1 day') END
        ) ORDER BY e.valid_from DESC LIMIT 1),
        (CASE WHEN NULLIF(ep.separation_date,'') IS NOT NULL
                    AND ep.separation_date >= (p.payroll_month || '-01')
                    AND ep.separation_date <= date(p.payroll_month || '-01','+1 month','-1 day')
               THEN ep.separation_date ELSE date(p.payroll_month || '-01','+1 month','-1 day') END),
        p.payroll_month, UPPER(TRIM(p.component)), UPPER(TRIM(p.component_type)),
        CAST(p.units AS REAL), CAST(p.amount AS REAL),
        (CASE WHEN NULLIF(ep.separation_date,'') IS NOT NULL
                    AND ep.separation_date >= (p.payroll_month || '-01')
                    AND ep.separation_date <= date(p.payroll_month || '-01','+1 month','-1 day')
               THEN ep.separation_date ELSE date(p.payroll_month || '-01','+1 month','-1 day') END)
      FROM stg_payroll_lines p
      LEFT JOIN stg_employees ep ON ep.emp_id=p.emp_id
      WHERE EXISTS (
        SELECT 1 FROM dim_employee e WHERE e.emp_id=p.emp_id AND e.valid_from <= (
          CASE WHEN NULLIF(ep.separation_date,'') IS NOT NULL
                    AND ep.separation_date >= (p.payroll_month || '-01')
                    AND ep.separation_date <= date(p.payroll_month || '-01','+1 month','-1 day')
               THEN ep.separation_date ELSE date(p.payroll_month || '-01','+1 month','-1 day') END
        )
      )
    """)
    conn.commit()


def write_analytics_sql():
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ANALYTICS_DIR.mkdir(parents=True, exist_ok=True)
    month_grid = "SELECT payroll_month FROM employee_month_snapshot GROUP BY payroll_month"
    historical_dept_name = "CASE WHEN d.renamed_on IS NOT NULL AND m.payroll_month < substr(d.renamed_on,1,7) AND NULLIF(TRIM(d.previous_name),'') IS NOT NULL THEN d.previous_name ELSE d.dept_name END"
    month_dept_grid = "SELECT m.payroll_month, d.dept_id, " + historical_dept_name + " AS dept_name, d.plant FROM (" + month_grid + ") m CROSS JOIN dim_department d"

    q01 = f"""-- Q01: Month-end headcount by plant and department (April-September 2026).
WITH month_depts AS ({month_dept_grid}),
headcount AS (
  SELECT payroll_month, dept_id_month_end AS dept_id, COUNT(DISTINCT emp_id) AS month_end_headcount
  FROM employee_month_snapshot WHERE active_at_month_end=1
  GROUP BY payroll_month, dept_id_month_end
)
SELECT md.payroll_month, md.plant, md.dept_id, md.dept_name,
       COALESCE(h.month_end_headcount,0) AS month_end_headcount
FROM month_depts md LEFT JOIN headcount h
 ON h.payroll_month=md.payroll_month AND h.dept_id=md.dept_id
ORDER BY md.payroll_month, md.plant, md.dept_id;"""

    q02 = f"""-- Q02: Monthly attrition = separations / average opening and closing headcount.
WITH month_depts AS ({month_dept_grid}),
opening AS (
 SELECT payroll_month, dept_id_month_start AS dept_id, COUNT(DISTINCT emp_id) AS opening_headcount
 FROM employee_month_snapshot WHERE active_at_month_start=1 GROUP BY payroll_month, dept_id_month_start
),
closing AS (
 SELECT payroll_month, dept_id_month_end AS dept_id, COUNT(DISTINCT emp_id) AS closing_headcount
 FROM employee_month_snapshot WHERE active_at_month_end=1 GROUP BY payroll_month, dept_id_month_end
),
separations AS (
 SELECT payroll_month, dept_id_attribution AS dept_id, COUNT(DISTINCT emp_id) AS separations
 FROM employee_month_snapshot WHERE separated_in_month=1 GROUP BY payroll_month, dept_id_attribution
)
SELECT md.payroll_month, md.plant, md.dept_id, md.dept_name,
       COALESCE(o.opening_headcount,0) AS opening_headcount,
       COALESCE(c.closing_headcount,0) AS closing_headcount,
       COALESCE(s.separations,0) AS separations,
       (COALESCE(o.opening_headcount,0)+COALESCE(c.closing_headcount,0))/2.0 AS average_headcount,
       CASE WHEN COALESCE(o.opening_headcount,0)+COALESCE(c.closing_headcount,0)>0
            THEN 200.0*COALESCE(s.separations,0)/(COALESCE(o.opening_headcount,0)+COALESCE(c.closing_headcount,0))
            ELSE NULL END AS attrition_rate_pct
FROM month_depts md
LEFT JOIN opening o ON o.payroll_month=md.payroll_month AND o.dept_id=md.dept_id
LEFT JOIN closing c ON c.payroll_month=md.payroll_month AND c.dept_id=md.dept_id
LEFT JOIN separations s ON s.payroll_month=md.payroll_month AND s.dept_id=md.dept_id
ORDER BY md.payroll_month, md.plant, md.dept_id;"""

    q03 = """-- Q03: Absenteeism = Absent / (Present + Absent + Missing Punch); approved leave, holidays and weekly offs excluded.
WITH attendance AS (
 SELECT substr(f.date_key,1,7) AS payroll_month, m.plant,
        SUM(CASE WHEN f.attendance_status='Absent' THEN 1 ELSE 0 END) AS absent_days,
        SUM(CASE WHEN f.attendance_status IN ('Present','Absent','Missing Punch') THEN 1 ELSE 0 END) AS scheduled_eligible_days
 FROM fact_attendance_daily f
 JOIN employee_month_snapshot m ON m.emp_id=f.emp_id AND m.payroll_month=substr(f.date_key,1,7)
 WHERE substr(f.date_key,1,7) BETWEEN '2026-04' AND '2026-09'
 GROUP BY substr(f.date_key,1,7), m.plant
), plants AS (SELECT DISTINCT plant FROM dim_department WHERE plant IS NOT NULL),
months AS (SELECT payroll_month FROM employee_month_snapshot GROUP BY payroll_month)
SELECT mo.payroll_month, p.plant, COALESCE(a.absent_days,0) AS absent_days,
       COALESCE(a.scheduled_eligible_days,0) AS scheduled_eligible_days,
       CASE WHEN COALESCE(a.scheduled_eligible_days,0)>0
            THEN 100.0*a.absent_days/a.scheduled_eligible_days ELSE NULL END AS absenteeism_rate_pct
FROM months mo CROSS JOIN plants p
LEFT JOIN attendance a ON a.payroll_month=mo.payroll_month AND a.plant=p.plant
ORDER BY mo.payroll_month,p.plant;"""

    q04 = """-- Q04: Top 10 employees with the most late arrivals and their latest known department.
WITH late AS (
 SELECT emp_id, COUNT(*) AS late_arrival_days, SUM(COALESCE(late_minutes,0)) AS total_late_minutes,
        AVG(CASE WHEN late_minutes>0 THEN late_minutes END) AS avg_late_minutes
 FROM fact_attendance_daily
 WHERE late_minutes>0 AND attendance_status='Present'
 GROUP BY emp_id
), current_version AS (
 SELECT *, ROW_NUMBER() OVER (PARTITION BY emp_id ORDER BY valid_from DESC) AS rn
 FROM dim_employee
)
SELECT l.emp_id, e.full_name, e.dept_id, d.dept_name, l.late_arrival_days,
       ROUND(l.total_late_minutes,1) AS total_late_minutes,
       ROUND(l.avg_late_minutes,1) AS avg_late_minutes
FROM late l LEFT JOIN current_version e ON e.emp_id=l.emp_id AND e.rn=1
LEFT JOIN dim_department d ON d.dept_id=e.dept_id
ORDER BY l.late_arrival_days DESC, l.total_late_minutes DESC, l.emp_id
LIMIT 10;"""

    q05_ctes = """WITH computed AS (
 SELECT m.payroll_month, m.dept_id_attribution AS dept_id, SUM(COALESCE(a.overtime_hours,0)) AS computed_ot_hours
 FROM fact_attendance_daily a JOIN employee_month_snapshot m
   ON m.emp_id=a.emp_id AND m.payroll_month=substr(a.date_key,1,7)
 GROUP BY m.payroll_month,m.dept_id_attribution
), approved AS (
 SELECT m.payroll_month, m.dept_id_attribution AS dept_id, SUM(COALESCE(o.approved_hours,0)) AS approved_ot_hours
 FROM fact_overtime_approved o JOIN employee_month_snapshot m
   ON m.emp_id=o.emp_id AND m.payroll_month=substr(o.date_key,1,7)
 GROUP BY m.payroll_month,m.dept_id_attribution
), paid AS (
 SELECT payroll_month, dept_id, SUM(COALESCE(units,0)) AS paid_ot_hours,
        SUM(COALESCE(amount,0)) AS paid_ot_amount
 FROM fact_payroll_line
 WHERE component='OVERTIME' AND component_type='EARNING'
 GROUP BY payroll_month,dept_id
), month_depts AS (
 SELECT m.payroll_month,d.dept_id,
        CASE WHEN d.renamed_on IS NOT NULL
                  AND m.payroll_month < substr(d.renamed_on,1,7)
                  AND NULLIF(TRIM(d.previous_name),'') IS NOT NULL
             THEN d.previous_name ELSE d.dept_name END AS dept_name,
        d.plant
 FROM (SELECT payroll_month FROM employee_month_snapshot GROUP BY payroll_month) m
 CROSS JOIN dim_department d
)
"""
    q05 = """-- Q05: Punch-computed vs approved vs paid overtime, grouped by month-end/last-working-day department.
""" + q05_ctes + """SELECT md.payroll_month,md.plant,md.dept_id,md.dept_name,
 COALESCE(c.computed_ot_hours,0) AS computed_ot_hours,
 COALESCE(a.approved_ot_hours,0) AS approved_ot_hours,
 COALESCE(p.paid_ot_hours,0) AS paid_ot_hours,
 COALESCE(p.paid_ot_amount,0) AS paid_ot_amount
FROM month_depts md
LEFT JOIN computed c ON c.payroll_month=md.payroll_month AND c.dept_id=md.dept_id
LEFT JOIN approved a ON a.payroll_month=md.payroll_month AND a.dept_id=md.dept_id
LEFT JOIN paid p ON p.payroll_month=md.payroll_month AND p.dept_id=md.dept_id
ORDER BY md.payroll_month,md.plant,md.dept_id;"""

    q06 = """-- Q06: Departments/months where paid overtime exceeds computed overtime by >15%.
""" + q05_ctes + """SELECT md.payroll_month,md.plant,md.dept_id,md.dept_name,
 COALESCE(c.computed_ot_hours,0) AS computed_ot_hours,
 COALESCE(p.paid_ot_hours,0) AS paid_ot_hours,
 COALESCE(p.paid_ot_hours,0)-COALESCE(c.computed_ot_hours,0) AS excess_ot_hours,
 CASE WHEN COALESCE(c.computed_ot_hours,0)>0
      THEN ROUND(100.0*(COALESCE(p.paid_ot_hours,0)-c.computed_ot_hours)/c.computed_ot_hours,2)
      ELSE NULL END AS pct_above_computed
FROM month_depts md
LEFT JOIN computed c ON c.payroll_month=md.payroll_month AND c.dept_id=md.dept_id
LEFT JOIN paid p ON p.payroll_month=md.payroll_month AND p.dept_id=md.dept_id
WHERE COALESCE(p.paid_ot_hours,0)>COALESCE(c.computed_ot_hours,0)*1.15
ORDER BY excess_ot_hours DESC,md.payroll_month,md.dept_id;"""

    q07 = """-- Q07: Positive payroll earnings but zero Present days. 'Zero attendance' here means zero Present days.
WITH payroll AS (
 SELECT payroll_month,emp_id,SUM(amount) AS total_earnings
 FROM fact_payroll_line WHERE component_type='EARNING'
 GROUP BY payroll_month,emp_id HAVING SUM(COALESCE(amount,0))>0
), attendance AS (
 SELECT substr(date_key,1,7) AS payroll_month,emp_id,
        SUM(CASE WHEN attendance_status='Present' THEN 1 ELSE 0 END) AS present_days,
        SUM(CASE WHEN attendance_status='Missing Punch' THEN 1 ELSE 0 END) AS missing_punch_days,
        SUM(CASE WHEN attendance_status='Absent' THEN 1 ELSE 0 END) AS absent_days
 FROM fact_attendance_daily GROUP BY substr(date_key,1,7),emp_id
)
SELECT p.payroll_month,p.emp_id,m.plant,m.dept_id_attribution AS dept_id,d.dept_name,
       ROUND(p.total_earnings,2) AS total_earnings,
       COALESCE(a.present_days,0) AS present_days,
       COALESCE(a.absent_days,0) AS absent_days,
       COALESCE(a.missing_punch_days,0) AS missing_punch_days
FROM payroll p JOIN employee_month_snapshot m ON m.emp_id=p.emp_id AND m.payroll_month=p.payroll_month
LEFT JOIN attendance a ON a.emp_id=p.emp_id AND a.payroll_month=p.payroll_month
LEFT JOIN dim_department d ON d.dept_id=m.dept_id_attribution
WHERE COALESCE(a.present_days,0)=0
ORDER BY p.payroll_month,p.total_earnings DESC,p.emp_id;"""

    q08 = """-- Q08: Payroll in a month after separation month. Exact pay date is not provided in the source.
WITH payroll AS (
 SELECT payroll_month,emp_id,SUM(amount) AS total_earnings
 FROM fact_payroll_line WHERE component_type='EARNING'
 GROUP BY payroll_month,emp_id HAVING SUM(COALESCE(amount,0))>0
)
SELECT p.emp_id,p.payroll_month,m.separation_date,m.plant,m.dept_id_attribution AS department_id,
       d.dept_name,ROUND(p.total_earnings,2) AS total_earnings
FROM payroll p JOIN employee_month_snapshot m ON m.emp_id=p.emp_id AND m.payroll_month=p.payroll_month
LEFT JOIN dim_department d ON d.dept_id=m.dept_id_attribution
WHERE m.separation_date IS NOT NULL
  AND p.payroll_month > substr(m.separation_date,1,7)
ORDER BY p.payroll_month,p.emp_id;"""

    q09 = """-- Q09: Monthly payroll cost = EARNING lines; per employee = cost / distinct paid employees.
WITH month_depts AS (
 SELECT m.payroll_month,d.dept_id,
        CASE WHEN d.renamed_on IS NOT NULL
                  AND m.payroll_month < substr(d.renamed_on,1,7)
                  AND NULLIF(TRIM(d.previous_name),'') IS NOT NULL
             THEN d.previous_name ELSE d.dept_name END AS dept_name,
        d.plant
 FROM (SELECT payroll_month FROM employee_month_snapshot GROUP BY payroll_month) m
 CROSS JOIN dim_department d
), payroll AS (
 SELECT payroll_month,dept_id,SUM(CASE WHEN component_type='EARNING' THEN COALESCE(amount,0) ELSE 0 END) AS payroll_cost,
        COUNT(DISTINCT CASE WHEN component_type='EARNING' AND COALESCE(amount,0)<>0 THEN emp_id END) AS paid_employee_count
 FROM fact_payroll_line GROUP BY payroll_month,dept_id
), joined AS (
 SELECT md.payroll_month,md.plant,md.dept_id,md.dept_name,
        COALESCE(p.payroll_cost,0) AS payroll_cost,COALESCE(p.paid_employee_count,0) AS paid_employee_count,
        CASE WHEN COALESCE(p.paid_employee_count,0)>0 THEN COALESCE(p.payroll_cost,0)*1.0/p.paid_employee_count ELSE NULL END AS payroll_cost_per_paid_employee
 FROM month_depts md LEFT JOIN payroll p ON p.payroll_month=md.payroll_month AND p.dept_id=md.dept_id
), lagged AS (
 SELECT *,LAG(payroll_cost) OVER (PARTITION BY dept_id ORDER BY payroll_month) AS previous_month_cost
 FROM joined
)
SELECT payroll_month,plant,dept_id,dept_name,ROUND(payroll_cost,2) AS payroll_cost,
       paid_employee_count,ROUND(payroll_cost_per_paid_employee,2) AS payroll_cost_per_paid_employee,
       ROUND(previous_month_cost,2) AS previous_month_cost,
       CASE WHEN previous_month_cost>0 THEN ROUND(100.0*(payroll_cost-previous_month_cost)/previous_month_cost,2) ELSE NULL END AS month_over_month_change_pct
FROM lagged ORDER BY payroll_month,plant,dept_id;"""

    q10 = """-- Q10: Employees whose approved annual leave days in the six-month period exceed full-year entitlement.
WITH annual_leave AS (
 SELECT emp_id,COUNT(DISTINCT date_key) AS approved_annual_leave_days
 FROM fact_leave_day
 WHERE date_key BETWEEN '2026-04-01' AND '2026-09-30'
   AND LOWER(COALESCE(leave_type,'')) LIKE '%annual%'
 GROUP BY emp_id
), current_employee AS (
 SELECT *,ROW_NUMBER() OVER (PARTITION BY emp_id ORDER BY valid_from DESC) AS rn
 FROM dim_employee
)
SELECT l.emp_id,e.full_name,e.plant,e.dept_id,d.dept_name,
       l.approved_annual_leave_days,e.annual_leave_entitlement,
       l.approved_annual_leave_days-e.annual_leave_entitlement AS days_over_entitlement
FROM annual_leave l JOIN current_employee e ON e.emp_id=l.emp_id AND e.rn=1
LEFT JOIN dim_department d ON d.dept_id=e.dept_id
WHERE e.annual_leave_entitlement IS NOT NULL
  AND l.approved_annual_leave_days>e.annual_leave_entitlement
ORDER BY days_over_entitlement DESC,l.emp_id;"""

    queries = {
        "q01_headcount.sql": q01,
        "q02_attrition.sql": q02,
        "q03_absenteeism.sql": q03,
        "q04_late_arrivals.sql": q04,
        "q05_overtime_reconciliation.sql": q05,
        "q06_ot_overpayment.sql": q06,
        "q07_paid_zero_attendance.sql": q07,
        "q08_pay_after_separation.sql": q08,
        "q09_payroll_cost.sql": q09,
        "q10_leave_overuse.sql": q10,
    }
    for filename, sql in queries.items():
        (ANALYTICS_DIR / filename).write_text(sql.strip() + "\n", encoding="utf-8")
    return queries


def run_dq_checks(conn):
    checks = []
    def add(name, table_name, passed, expected, actual, details, status_override=None):
        checks.append({
            "check_name": name, "table_name": table_name,
            "status": status_override or ("PASS" if passed else "FAIL"),
            "expected": str(expected), "actual": str(actual), "details": str(details),
            "checked_at": datetime.now().isoformat(timespec="seconds"),
        })

    # Uniqueness/not-null for primary and natural keys across core warehouse dimensions/facts.
    pk_specs = {
        "dim_department": ["dept_id"],
        "dim_shift": ["shift_code"],
        "dim_date": ["date_key"],
        "dim_employee": ["employee_sk"],
        "fact_attendance_daily": ["attendance_sk"],
        "fact_leave_day": ["leave_day_sk"],
        "fact_overtime_approved": ["request_id"],
        "fact_payroll_line": ["line_id"],
    }
    for table, keys in pk_specs.items():
        for key in keys:
            nulls = conn.execute(f'SELECT COUNT(*) FROM "{table}" WHERE "{key}" IS NULL').fetchone()[0]
            dups = conn.execute(f'SELECT COUNT(*) FROM (SELECT "{key}" FROM "{table}" GROUP BY "{key}" HAVING COUNT(*)>1)').fetchone()[0]
            add(f"PK not-null: {table}.{key}", table, nulls == 0, 0, nulls, "Primary key must not be null")
            add(f"PK unique: {table}.{key}", table, dups == 0, 0, dups, "Primary key must be unique")

    fk_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
    add("Foreign key integrity", "warehouse", len(fk_rows) == 0, 0, len(fk_rows), "Rows returned by PRAGMA foreign_key_check")

    # Business sanity checks.
    negative_hours = conn.execute("SELECT COUNT(*) FROM fact_attendance_daily WHERE worked_hours < 0").fetchone()[0]
    add("No negative worked hours", "fact_attendance_daily", negative_hours == 0, 0, negative_hours, "Worked hours below zero")
    over16 = conn.execute("""
      SELECT COUNT(*) FROM fact_attendance_daily
      WHERE check_in IS NOT NULL AND check_out IS NOT NULL
        AND (julianday(check_out)-julianday(check_in))*24 > 16
    """).fetchone()[0]
    add("No shift longer than 16 hours", "fact_attendance_daily", over16 == 0, 0, over16, "Check-in/check-out span > 16 hours")
    invalid_status = conn.execute("SELECT COUNT(*) FROM fact_attendance_daily WHERE attendance_status NOT IN ('Present','Absent','On Leave','Holiday','Weekly Off','Missing Punch')").fetchone()[0]
    add("Accepted attendance statuses", "fact_attendance_daily", invalid_status == 0, 0, invalid_status, "Allowed status values checked")
    invalid_components = conn.execute("SELECT COUNT(*) FROM fact_payroll_line WHERE component NOT IN (" + ",".join("?" for _ in VALID_PAY_COMPONENTS) + ")", tuple(sorted(VALID_PAY_COMPONENTS))).fetchone()[0]
    add("Accepted payroll components", "fact_payroll_line", invalid_components == 0, 0, invalid_components, "Source dictionary pay component list")
    blank_leave_type = conn.execute("SELECT COUNT(*) FROM fact_leave_day WHERE leave_type IS NULL OR TRIM(leave_type)='' ").fetchone()[0]
    leave_values = [r[0] for r in conn.execute("SELECT DISTINCT leave_type FROM fact_leave_day ORDER BY leave_type").fetchall()]
    add("Leave type populated", "fact_leave_day", blank_leave_type == 0, 0, blank_leave_type,
        "Nonblank leave types observed: " + ", ".join(str(v) for v in leave_values))
    allowed_leave_types = {"ANNUAL", "CASUAL", "SICK"}
    invalid_leave_types = conn.execute(
        "SELECT COUNT(*) FROM fact_leave_day WHERE UPPER(TRIM(COALESCE(leave_type,''))) NOT IN ('ANNUAL','CASUAL','SICK')"
    ).fetchone()[0]
    add("Accepted leave types", "fact_leave_day", invalid_leave_types == 0, 0, invalid_leave_types,
        "Allowed values observed in the provided synthetic dataset: Annual, Casual, Sick; confirm against HR policy for future source files")

    overlaps = conn.execute("""
      SELECT COUNT(*) FROM dim_employee a JOIN dim_employee b
      ON a.emp_id=b.emp_id AND a.employee_sk < b.employee_sk
      AND a.valid_from <= b.valid_to AND b.valid_from <= a.valid_to
    """).fetchone()[0]
    add("No overlapping SCD2 ranges", "dim_employee", overlaps == 0, 0, overlaps, "Pairwise overlap check across employee versions")

    # Source-to-stage reconciliation.
    recon_pairs = [
        ("raw_employees", "stg_employees", "stg_employee_dedup_log"),
        ("raw_employment_history", "stg_employment_history", "stg_employment_history_dedup_log"),
        ("raw_biometric_punches", "stg_punches_clean", "stg_rejected_punches", "stg_duplicate_punches"),
        ("raw_leave_requests", "stg_leave_requests"),
        ("raw_overtime_requests", "stg_overtime_requests"),
        ("raw_payroll_lines", "stg_payroll_lines"),
        ("raw_departments", "stg_departments"),
        ("raw_shifts", "stg_shifts"),
        ("raw_holidays", "stg_holidays"),
        ("raw_device_mapping", "stg_device_mapping", "stg_device_mapping_duplicate_log", "stg_rejected_device_mapping"),
        ("raw_shift_roster", "stg_roster_original", "stg_roster_duplicate_log", "stg_rejected_roster_rows"),
        ("raw_applications", "stg_applications"),
    ]
    for pair in recon_pairs:
        raw_name = pair[0]
        stage_names = pair[1:]
        raw_count = conn.execute(f'SELECT COUNT(*) FROM "{raw_name}"').fetchone()[0]
        stage_total = sum(conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in stage_names)
        if raw_name == "raw_biometric_punches":
            # All mapped clean, unmapped rejected, and near-duplicate rows are accounted for.
            pass
        passed = raw_count == stage_total
        add(f"Raw/staging reconciliation: {raw_name}", raw_name, passed, raw_count, stage_total,
            "Raw rows should equal clean/staged plus deduplicated/rejected rows; expanded roster and leave-day tables are excluded")

    # Original roster rows are reconciled exactly above; the filled roster may contain added weeks.
    roster_original = conn.execute("SELECT COUNT(*) FROM stg_roster_original").fetchone()[0]
    roster_staged = conn.execute("SELECT COUNT(*) FROM stg_shift_roster").fetchone()[0]
    add("Filled roster preserves all deduplicated source weeks", "stg_shift_roster", roster_staged >= roster_original,
        f">={roster_original}", roster_staged, "Filled weeks increase staged row count by design; source rows are reconciled separately")

    # Validate source-to-warehouse row counts for each fact.
    attendance_source = conn.execute("SELECT COUNT(*) FROM stg_attendance_daily").fetchone()[0]
    attendance_fact = conn.execute("SELECT COUNT(*) FROM fact_attendance_daily").fetchone()[0]
    add("Attendance rows loaded to fact", "fact_attendance_daily", attendance_source == attendance_fact, attendance_source, attendance_fact, "One fact row per employee per scheduled day")
    payroll_source = conn.execute("SELECT COUNT(*) FROM stg_payroll_lines").fetchone()[0]
    payroll_fact = conn.execute("SELECT COUNT(*) FROM fact_payroll_line").fetchone()[0]
    add("Payroll lines loaded to fact", "fact_payroll_line", payroll_source == payroll_fact, payroll_source, payroll_fact, "Each source payroll line remains a fact line")

    # Validate the configured device-clock correction against paired check-in/check-out
    # deviations. A documented row alone is not enough to mark this control as PASS.
    correction_row = conn.execute(
        "SELECT device_id, correction_minutes_applied FROM stg_clock_correction_evidence LIMIT 1"
    ).fetchone()
    clock_status = "FAIL"
    clock_details = "Clock correction evidence row is missing."
    clock_actual = "Missing"
    if correction_row is not None:
        device_id, applied_offset = correction_row[0], int(correction_row[1])
        clock_actual = f"Evidence present; {device_id} correction {applied_offset} minutes"
        clock_status = "WARN"
        clock_details = "No paired-shift diagnostic found; treat the correction as provisional."
        diagnostic_path = DOCS_DIR / "clock_pairing_diagnostic.csv"
        if diagnostic_path.exists():
            try:
                diag = pd.read_csv(diagnostic_path)
                device_diag = diag.loc[diag["device_id"].astype(str) == str(device_id)].copy()
                applied_row = device_diag.loc[
                    pd.to_numeric(device_diag["candidate_correction_minutes"], errors="coerce") == applied_offset
                ]
                baseline_row = device_diag.loc[
                    pd.to_numeric(device_diag["candidate_correction_minutes"], errors="coerce") == 0
                ]
                if not applied_row.empty and not baseline_row.empty:
                    applied = applied_row.iloc[0]
                    baseline = baseline_row.iloc[0]
                    checkin_gain = float(baseline["median_abs_checkin_deviation_min"]) - float(applied["median_abs_checkin_deviation_min"])
                    checkout_gain = float(baseline["median_abs_checkout_deviation_min"]) - float(applied["median_abs_checkout_deviation_min"])
                    combined_gain = float(baseline["median_combined_boundary_error_min"]) - float(applied["median_combined_boundary_error_min"])
                    paired_shifts = int(applied["paired_employee_shifts"])
                    best_offset = int(applied["best_tested_offset_minutes"])
                    other_devices = diag.loc[diag["device_id"].astype(str) != str(device_id)]
                    other_devices_best_zero = (
                        not other_devices.empty
                        and pd.to_numeric(other_devices["best_tested_offset_minutes"], errors="coerce").notna().all()
                        and pd.to_numeric(other_devices["best_tested_offset_minutes"], errors="coerce").eq(0).all()
                    )
                    clock_details = (
                        f"Paired-shift validation for {device_id}: applied={applied_offset} min; "
                        f"best tested={best_offset} min; paired shifts={paired_shifts}; "
                        f"median absolute check-in deviation improved {checkin_gain:.2f} min, "
                        f"check-out improved {checkout_gain:.2f} min, combined error improved {combined_gain:.2f} min; "
                        f"all other devices prefer zero offset={other_devices_best_zero}."
                    )
                    # Conservative acceptance thresholds: enough paired shifts, chosen offset
                    # is the best of -60/0/+60, both boundaries improve by >=10 minutes, and
                    # all other devices' best tested offsets remain zero.
                    confirmed = (
                        paired_shifts >= 100
                        and best_offset == applied_offset
                        and checkin_gain >= 10.0
                        and checkout_gain >= 10.0
                        and combined_gain >= 10.0
                        and other_devices_best_zero
                    )
                    clock_status = "PASS" if confirmed else "WARN"
            except (ValueError, TypeError, KeyError, IndexError) as exc:
                clock_details = f"Paired-shift diagnostic could not be interpreted: {exc}. Correction remains provisional."
    add("Device clock correction documented and validated", "stg_clock_correction_evidence",
        clock_status == "PASS", "Correction supported by paired-shift evidence", clock_actual,
        clock_details, status_override=clock_status)

    dq = pd.DataFrame(checks)
    conn.execute("DROP TABLE IF EXISTS dq_results")
    dq.to_sql("dq_results", conn, index=False, if_exists="replace")
    dq.to_csv(QUALITY_DIR / "dq_results.csv", index=False)
    return dq


def create_erd_png():
    """Create a simple ERD image when Pillow is available; always save Mermaid fallback."""
    erd_path = PROJECT_DIR / "warehouse" / "erd.png"
    mermaid = """erDiagram
  DIM_DEPARTMENT ||--o{ DIM_EMPLOYEE : contains
  DIM_EMPLOYEE ||--o{ FACT_ATTENDANCE_DAILY : employee
  DIM_EMPLOYEE ||--o{ FACT_LEAVE_DAY : employee
  DIM_EMPLOYEE ||--o{ FACT_OVERTIME_APPROVED : employee
  DIM_EMPLOYEE ||--o{ FACT_PAYROLL_LINE : employee
  DIM_DEPARTMENT ||--o{ FACT_ATTENDANCE_DAILY : department
  DIM_DEPARTMENT ||--o{ FACT_LEAVE_DAY : department
  DIM_DEPARTMENT ||--o{ FACT_OVERTIME_APPROVED : department
  DIM_DEPARTMENT ||--o{ FACT_PAYROLL_LINE : department
  DIM_DATE ||--o{ FACT_ATTENDANCE_DAILY : date
  DIM_DATE ||--o{ FACT_LEAVE_DAY : date
  DIM_DATE ||--o{ FACT_OVERTIME_APPROVED : date
  DIM_DATE ||--o{ FACT_PAYROLL_LINE : date
  DIM_SHIFT ||--o{ FACT_ATTENDANCE_DAILY : shift
"""
    (PROJECT_DIR / "warehouse" / "erd.mmd").write_text(mermaid, encoding="utf-8")
    try:
        from PIL import Image, ImageDraw, ImageFont
        width, height = 1600, 1050
        img = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("Arial.ttf", 24)
            small = ImageFont.truetype("Arial.ttf", 18)
        except Exception:
            font = ImageFont.load_default()
            small = ImageFont.load_default()
        boxes = {
            "dim_employee": (560, 80, 1040, 300, ["PK employee_sk", "emp_id (SCD2 natural key)", "FK dept_id", "valid_from / valid_to"]),
            "dim_department": (60, 100, 460, 300, ["PK dept_id", "dept_name", "plant", "cost_center"]),
            "dim_shift": (1160, 100, 1540, 280, ["PK shift_code", "start_time / end_time", "grace_minutes"]),
            "dim_date": (600, 400, 1000, 590, ["PK date_key", "year / month / quarter", "day_of_week"]),
            "fact_attendance_daily": (40, 700, 430, 980, ["PK attendance_sk", "FK employee_sk / dept_id", "FK shift_code / date_key", "status, hours, late, OT"]),
            "fact_leave_day": (460, 700, 790, 980, ["PK leave_day_sk", "FK employee_sk / dept_id", "FK date_key", "leave_type"]),
            "fact_overtime_approved": (820, 700, 1180, 980, ["PK request_id", "FK employee_sk / dept_id", "FK date_key", "approved_hours"]),
            "fact_payroll_line": (1210, 700, 1570, 980, ["PK line_id", "FK employee_sk / dept_id", "FK date_key", "component, amount, units"]),
        }
        for title, (x1,y1,x2,y2,lines) in boxes.items():
            draw.rounded_rectangle((x1,y1,x2,y2), radius=12, outline="#34495e", width=3, fill="#f4f7fb")
            draw.text((x1+12,y1+12), title, fill="#16324f", font=font)
            y = y1 + 56
            for line in lines:
                draw.text((x1+12,y), line, fill="#17202a", font=small)
                y += 36
        # Actual dimension-to-fact relationships from the CREATE TABLE DDL.
        # Box centers: department=(260,200), employee=(800,190), shift=(1350,190),
        # date=(800,495); facts: attendance=(235,840), leave=(625,840),
        # overtime=(1000,840), payroll=(1390,840).
        links = [
            # dim_department -> all four facts
            ((260,300),(235,700)), ((260,300),(625,700)), ((260,300),(1000,700)), ((260,300),(1390,700)),
            # dim_employee -> all four facts
            ((800,300),(235,700)), ((800,300),(625,700)), ((800,300),(1000,700)), ((800,300),(1390,700)),
            # dim_date -> all four facts
            ((800,590),(235,700)), ((800,590),(625,700)), ((800,590),(1000,700)), ((800,590),(1390,700)),
            # dim_shift -> fact_attendance_daily only
            ((1350,280),(235,700)),
        ]
        for start,end in links:
            draw.line([start,end], fill="#85929e", width=2)
        img.save(erd_path)
    except Exception:
        pass
    return erd_path


def write_cfo_findings(conn, query_results):
    q06 = query_results.get("q06_ot_overpayment", pd.DataFrame())
    q07 = query_results.get("q07_paid_zero_attendance", pd.DataFrame())
    q08 = query_results.get("q08_pay_after_separation", pd.DataFrame())
    q05 = query_results.get("q05_overtime_reconciliation", pd.DataFrame())

    lines = [
        "# CFO Findings - Indus Weave Textiles",
        "",
        "All numbers below are generated from the current local pipeline run. Recheck them after any transformation change.",
        "",
        "## Finding 1 - Paid overtime vs punch-computed overtime",
    ]
    if not q06.empty:
        top = q06.sort_values("excess_ot_hours", ascending=False).iloc[0]
        lines += [
            f"The largest flagged department/month is {top['dept_name']} ({top['plant']}) in {top['payroll_month']}: paid OT {top['paid_ot_hours']:.2f} hours versus computed OT {top['computed_ot_hours']:.2f} hours, an excess of {top['excess_ot_hours']:.2f} hours.",
            "Evidence query: analytics/q06_ot_overpayment.sql. Recommended action: audit approval records, punch correction, roster assignment and payroll processing for this department/month.",
        ]
    else:
        lines += ["No department/month exceeded the 15% threshold in the current query result.", "Evidence query: analytics/q06_ot_overpayment.sql. Recommended action: retain the reconciliation control and review any non-flagged outliers separately."]

    lines += ["", "## Finding 2 - Payroll paid with zero Present days"]
    if not q07.empty:
        lines += [
            f"The query identified {len(q07)} employee-month rows with positive earnings and zero Present days under the documented definition.",
            "Evidence query: analytics/q07_paid_zero_attendance.sql. Recommended action: reconcile missing-punch exceptions, approved leave, roster/shift allocation and payroll eligibility before treating these as errors.",
        ]
    else:
        lines += ["The query found no employee-month with positive earnings and zero Present days under the documented definition.", "Evidence query: analytics/q07_paid_zero_attendance.sql. Recommended action: keep this check in the monthly payroll close process."]

    lines += ["", "## Finding 3 - Payroll after separation month"]
    if not q08.empty:
        lines += [
            f"The query identified {len(q08)} employee-month rows with earnings in a month after the recorded separation month.",
            "Evidence query: analytics/q08_pay_after_separation.sql. Recommended action: verify final settlements and payroll effective dates against HR separation records.",
        ]
    else:
        lines += ["The query found no positive-earning payroll rows in a month after the recorded separation month.", "Evidence query: analytics/q08_pay_after_separation.sql. Recommended action: retain the control and validate final settlement treatment."]

    lines += [
        "", "## Interpretation and caveats",
        "- These are screening findings from the current pipeline, not automatically confirmed fraud or overpayment. Validate anomalies against source evidence.",
        "- Payroll exports provide payroll_month, not the exact payment date. Query 8 therefore flags a payroll month after the separation month, and cannot prove the exact day the payment was made.",
        "- The clock correction on DEV-SITE-01 is provisional: the current sample showed a modest improvement only. Do not describe it as conclusively proven unless additional punch/shift analysis confirms it.",
        "- Zero attendance in Query 7 is operationally defined as zero Present days; Missing Punch, Absent, On Leave, Holiday and Weekly Off are reported separately where available.",
    ]
    (PRESENTATION_DIR / "cfo_findings.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def create_presentation_pdf(query_results, dq_df):
    """Create six readable landscape slides with ReportLab canvas."""
    import textwrap
    text_path = PRESENTATION_DIR / "slides_content.md"
    q06 = query_results.get("q06_ot_overpayment", pd.DataFrame())
    q07 = query_results.get("q07_paid_zero_attendance", pd.DataFrame())
    q08 = query_results.get("q08_pay_after_separation", pd.DataFrame())
    fails = int((dq_df["status"] == "FAIL").sum()) if not dq_df.empty else 0
    warns = int((dq_df["status"] == "WARN").sum()) if not dq_df.empty else 0
    passes = int((dq_df["status"] == "PASS").sum()) if not dq_df.empty else 0
    slides = [
        ("Business problem", [
            "Indus Weave Textiles has six months of separate HR/HCM exports (April-September 2026).",
            "Core question: why do punch-computed overtime, approved overtime and paid overtime differ?",
            "Scope: preserve raw data, clean staging, model a warehouse, answer 10 SQL questions and prove data quality.",
        ]),
        ("Pipeline architecture", [
            "12 source files are loaded into SQLite with source metadata and row hashes.",
            "Staging handles employee deduplication, roster fill, biometric mapping, punch deduplication, leave approvals and attendance logic.",
            "Warehouse: dim_employee SCD Type 2, dim_department, dim_shift, dim_date and four fact tables.",
        ]),
        ("Data quality and reconciliation", [
            f"Current automated checks: {passes} PASS, {fails} FAIL, {warns} WARN.",
            "Unmapped and near-duplicate punches are retained in audit/rejected tables rather than silently discarded.",
            "Clock correction for DEV-SITE-01 is provisional; the diagnostic improvement is modest and requires further validation.",
        ]),
        ("Overtime reconciliation", [
            f"Q6 currently flags {len(q06)} department-month rows where paid overtime exceeds punch-computed overtime by more than 15%.",
            "Compare punch-computed OT, approved-request OT and payroll OVERTIME units as separate measures.",
            "Investigate roster, punch pairing, clock adjustment, approvals and payroll processing before classifying a difference as an error.",
        ]),
        ("Payroll exception checks", [
            f"Q7 identified {len(q07)} paid employee-month rows with zero Present days under the documented definition.",
            f"Q8 identified {len(q08)} positive-earning employee-month rows after the separation month.",
            "The source contains payroll month rather than exact payment date; Q8 is a monthly screening check, not proof of the exact payment date.",
        ]),
        ("Recommendations", [
            "Reconcile computed, approved and paid overtime every month at employee and department level.",
            "Review paid OT > computed OT by 15%, zero-Present payroll and payroll after separation month as exceptions.",
            "Validate each exception against source evidence and retain DQ checks as part of payroll close.",
        ]),
    ]
    with text_path.open("w", encoding="utf-8") as f:
        for idx, (title, bullets) in enumerate(slides, start=1):
            f.write(f"# {idx}. {title}\n\n")
            f.write("\n".join(f"- {bullet}" for bullet in bullets) + "\n\n")

    pdf_path = PRESENTATION_DIR / "slides.pdf"
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import landscape, A4
        from reportlab.pdfgen import canvas
        from reportlab.pdfbase.pdfmetrics import stringWidth

        page_w, page_h = landscape(A4)
        c = canvas.Canvas(str(pdf_path), pagesize=(page_w, page_h))
        margin = 48
        for page_num, (title, bullets) in enumerate(slides, start=1):
            c.setFillColor(colors.HexColor("#F4F7FB"))
            c.rect(0, 0, page_w, page_h, stroke=0, fill=1)
            c.setFillColor(colors.HexColor("#1F618D"))
            c.rect(0, page_h-16, page_w, 16, stroke=0, fill=1)
            c.setFillColor(colors.HexColor("#15324B"))
            c.setFont("Helvetica-Bold", 25)
            c.drawString(margin, page_h-72, f"{page_num}. {title}")
            y = page_h-125
            max_width = page_w - 2*margin - 35
            for bullet in bullets:
                words = bullet.split()
                lines = []
                current = ""
                for word in words:
                    candidate = (current + " " + word).strip()
                    if stringWidth(candidate, "Helvetica", 16) <= max_width:
                        current = candidate
                    else:
                        if current:
                            lines.append(current)
                        current = word
                if current:
                    lines.append(current)
                c.setFillColor(colors.HexColor("#1F618D"))
                c.circle(margin+5, y+3, 3, stroke=0, fill=1)
                c.setFillColor(colors.HexColor("#283747"))
                c.setFont("Helvetica", 16)
                for line in lines:
                    c.drawString(margin+22, y, line)
                    y -= 24
                y -= 22
            c.setStrokeColor(colors.HexColor("#D5DFE8"))
            c.line(margin, 64, page_w-margin, 64)
            c.setFont("Helvetica", 9)
            c.setFillColor(colors.HexColor("#566573"))
            c.drawString(margin, 43, "PeopleOps Data Hackathon | SQLite + Python + SQL")
            c.drawRightString(page_w-margin, 43, f"{page_num} / 6")
            c.showPage()
        c.save()
        return pdf_path
    except Exception as exc:
        print("PDF generation unavailable:", exc)
        print("Slide text saved to:", text_path)
        return None


def write_project_docs():
    readme = """# PeopleOps Data Hackathon - Indus Weave Textiles

## Scope
Python + SQLite pipeline for the April-September 2026 synthetic HR export case study.

## Run from project root
Run `python run_pipeline.py` to execute raw ingestion, staging/attendance, warehouse build, 10 SQL questions, CSV exports and data-quality checks. To run individual stages, execute `ingestion/load_raw.py`, `transform/stage2_clean_and_attendance.py`, then `warehouse/stage4_warehouse_analytics.py` in that order.

## Layout
- `data/raw/`: untouched copies of source exports
- `data/peopleops.sqlite`: SQLite database
- `ingestion/`: raw load scripts
- `transform/`: cleaning and attendance logic
- `warehouse/ddl/`: dimension/fact DDL; `warehouse/erd.png` is the relationship diagram
- `analytics/`: ten standalone SQL questions and `analytics/results/` CSV outputs
- `quality/dq_results.csv`: automated check results
- `presentation/cfo_findings.md`: generated CFO findings and caveats
- `presentation/slides.pdf`: six-slide draft when ReportLab is installed

## Important definitions and limitations
- Raw tables are preserved; transformations are written to staging/warehouse tables.
- Employee SCD Type 2 ranges use `effective_from` and the day before the next employee record; the latest version ends at `9999-12-31` to support SQLite as-of joins.
- Q03 absenteeism is defined as Absent / (Present + Absent + Missing Punch); On Leave, Holiday and Weekly Off are excluded.
- Q07 defines zero attendance as zero days marked Present; investigate missing punches, absences and leave before calling these errors.
- Q08 flags positive earnings in a payroll month after the separation month. The source does not contain exact payment dates, so it cannot prove the exact payment date.
- The `DEV-SITE-01` -60 minute clock correction is provisional based on a modest improvement in the sampled diagnostic. Validate before calling it conclusive.
- Generated results are exception screens, not automatic proof of fraud or overpayment. Investigate against source evidence.

All raw file changes must be implemented in code; do not edit source CSV/JSON files by hand. Review `quality/dq_results.csv` and resolve all FAIL checks before submission.
"""
    if not (PROJECT_DIR / "README.md").exists():
        (PROJECT_DIR / "README.md").write_text(readme, encoding="utf-8")
    if not (PROJECT_DIR / "requirements.txt").exists():
        (PROJECT_DIR / "requirements.txt").write_text(
            "pandas>=2.0\nreportlab>=4.0  # only required for presentation/slides.pdf\n",
            encoding="utf-8",
        )
    pipeline_lines = [
        "from pathlib import Path",
        "import subprocess",
        "import sys",
        "",
        "ROOT = Path(__file__).resolve().parent",
        "STAGES = [",
        '    ROOT / "ingestion" / "load_raw.py",',
        '    ROOT / "transform" / "stage2_clean_and_attendance.py",',
        '    ROOT / "warehouse" / "stage4_warehouse_analytics.py",',
        "]",
        "",
        "for stage in STAGES:",
        "    if not stage.exists():",
        '        raise FileNotFoundError(f"Required stage script is missing: {stage}")',
        '    print("\\n" + "=" * 68)',
        '    print("RUNNING:", stage.relative_to(ROOT))',
        '    print("=" * 68)',
        '    result = subprocess.run([sys.executable, str(stage)], cwd=ROOT)',
        "    if result.returncode != 0:",
        '        raise SystemExit(f"Pipeline stopped at {stage.name} with code {result.returncode}")',
        '',
        'print("\\nPIPELINE FINISHED. Review quality/dq_results.csv before submission.")',
    ]
    (PROJECT_DIR / "run_pipeline.py").write_text("\n".join(pipeline_lines) + "\n", encoding="utf-8")


def main():
    if not DB_PATH.exists():
        raise FileNotFoundError(f"SQLite database missing: {DB_PATH}")
    for folder in [DDL_DIR, ANALYTICS_DIR, RESULTS_DIR, QUALITY_DIR, PRESENTATION_DIR, DOCS_DIR]:
        folder.mkdir(parents=True, exist_ok=True)
    write_project_docs()

    conn = sqlite3.connect(DB_PATH)
    try:
        ensure_required_tables(conn)
        conn.execute("PRAGMA foreign_keys=OFF")
        dept_rows, emp_rows, shift_rows = create_dimension_data(conn)
        create_dimensions(conn, dept_rows, emp_rows, shift_rows)
        build_employee_month_snapshot(conn, emp_rows)
        conn.execute("PRAGMA foreign_keys=ON")
        create_fact_tables_and_load(conn)

        # Export DDL as standalone SQL files for the submission repo.
        (DDL_DIR / "01_dimensions.sql").write_text("""-- Dimension DDL: see stage4_warehouse_analytics.py for full rebuild automation.
CREATE TABLE dim_department (dept_id TEXT PRIMARY KEY, dept_name TEXT NOT NULL, plant TEXT, cost_center TEXT, previous_name TEXT, renamed_on TEXT);
CREATE TABLE dim_shift (shift_code TEXT PRIMARY KEY, shift_name TEXT NOT NULL, start_time TEXT, end_time TEXT, crosses_midnight TEXT, grace_minutes REAL, start_minutes INTEGER, end_minutes INTEGER);
CREATE TABLE dim_date (date_key TEXT PRIMARY KEY, year INTEGER NOT NULL, month INTEGER NOT NULL, month_name TEXT NOT NULL, quarter INTEGER NOT NULL, day_of_month INTEGER NOT NULL, day_of_week INTEGER NOT NULL, day_name TEXT NOT NULL, is_weekend INTEGER NOT NULL, month_start TEXT NOT NULL, month_end TEXT NOT NULL);
CREATE TABLE dim_employee (employee_sk INTEGER PRIMARY KEY, emp_id TEXT NOT NULL, full_name TEXT, gender TEXT, plant TEXT, plant_code TEXT, hire_date TEXT, separation_date TEXT, annual_leave_entitlement REAL, employment_type TEXT, status TEXT, dept_id TEXT NOT NULL REFERENCES dim_department(dept_id), designation TEXT, grade TEXT, basic_salary REAL, valid_from TEXT NOT NULL, valid_to TEXT NOT NULL, is_current INTEGER NOT NULL, change_reason TEXT, UNIQUE(emp_id, valid_from));
""", encoding="utf-8")
        (DDL_DIR / "02_facts.sql").write_text("""-- Fact-table DDL for SQLite. Run after 01_dimensions.sql.
CREATE TABLE fact_attendance_daily (
 attendance_sk INTEGER PRIMARY KEY AUTOINCREMENT,
 emp_id TEXT NOT NULL,
 employee_sk INTEGER NOT NULL REFERENCES dim_employee(employee_sk),
 dept_id TEXT REFERENCES dim_department(dept_id),
 shift_code TEXT REFERENCES dim_shift(shift_code),
 date_key TEXT NOT NULL REFERENCES dim_date(date_key),
 attendance_status TEXT NOT NULL,
 punch_count INTEGER NOT NULL,
 check_in TEXT, check_out TEXT, worked_hours REAL,
 late_minutes REAL, early_departure_minutes REAL, overtime_hours REAL,
 UNIQUE(emp_id,date_key)
);
CREATE TABLE fact_leave_day (
 leave_day_sk INTEGER PRIMARY KEY AUTOINCREMENT,
 request_id TEXT, emp_id TEXT NOT NULL,
 employee_sk INTEGER NOT NULL REFERENCES dim_employee(employee_sk),
 dept_id TEXT REFERENCES dim_department(dept_id),
 date_key TEXT NOT NULL REFERENCES dim_date(date_key),
 leave_type TEXT, UNIQUE(request_id,emp_id,date_key)
);
CREATE TABLE fact_overtime_approved (
 request_id TEXT PRIMARY KEY,
 emp_id TEXT NOT NULL,
 employee_sk INTEGER NOT NULL REFERENCES dim_employee(employee_sk),
 dept_id TEXT REFERENCES dim_department(dept_id),
 date_key TEXT NOT NULL REFERENCES dim_date(date_key),
 requested_hours REAL, approved_hours REAL, status TEXT NOT NULL
);
CREATE TABLE fact_payroll_line (
 line_id TEXT PRIMARY KEY,
 emp_id TEXT NOT NULL,
 employee_sk INTEGER NOT NULL REFERENCES dim_employee(employee_sk),
 dept_id TEXT REFERENCES dim_department(dept_id),
 date_key TEXT NOT NULL REFERENCES dim_date(date_key),
 payroll_month TEXT NOT NULL, component TEXT NOT NULL, component_type TEXT NOT NULL,
 units REAL, amount REAL, attribution_date TEXT NOT NULL
);
""", encoding="utf-8")

        queries = write_analytics_sql()
        query_results = {}
        print("\n===== RUNNING 10 ANALYTICS QUERIES =====")
        for filename, sql in queries.items():
            result = pd.read_sql_query(sql, conn)
            out_name = filename.replace(".sql", ".csv")
            result.to_csv(RESULTS_DIR / out_name, index=False)
            query_results[filename.replace(".sql", "")] = result
            print(f"{filename}: {len(result):,} rows -> {out_name}")

        dq_df = run_dq_checks(conn)
        create_erd_png()
        write_cfo_findings(conn, query_results)
        pdf_path = create_presentation_pdf(query_results, dq_df)

        failures = int((dq_df["status"] == "FAIL").sum()) if not dq_df.empty else 0
        print("\n" + "=" * 68)
        print("STAGE 4-6 SUMMARY")
        print("=" * 68)
        for table in ["dim_employee", "dim_department", "dim_shift", "dim_date", "fact_attendance_daily", "fact_leave_day", "fact_overtime_approved", "fact_payroll_line", "employee_month_snapshot"]:
            count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            print(f"{table}: {count:,} rows")
        print(f"SQL queries: {len(queries)}")
        print(
            f"DQ checks: {len(dq_df)}; PASS: {int((dq_df['status']=='PASS').sum())}; "
            f"WARN: {int((dq_df['status']=='WARN').sum())}; FAIL: {failures}"
        )
        print("Database:", DB_PATH)
        print("SQL files:", ANALYTICS_DIR)
        print("Result CSVs:", RESULTS_DIR)
        print("DQ results:", QUALITY_DIR / "dq_results.csv")
        print("ERD:", PROJECT_DIR / "warehouse" / "erd.png")
        print("CFO findings:", PRESENTATION_DIR / "cfo_findings.md")
        print("Slides PDF:", pdf_path or "PDF library unavailable; see slides_content.md")
        for result_name in ["q05_overtime_reconciliation", "q06_ot_overpayment", "q07_paid_zero_attendance", "q08_pay_after_separation"]:
            result_df = query_results.get(result_name, pd.DataFrame())
            print(f"\nPreview: {result_name} ({len(result_df)} rows)")
            print(result_df.head(5).to_string(index=False) if not result_df.empty else "No rows returned.")
        print("\nNote: review FAIL/WARN checks and clock-correction evidence before submission.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
