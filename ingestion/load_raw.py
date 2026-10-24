
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
import pandas as pd


# --------------------------------------------------
# 1. PROJECT SETTINGS
# --------------------------------------------------

PROJECT_DIR = Path(__file__).resolve().parents[1]
RAW_DIR = PROJECT_DIR / "data" / "raw"
DOCS_DIR = PROJECT_DIR / "docs"

DB_PATH = PROJECT_DIR / "data" / "peopleops.sqlite"
REPORT_PATH = DOCS_DIR / "raw_load_audit.csv"

EXPECTED_COUNTS = {
    "employees.csv": 1316,
    "employment_history.csv": 3686,
    "departments.csv": 25,
    "shifts.json": 4,
    "shift_roster.csv": 36402,
    "device_mapping.csv": 1260,
    "biometric_punches.csv": 345280,
    "holidays.csv": 12,
    "leave_requests.json": 5372,
    "overtime_requests.csv": 22098,
    "payroll_lines.csv": 53617,
    "applications.csv": 3835,
}

METADATA_COLUMNS = {
    "_source_file",
    "_loaded_at",
    "_row_hash",
    "_source_row_number",
    "_raw_record_json",
}


# --------------------------------------------------
# 2. HELPER FUNCTIONS
# --------------------------------------------------

def quote_identifier(name):
    """Safely quote a SQLite table or column name."""
    return '"' + name.replace('"', '""') + '"'


def get_table_name(file_path):
    """Example: employees.csv becomes raw_employees."""
    return "raw_" + file_path.stem.lower()


def validate_source_columns(columns):
    """Prevent source fields from overwriting metadata."""
    duplicates = set(columns) & METADATA_COLUMNS

    if duplicates:
        raise ValueError(
            f"Source columns conflict with metadata: {duplicates}"
        )

    if len(columns) != len(set(columns)):
        raise ValueError("Duplicate source column names detected.")


def get_json_records(file_path):
    """
    Read JSON without flattening nested objects.
    Each top-level business record becomes one raw row.
    """
    with file_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    if isinstance(payload, list):
        records = payload

    elif isinstance(payload, dict):
        # Prefer the collection belonging to this dataset.
        preferred_keys = {
            "shifts.json": ["shifts"],
            "leave_requests.json": [
                "leave_requests",
                "requests",
                "data",
                "records",
            ],
        }

        records = None

        for key in preferred_keys.get(file_path.name, []):
            if isinstance(payload.get(key), list):
                records = payload[key]
                break

        # Fallback: accept a single list at the top level.
        if records is None:
            lists = [
                value
                for value in payload.values()
                if isinstance(value, list)
            ]

            if len(lists) == 1:
                records = lists[0]

        if records is None:
            raise ValueError(
                f"Could not identify the record list in "
                f"{file_path.name}. Inspect its JSON structure first."
            )

    else:
        raise ValueError(
            f"Unexpected JSON structure in {file_path.name}"
        )

    # Keep non-dictionary records without discarding them.
    return [
        record if isinstance(record, dict) else {"value": record}
        for record in records
    ]


def json_value_to_text(value):
    """Represent each JSON field as text without flattening it."""
    if value is None:
        return None

    if isinstance(value, (dict, list)):
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    if isinstance(value, bool):
        return "true" if value else "false"

    return str(value)


def create_raw_table(
    connection,
    table_name,
    source_columns,
    is_json,
):
    """Create a raw table and verify its column structure."""
    metadata = [
        "_source_file",
        "_loaded_at",
        "_row_hash",
        "_source_row_number",
    ]

    if is_json:
        metadata.append("_raw_record_json")

    all_columns = source_columns + metadata

    definitions = [
        f"{quote_identifier(column)} TEXT"
        for column in source_columns
    ]

    for column in metadata:
        definitions.append(
            f"{quote_identifier(column)} TEXT NOT NULL"
        )

    definitions.append(
        "UNIQUE "
        '("_source_file", "_source_row_number")'
    )

    create_sql = (
        f"CREATE TABLE IF NOT EXISTS "
        f"{quote_identifier(table_name)} "
        f"({', '.join(definitions)})"
    )

    connection.execute(create_sql)

    actual_columns = [
        row[1]
        for row in connection.execute(
            f"PRAGMA table_info({quote_identifier(table_name)})"
        )
    ]

    if actual_columns != all_columns:
        raise RuntimeError(
            f"Schema mismatch in {table_name}.\n"
            f"Expected: {all_columns}\n"
            f"Found: {actual_columns}"
        )

    return all_columns


def make_upsert_sql(table_name, source_columns, is_json):
    """Insert source rows; update existing source positions on rerun."""
    metadata = [
        "_source_file",
        "_loaded_at",
        "_row_hash",
        "_source_row_number",
    ]

    if is_json:
        metadata.append("_raw_record_json")

    columns = source_columns + metadata

    column_sql = ", ".join(
        quote_identifier(column) for column in columns
    )
    placeholders = ", ".join("?" for _ in columns)

    update_columns = source_columns + [
        "_loaded_at",
        "_row_hash",
    ]

    if is_json:
        update_columns.append("_raw_record_json")

    update_sql = ", ".join(
        f"{quote_identifier(column)} = "
        f"excluded.{quote_identifier(column)}"
        for column in update_columns
    )

    return f"""
        INSERT INTO {quote_identifier(table_name)}
            ({column_sql})
        VALUES ({placeholders})
        ON CONFLICT ("_source_file", "_source_row_number")
        DO UPDATE SET {update_sql}
    """


# --------------------------------------------------
# 3. INGEST CSV FILES
# --------------------------------------------------

def load_csv(connection, file_path):
    table_name = get_table_name(file_path)

    header_df = pd.read_csv(
        file_path,
        dtype="string",
        keep_default_na=False,
        nrows=0,
    )

    columns = header_df.columns.tolist()
    validate_source_columns(columns)

    create_raw_table(
        connection,
        table_name,
        columns,
        is_json=False,
    )

    insert_sql = make_upsert_sql(
        table_name,
        columns,
        is_json=False,
    )

    # Refresh only this source file's table rows.
    # The full source file remains unchanged on disk.
    connection.execute(
        f"DELETE FROM {quote_identifier(table_name)} "
        'WHERE "_source_file" = ?',
        (file_path.name,),
    )

    loaded_at = datetime.now(timezone.utc).isoformat()
    source_rows = 0
    batch = []
    batch_size = 20000

    for chunk in pd.read_csv(
        file_path,
        dtype="string",
        keep_default_na=False,
        chunksize=batch_size,
    ):
        for row in chunk.itertuples(index=False, name=None):
            source_rows += 1

            values = [
                "" if pd.isna(value) else str(value)
                for value in row
            ]

            hash_input = json.dumps(
                values,
                ensure_ascii=False,
                separators=(",", ":"),
            )

            row_hash = hashlib.sha256(
                hash_input.encode("utf-8")
            ).hexdigest()

            batch.append(
                values
                + [
                    file_path.name,
                    loaded_at,
                    row_hash,
                    str(source_rows),
                ]
            )

            if len(batch) >= batch_size:
                connection.executemany(insert_sql, batch)
                batch.clear()

    if batch:
        connection.executemany(insert_sql, batch)

    table_rows = connection.execute(
        f"SELECT COUNT(*) FROM {quote_identifier(table_name)}"
    ).fetchone()[0]

    return source_rows, table_name, table_rows


# --------------------------------------------------
# 4. INGEST JSON FILES
# --------------------------------------------------

def load_json(connection, file_path):
    table_name = get_table_name(file_path)
    records = get_json_records(file_path)

    # Preserve every source field; nested lists and objects
    # are stored as JSON text, not flattened.
    columns = list(
        dict.fromkeys(
            key
            for record in records
            for key in record.keys()
        )
    )

    validate_source_columns(columns)

    create_raw_table(
        connection,
        table_name,
        columns,
        is_json=True,
    )

    insert_sql = make_upsert_sql(
        table_name,
        columns,
        is_json=True,
    )

    connection.execute(
        f"DELETE FROM {quote_identifier(table_name)} "
        'WHERE "_source_file" = ?',
        (file_path.name,),
    )

    loaded_at = datetime.now(timezone.utc).isoformat()
    batch = []
    batch_size = 20000

    for row_number, record in enumerate(records, start=1):
        raw_record_json = json.dumps(
            record,
            ensure_ascii=False,
            separators=(",", ":"),
        )

        row_hash = hashlib.sha256(
            raw_record_json.encode("utf-8")
        ).hexdigest()

        values = [
            json_value_to_text(record.get(column))
            for column in columns
        ]

        batch.append(
            values
            + [
                file_path.name,
                loaded_at,
                row_hash,
                str(row_number),
                raw_record_json,
            ]
        )

        if len(batch) >= batch_size:
            connection.executemany(insert_sql, batch)
            batch.clear()

    if batch:
        connection.executemany(insert_sql, batch)

    table_rows = connection.execute(
        f"SELECT COUNT(*) FROM {quote_identifier(table_name)}"
    ).fetchone()[0]

    return len(records), table_name, table_rows


# --------------------------------------------------
# 5. RUN THE RAW INGESTION PIPELINE
# --------------------------------------------------

def main():
    if not RAW_DIR.exists():
        raise FileNotFoundError(
            f"Raw data folder not found: {RAW_DIR}"
        )

    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    source_files = (
        sorted(RAW_DIR.glob("*.csv"))
        + sorted(RAW_DIR.glob("*.json"))
    )

    if len(source_files) != 12:
        raise RuntimeError(
            f"Expected 12 source files, found {len(source_files)}"
        )

    print("=" * 65)
    print("PEOPLEOPS - RAW INGESTION")
    print("=" * 65)
    print("Database:", DB_PATH)

    report_rows = []

    connection = sqlite3.connect(DB_PATH)

    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")

        for file_path in source_files:

            if file_path.name not in EXPECTED_COUNTS:
                raise ValueError(
                    f"No expected count defined for {file_path.name}"
                )

            print(f"\nLoading: {file_path.name}")

            if file_path.suffix.lower() == ".csv":
                source_rows, table_name, table_rows = load_csv(
                    connection,
                    file_path,
                )
            else:
                source_rows, table_name, table_rows = load_json(
                    connection,
                    file_path,
                )

            expected_rows = EXPECTED_COUNTS[file_path.name]

            source_matches_table = source_rows == table_rows
            source_matches_expected = source_rows == expected_rows

            report_rows.append({
                "source_file": file_path.name,
                "table_name": table_name,
                "source_rows": source_rows,
                "table_rows": table_rows,
                "expected_rows_from_pdf": expected_rows,
                "source_matches_table": source_matches_table,
                "matches_pdf_count": source_matches_expected,
            })

            print("Table:", table_name)
            print(f"Source rows: {source_rows:,}")
            print(f"Database rows: {table_rows:,}")
            print("Source and table match:", source_matches_table)
            print("PDF expected count matches:", source_matches_expected)

        connection.commit()

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()

    report_df = pd.DataFrame(report_rows)
    report_df.to_csv(REPORT_PATH, index=False)

    failures = report_df[
        (~report_df["source_matches_table"])
        | (~report_df["matches_pdf_count"])
    ]

    print("\n" + "=" * 65)
    print("RAW INGESTION SUMMARY")
    print("=" * 65)

    print(
        report_df[
            [
                "source_file",
                "source_rows",
                "table_rows",
                "source_matches_table",
                "matches_pdf_count",
            ]
        ].to_string(index=False)
    )

    print("\nTotal files loaded:", len(report_df))
    print("Failed count checks:", len(failures))
    print("SQLite database saved to:", DB_PATH)
    print("Load audit saved to:", REPORT_PATH)


if __name__ == "__main__":
    main()
