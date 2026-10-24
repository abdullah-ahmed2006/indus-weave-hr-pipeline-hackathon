-- Q03: Absenteeism = Absent / (Present + Absent + Missing Punch); approved leave, holidays and weekly offs excluded.
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
ORDER BY mo.payroll_month,p.plant;
