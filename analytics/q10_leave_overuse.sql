-- Q10: Employees whose approved annual leave days in the six-month period exceed full-year entitlement.
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
ORDER BY days_over_entitlement DESC,l.emp_id;
