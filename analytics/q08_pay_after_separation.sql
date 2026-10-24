-- Q08: Payroll in a month after separation month. Exact pay date is not provided in the source.
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
ORDER BY p.payroll_month,p.emp_id;
