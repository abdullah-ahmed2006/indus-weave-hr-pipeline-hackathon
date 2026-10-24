-- Q09: Monthly payroll cost = EARNING lines; per employee = cost / distinct paid employees.
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
FROM lagged ORDER BY payroll_month,plant,dept_id;
