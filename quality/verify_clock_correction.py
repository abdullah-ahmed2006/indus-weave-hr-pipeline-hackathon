"""Validate candidate device-clock offsets against employee shift boundaries.

This is diagnostic only: it does not modify raw punches, the database, or the
configured production correction. Run from any working directory with the
project database built through Stage 2/3.
"""
from pathlib import Path
import sqlite3
import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_DIR / "data" / "peopleops.sqlite"
REPORT_PATH = PROJECT_DIR / "docs" / "clock_pairing_diagnostic.csv"
TEST_OFFSETS = (-60, 0, 60)


def monday_iso(value):
    value = pd.Timestamp(value).normalize()
    return (value - pd.Timedelta(days=value.day_of_week)).strftime("%Y-%m-%d")


def main():
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Database missing: {DB_PATH}. Run the pipeline first.")

    with sqlite3.connect(DB_PATH) as conn:
        punches = pd.read_sql_query(
            "SELECT punch_id, device_id, device_user_id, punch_time FROM raw_biometric_punches",
            conn,
        )
        mapping = pd.read_sql_query(
            "SELECT device_id, device_user_id, emp_id FROM stg_device_mapping",
            conn,
        )
        roster = pd.read_sql_query(
            "SELECT emp_id, week_start, shift_code FROM stg_shift_roster",
            conn,
        )
        shifts = pd.read_sql_query("SELECT * FROM stg_shifts", conn)

    punches["punch_time"] = pd.to_datetime(punches["punch_time"], errors="coerce", format="mixed")
    punches = punches.merge(
        mapping,
        on=["device_id", "device_user_id"],
        how="left",
        validate="many_to_one",
    )
    punches = punches.dropna(subset=["emp_id", "punch_time"]).copy()

    roster["week_start"] = pd.to_datetime(roster["week_start"], errors="coerce")
    roster = roster.dropna(subset=["emp_id", "week_start", "shift_code"])
    roster_lookup = {
        (str(row.emp_id), pd.Timestamp(row.week_start).strftime("%Y-%m-%d")): str(row.shift_code).upper()
        for row in roster.itertuples(index=False)
    }

    shift_lookup = {}
    for row in shifts.to_dict("records"):
        shift_code = str(row.get("shift_code", "")).upper()
        if not shift_code:
            continue
        shift_lookup[shift_code] = {
            "start": int(float(row["start_minutes"])),
            "end": int(float(row["end_minutes"])),
            "crosses": str(row.get("crosses_midnight", "false")).strip().casefold() in {"1", "true", "yes"},
        }

    output = []
    for device_id, device_punches in punches.groupby("device_id", sort=True):
        device_punches = device_punches.sort_values("punch_time").copy()
        for offset in TEST_OFFSETS:
            adjusted_times = device_punches["punch_time"] + pd.to_timedelta(offset, unit="m")
            assigned = []
            for emp_id, ts in zip(device_punches["emp_id"].astype(str), adjusted_times):
                current_day = ts.normalize()
                prior_day = current_day - pd.Timedelta(days=1)
                previous_shift = roster_lookup.get((emp_id, monday_iso(prior_day)))
                current_shift = roster_lookup.get((emp_id, monday_iso(current_day)))

                if ts.hour < 12 and previous_shift == "NIGHT":
                    shift_day = prior_day
                elif ts.hour >= 18 and current_shift == "NIGHT":
                    shift_day = current_day
                else:
                    shift_day = current_day

                shift_code = roster_lookup.get((emp_id, monday_iso(shift_day)))
                if shift_code not in shift_lookup:
                    continue
                assigned.append((emp_id, shift_day, shift_code, ts))

            if not assigned:
                output.append({
                    "device_id": device_id,
                    "candidate_correction_minutes": offset,
                    "paired_employee_shifts": 0,
                    "median_abs_checkin_deviation_min": None,
                    "median_abs_checkout_deviation_min": None,
                    "median_combined_boundary_error_min": None,
                    "pct_paired_shifts_both_within_60_min": None,
                })
                continue

            frame = pd.DataFrame(
                assigned,
                columns=["emp_id", "shift_date", "shift_code", "adjusted_time"],
            )
            paired = frame.groupby(["emp_id", "shift_date", "shift_code"], as_index=False).agg(
                punch_count=("adjusted_time", "size"),
                first_punch=("adjusted_time", "min"),
                last_punch=("adjusted_time", "max"),
            )
            paired = paired.loc[paired["punch_count"] >= 2].copy()

            if paired.empty:
                output.append({
                    "device_id": device_id,
                    "candidate_correction_minutes": offset,
                    "paired_employee_shifts": 0,
                    "median_abs_checkin_deviation_min": None,
                    "median_abs_checkout_deviation_min": None,
                    "median_combined_boundary_error_min": None,
                    "pct_paired_shifts_both_within_60_min": None,
                })
                continue

            paired["shift_date"] = pd.to_datetime(paired["shift_date"])
            paired["start_minutes"] = paired["shift_code"].map(
                {k: v["start"] for k, v in shift_lookup.items()}
            )
            paired["end_minutes"] = paired["shift_code"].map(
                {k: v["end"] for k, v in shift_lookup.items()}
            )
            paired["crosses_midnight"] = paired["shift_code"].map(
                {k: v["crosses"] for k, v in shift_lookup.items()}
            )
            paired["scheduled_start"] = paired["shift_date"] + pd.to_timedelta(paired["start_minutes"], unit="m")
            paired["scheduled_end"] = paired["shift_date"] + pd.to_timedelta(paired["end_minutes"], unit="m")
            overnight = paired["crosses_midnight"] | (paired["end_minutes"] <= paired["start_minutes"])
            paired.loc[overnight, "scheduled_end"] += pd.Timedelta(days=1)
            paired["abs_checkin_deviation_min"] = (
                (paired["first_punch"] - paired["scheduled_start"]).dt.total_seconds().abs() / 60
            )
            paired["abs_checkout_deviation_min"] = (
                (paired["last_punch"] - paired["scheduled_end"]).dt.total_seconds().abs() / 60
            )
            paired["combined_error_min"] = (
                paired["abs_checkin_deviation_min"] + paired["abs_checkout_deviation_min"]
            ) / 2
            both_near = (
                paired["abs_checkin_deviation_min"].le(60)
                & paired["abs_checkout_deviation_min"].le(60)
            )

            output.append({
                "device_id": device_id,
                "candidate_correction_minutes": offset,
                "paired_employee_shifts": int(len(paired)),
                "median_abs_checkin_deviation_min": round(float(paired["abs_checkin_deviation_min"].median()), 2),
                "median_abs_checkout_deviation_min": round(float(paired["abs_checkout_deviation_min"].median()), 2),
                "median_combined_boundary_error_min": round(float(paired["combined_error_min"].median()), 2),
                "pct_paired_shifts_both_within_60_min": round(float(both_near.mean() * 100), 2),
            })

    results = pd.DataFrame(output)
    valid_scores = results.dropna(subset=["median_combined_boundary_error_min"])
    if not valid_scores.empty:
        best_rows = valid_scores.loc[
            valid_scores.groupby("device_id")["median_combined_boundary_error_min"].idxmin(),
            ["device_id", "candidate_correction_minutes"],
        ].rename(columns={"candidate_correction_minutes": "best_tested_offset_minutes"})
        results = results.merge(best_rows, on="device_id", how="left")
    else:
        results["best_tested_offset_minutes"] = pd.NA
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    results.to_csv(REPORT_PATH, index=False)

    print("CLOCK CORRECTION VALIDATION (diagnostic only; no data changed)")
    print(results.to_string(index=False))
    print("\nSaved report:", REPORT_PATH)
    print("Compare the paired-shift boundary errors for -60, 0 and +60 minutes.")
    print("Do not call the correction proven unless the intended offset clearly improves both check-in and check-out metrics.")


if __name__ == "__main__":
    main()
