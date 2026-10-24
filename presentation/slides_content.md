# 1. Business problem

- Indus Weave Textiles has six months of separate HR/HCM exports (April-September 2026).
- Core question: why do punch-computed overtime, approved overtime and paid overtime differ?
- Scope: preserve raw data, clean staging, model a warehouse, answer 10 SQL questions and prove data quality.

# 2. Pipeline architecture

- 12 source files are loaded into SQLite with source metadata and row hashes.
- Staging handles employee deduplication, roster fill, biometric mapping, punch deduplication, leave approvals and attendance logic.
- Warehouse: dim_employee SCD Type 2, dim_department, dim_shift, dim_date and four fact tables.

# 3. Data quality and reconciliation

- Current automated checks: 35 PASS, 0 FAIL, 1 WARN.
- Unmapped and near-duplicate punches are retained in audit/rejected tables rather than silently discarded.
- Clock correction for DEV-SITE-01 is provisional; the diagnostic improvement is modest and requires further validation.

# 4. Overtime reconciliation

- Q6 currently flags 23 department-month rows where paid overtime exceeds punch-computed overtime by more than 15%.
- Compare punch-computed OT, approved-request OT and payroll OVERTIME units as separate measures.
- Investigate roster, punch pairing, clock adjustment, approvals and payroll processing before classifying a difference as an error.

# 5. Payroll exception checks

- Q7 identified 59 paid employee-month rows with zero Present days under the documented definition.
- Q8 identified 21 positive-earning employee-month rows after the separation month.
- The source contains payroll month rather than exact payment date; Q8 is a monthly screening check, not proof of the exact payment date.

# 6. Recommendations

- Reconcile computed, approved and paid overtime every month at employee and department level.
- Review paid OT > computed OT by 15%, zero-Present payroll and payroll after separation month as exceptions.
- Validate each exception against source evidence and retain DQ checks as part of payroll close.

