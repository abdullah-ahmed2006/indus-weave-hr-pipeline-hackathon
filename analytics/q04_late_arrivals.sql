-- Q04: Top 10 employees with the most late arrivals and their latest known department.
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
LIMIT 10;
