-- Q01: Month-end headcount by plant and department (April-September 2026).
WITH month_depts AS (SELECT m.payroll_month, d.dept_id, d.dept_name, d.plant FROM (SELECT payroll_month FROM employee_month_snapshot GROUP BY payroll_month) m CROSS JOIN dim_department d),
headcount AS (
  SELECT payroll_month, dept_id_month_end AS dept_id, COUNT(DISTINCT emp_id) AS month_end_headcount
  FROM employee_month_snapshot WHERE active_at_month_end=1
  GROUP BY payroll_month, dept_id_month_end
)
SELECT md.payroll_month, md.plant, md.dept_id, md.dept_name,
       COALESCE(h.month_end_headcount,0) AS month_end_headcount
FROM month_depts md LEFT JOIN headcount h
 ON h.payroll_month=md.payroll_month AND h.dept_id=md.dept_id
ORDER BY md.payroll_month, md.plant, md.dept_id;
