-- Q05: Punch-computed vs approved vs paid overtime, grouped by month-end/last-working-day department.
WITH computed AS (
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
 SELECT m.payroll_month,d.dept_id,d.dept_name,d.plant
 FROM (SELECT payroll_month FROM employee_month_snapshot GROUP BY payroll_month) m
 CROSS JOIN dim_department d
)
SELECT md.payroll_month,md.plant,md.dept_id,md.dept_name,
 COALESCE(c.computed_ot_hours,0) AS computed_ot_hours,
 COALESCE(a.approved_ot_hours,0) AS approved_ot_hours,
 COALESCE(p.paid_ot_hours,0) AS paid_ot_hours,
 COALESCE(p.paid_ot_amount,0) AS paid_ot_amount
FROM month_depts md
LEFT JOIN computed c ON c.payroll_month=md.payroll_month AND c.dept_id=md.dept_id
LEFT JOIN approved a ON a.payroll_month=md.payroll_month AND a.dept_id=md.dept_id
LEFT JOIN paid p ON p.payroll_month=md.payroll_month AND p.dept_id=md.dept_id
ORDER BY md.payroll_month,md.plant,md.dept_id;
