-- Fact-table DDL for SQLite. Run after 01_dimensions.sql.
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
