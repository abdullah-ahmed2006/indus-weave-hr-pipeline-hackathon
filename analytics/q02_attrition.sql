-- Q02: Monthly attrition = separations / average opening and closing headcount.
WITH month_depts AS (SELECT m.payroll_month, d.dept_id, d.dept_name, d.plant FROM (SELECT payroll_month FROM employee_month_snapshot GROUP BY payroll_month) m CROSS JOIN dim_department d),
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
ORDER BY md.payroll_month, md.plant, md.dept_id;
