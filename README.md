# PeopleOps Data Hackathon - Indus Weave Textiles

## Scope
Python + SQLite pipeline for the April-September 2026 synthetic HR export case study.

## Run from project root
Run `python run_pipeline.py` to execute raw ingestion, staging/attendance, warehouse build, 10 SQL questions, CSV exports and data-quality checks. To run individual stages, execute `ingestion/load_raw.py`, `transform/stage2_clean_and_attendance.py`, then `warehouse/stage4_warehouse_analytics.py` in that order.

## Layout
- `data/raw/`: untouched copies of source exports
- `data/peopleops.sqlite`: SQLite database
- `ingestion/`: raw load scripts
- `transform/`: cleaning and attendance logic
- `warehouse/ddl/`: dimension/fact DDL; `warehouse/erd.png` is the relationship diagram
- `analytics/`: ten standalone SQL questions and `analytics/results/` CSV outputs
- `quality/dq_results.csv`: automated check results
- `presentation/cfo_findings.md`: generated CFO findings and caveats
- `presentation/slides.pdf`: six-slide draft when ReportLab is installed

## Important definitions and limitations
- Raw tables are preserved; transformations are written to staging/warehouse tables.
- Employee SCD Type 2 ranges use `effective_from` and the day before the next employee record; the latest version ends at `9999-12-31` to support SQLite as-of joins.
- Q03 absenteeism is defined as Absent / (Present + Absent + Missing Punch); On Leave, Holiday and Weekly Off are excluded.
- Q07 defines zero attendance as zero days marked Present; investigate missing punches, absences and leave before calling these errors.
- Q08 flags positive earnings in a payroll month after the separation month. The source does not contain exact payment dates, so it cannot prove the exact payment date.
- The `DEV-SITE-01` -60 minute clock correction is provisional based on a modest improvement in the sampled diagnostic. Validate before calling it conclusive.
- Generated results are exception screens, not automatic proof of fraud or overpayment. Investigate against source evidence.

All raw file changes must be implemented in code; do not edit source CSV/JSON files by hand. Review `quality/dq_results.csv` and resolve all FAIL checks before submission.
