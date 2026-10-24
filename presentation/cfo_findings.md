# CFO Findings - Indus Weave Textiles

All numbers below are generated from the current local pipeline run. Recheck them after any transformation change.

## Finding 1 - Paid overtime vs punch-computed overtime
The largest flagged department/month is Stitching (Faisalabad Plant) in 2026-09: paid OT 1778.50 hours versus computed OT 729.50 hours, an excess of 1049.00 hours.
Evidence query: analytics/q06_ot_overpayment.sql. Recommended action: audit approval records, punch correction, roster assignment and payroll processing for this department/month.

## Finding 2 - Payroll paid with zero Present days
The query identified 59 employee-month rows with positive earnings and zero Present days under the documented definition.
Evidence query: analytics/q07_paid_zero_attendance.sql. Recommended action: reconcile missing-punch exceptions, approved leave, roster/shift allocation and payroll eligibility before treating these as errors.

## Finding 3 - Payroll after separation month
The query identified 21 employee-month rows with earnings in a month after the recorded separation month.
Evidence query: analytics/q08_pay_after_separation.sql. Recommended action: verify final settlements and payroll effective dates against HR separation records.

## Interpretation and caveats
- These are screening findings from the current pipeline, not automatically confirmed fraud or overpayment. Validate anomalies against source evidence.
- Payroll exports provide payroll_month, not the exact payment date. Query 8 therefore flags a payroll month after the separation month, and cannot prove the exact day the payment was made.
- The clock correction on DEV-SITE-01 is provisional: the current sample showed a modest improvement only. Do not describe it as conclusively proven unless additional punch/shift analysis confirms it.
- Zero attendance in Query 7 is operationally defined as zero Present days; Missing Punch, Absent, On Leave, Holiday and Weekly Off are reported separately where available.
