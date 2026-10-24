from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
STAGES = [
    ROOT / "ingestion" / "load_raw.py",
    ROOT / "transform" / "stage2_clean_and_attendance.py",
    ROOT / "warehouse" / "stage4_warehouse_analytics.py",
]

for stage in STAGES:
    if not stage.exists():
        raise FileNotFoundError(f"Required stage script is missing: {stage}")
    print("\n" + "=" * 68)
    print("RUNNING:", stage.relative_to(ROOT))
    print("=" * 68)
    result = subprocess.run([sys.executable, str(stage)], cwd=ROOT)
    if result.returncode != 0:
        raise SystemExit(f"Pipeline stopped at {stage.name} with code {result.returncode}")

print("\nPIPELINE FINISHED. Review quality/dq_results.csv before submission.")
