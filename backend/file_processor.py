"""
file_processor.py
------------------
Handles CSV/Excel uploads: reads the file, cleans up column names, lets
pandas infer types, and loads it into Postgres as a real table — so the
SQL agent can query an uploaded file exactly the same way it queries any
other table. No separate "file mode" logic needed anywhere else.
"""

import os
import re
import uuid

import pandas as pd
from sqlalchemy import inspect

from database import get_engine

# Uploaded tables live in their own schema-ish prefix so they're easy to
# spot and don't collide with real business tables (e.g. an uploaded
# "sales.csv" won't silently overwrite a "sales" table already in the DB).
UPLOAD_TABLE_PREFIX = "uploaded_"


def _sanitize_table_name(filename: str) -> str:
    """
    Turn 'Q3 Sales Report (final).xlsx' into something Postgres accepts as
    an identifier: 'uploaded_q3_sales_report_final'. A short random suffix
    is appended so two uploads with the same filename don't collide.
    """
    stem = os.path.splitext(filename)[0]
    stem = stem.lower().strip()
    stem = re.sub(r"[^a-z0-9]+", "_", stem).strip("_")
    if not stem:
        stem = "table"
    # Postgres identifiers max out at 63 bytes — leave room for prefix/suffix.
    stem = stem[:40]
    suffix = uuid.uuid4().hex[:6]
    return f"{UPLOAD_TABLE_PREFIX}{stem}_{suffix}"


def _sanitize_column_names(df: pd.DataFrame) -> pd.DataFrame:
    """
    'Order Date', 'Unit Price ($)' etc. become 'order_date', 'unit_price'.
    The agent writes SQL using these names, so they need to be predictable
    snake_case identifiers, not whatever a spreadsheet author typed.
    """
    new_columns = []
    for col in df.columns:
        clean = str(col).strip().lower()
        clean = re.sub(r"[^a-z0-9]+", "_", clean).strip("_")
        new_columns.append(clean or "unnamed_column")
    df.columns = new_columns
    return df


def read_upload(file_path: str, filename: str) -> pd.DataFrame:
    """
    Read a CSV or Excel file into a DataFrame.

    CSV files may use different encodings, so UTF-8 is tried first,
    followed by common encodings used by Windows/Excel-generated files.
    """
    ext = os.path.splitext(filename)[1].lower()

    if ext == ".csv":
        encodings = [
            "utf-8",
            "utf-8-sig",
            "cp1252",
            "latin1",
        ]

        df = None
        last_error = None

        for encoding in encodings:
            try:
                df = pd.read_csv(
                    file_path,
                    encoding=encoding
                )
                break
            except UnicodeDecodeError as e:
                last_error = e

        if df is None:
            raise ValueError(
                "Could not decode the CSV file. "
                "Please save the CSV as UTF-8 and try again."
            ) from last_error

    elif ext in (".xlsx", ".xls"):
        df = pd.read_excel(file_path)

    else:
        raise ValueError(
            f"Unsupported file type '{ext}'. "
            "Only .csv, .xlsx, and .xls are supported."
        )

    if df.empty:
        raise ValueError("The uploaded file has no rows.")

    return df


def load_dataframe_to_db(df: pd.DataFrame, table_name: str, schema: str | None = None) -> dict:
    """
    Write a DataFrame into Postgres as a new table. Returns a small summary
    dict describing what was loaded, so the API layer can tell the user
    (and so the agent's first response can reference it directly).

    schema:
        Pass the uploading user's schema (see database.ensure_user_schema)
        so their data lands in their own private namespace, not the
        shared public schema every user could otherwise see.
    """
    engine = get_engine()

    df.to_sql(table_name, engine, schema=schema, if_exists="replace", index=False)

    inspector = inspect(engine)
    columns = [col["name"] for col in inspector.get_columns(table_name, schema=schema)]

    return {
        "table_name": table_name,
        "row_count": len(df),
        "columns": columns,
    }


def process_upload(file_path: str, filename: str, schema: str | None = None) -> dict:
    """
    Full pipeline: read the file, clean it up, load it into Postgres.
    This is the single function main.py's upload endpoint should call.

    schema:
        The uploading user's private schema — always pass this in a
        multi-user context (see main.py). Omitting it writes to the
        shared public schema, visible to every user.

    Returns:
        {
            "table_name": "uploaded_q3_sales_report_final_a1b2c3",
            "row_count": 1204,
            "columns": ["order_date", "region", "revenue", ...],
        }
    """
    df = read_upload(file_path, filename)
    df = _sanitize_column_names(df)
    table_name = _sanitize_table_name(filename)
    return load_dataframe_to_db(df, table_name, schema=schema)


if __name__ == "__main__":
    # Manual smoke test:
    #   python file_processor.py path/to/some_file.csv
    import sys

    if len(sys.argv) != 2:
        print("Usage: python file_processor.py <path_to_csv_or_xlsx>")
        sys.exit(1)

    path = sys.argv[1]
    result = process_upload(path, os.path.basename(path))
    print("Loaded into table:", result["table_name"])
    print("Rows:", result["row_count"])
    print("Columns:", result["columns"])