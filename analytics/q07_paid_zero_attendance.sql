-- Q07: Positive payroll earnings but zero Present days. 'Zero attendance' here means zero Present days.
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
ORDER BY p.payroll_month,p.total_earnings DESC,p.emp_id;
