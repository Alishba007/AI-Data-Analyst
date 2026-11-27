"""
file_processor.py
-----------------
CSV/Excel upload -> clean DataFrame -> Postgres table in the user's own schema.

Hardening vs. the old version:
- Row / column caps (read with nrows so a huge file can't eat all RAM).
- Per-user table quota (unrestricted resource consumption).
- `schema` is REQUIRED: forgetting it can no longer drop data into the shared
  public schema.
- Column names are de-duplicated (two headers that sanitize to the same name
  used to crash to_sql) and truncated to Postgres' 63-byte limit.
- Sanitized column names are [a-z0-9_] only, so a malicious CSV header cannot
  carry prompt-injection text into the LLM prompt.
- Parser errors become generic ValueErrors (no internals leaked to the client).
- Columns named *date* are parsed to real timestamps when >=90% of values parse.
"""

import logging
import os
import re
import uuid
import warnings

import pandas as pd
from sqlalchemy import inspect

from database import get_engine

logger = logging.getLogger("file_processor")

UPLOAD_TABLE_PREFIX = "uploaded_"
MAX_ROWS = 200_000
MAX_COLS = 100
MAX_TABLES_PER_USER = 20


def _sanitize_table_name(filename: str) -> str:
    stem = os.path.splitext(filename)[0].lower().strip()
    stem = re.sub(r"[^a-z0-9]+", "_", stem).strip("_") or "table"
    stem = stem[:40]  # 63-byte identifier limit: leave room for prefix + suffix
    return f"{UPLOAD_TABLE_PREFIX}{stem}_{uuid.uuid4().hex[:6]}"


def _sanitize_column_names(df: pd.DataFrame) -> pd.DataFrame:
    """'Order Date' -> order_date. Duplicates become order_date, order_date_2, ..."""
    seen: dict[str, int] = {}
    cols = []
    for col in df.columns:
        clean = re.sub(r"[^a-z0-9]+", "_", str(col).strip().lower()).strip("_")[:50] or "unnamed_column"
        n = seen.get(clean, 0) + 1
        seen[clean] = n
        cols.append(clean if n == 1 else f"{clean}_{n}")
    df.columns = cols
    return df


def _parse_date_columns(df: pd.DataFrame) -> pd.DataFrame:
    for c in df.columns:
        if "date" in c and df[c].dtype == object and df[c].notna().any():
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                parsed = pd.to_datetime(df[c], errors="coerce")
            if parsed.notna().sum() >= 0.9 * df[c].notna().sum():
                df[c] = parsed
    return df


def read_upload(file_path: str, filename: str) -> pd.DataFrame:
    ext = os.path.splitext(filename)[1].lower()
    limit = MAX_ROWS + 1  # read one extra row so we can detect "too many"

    if ext == ".csv":
        df, last_err = None, None
        for enc in ("utf-8-sig", "cp1252", "latin1"):   # utf-8-sig also reads plain utf-8
            try:
                df = pd.read_csv(file_path, encoding=enc, nrows=limit)
                break
            except UnicodeDecodeError as e:
                last_err = e
            except (pd.errors.ParserError, pd.errors.EmptyDataError) as e:
                logger.info("csv parse error: %s", e)
                raise ValueError("The CSV file could not be parsed. Check that it is a valid CSV.")
        if df is None:
            raise ValueError("Could not decode the CSV file. Please save it as UTF-8 and try again.") from last_err
    elif ext in (".xlsx", ".xls"):
        try:
            df = pd.read_excel(file_path, nrows=limit)
        except Exception:
            logger.exception("excel read failed")
            raise ValueError("The spreadsheet could not be read. Check that it is a valid Excel file.")
    else:
        raise ValueError("Unsupported file type. Only .csv, .xlsx and .xls are supported.")

    if df.empty:
        raise ValueError("The uploaded file has no rows.")
    if len(df) > MAX_ROWS:
        raise ValueError(f"The file has too many rows (max {MAX_ROWS:,}).")
    if df.shape[1] > MAX_COLS:
        raise ValueError(f"The file has too many columns (max {MAX_COLS}).")
    return df


def load_dataframe_to_db(df: pd.DataFrame, table_name: str, schema: str) -> dict:
    engine = get_engine()
    df.to_sql(table_name, engine, schema=schema, if_exists="fail", index=False, chunksize=5000)
    columns = [c["name"] for c in inspect(engine).get_columns(table_name, schema=schema)]
    return {"table_name": table_name, "row_count": len(df), "columns": columns}


def process_upload(file_path: str, filename: str, schema: str) -> dict:
    """
    read -> clean -> load. `schema` is mandatory (the user's private schema).
    Returns {"table_name", "row_count", "columns"}.
    """
    if not schema:
        raise ValueError("A target schema is required.")

    existing = inspect(get_engine()).get_table_names(schema=schema)
    if len(existing) >= MAX_TABLES_PER_USER:
        raise ValueError(f"Dataset limit reached ({MAX_TABLES_PER_USER}). Remove an old dataset first.")

    df = read_upload(file_path, filename)
    df = _sanitize_column_names(df)
    df = _parse_date_columns(df)
    return load_dataframe_to_db(df, _sanitize_table_name(filename), schema=schema)


if __name__ == "__main__":
    # Manual smoke test: python file_processor.py <path> <user_id>
    import sys
    from database import ensure_user_schema

    if len(sys.argv) != 3:
        print("Usage: python file_processor.py <path_to_csv_or_xlsx> <user_id>")
        sys.exit(1)
    sch = ensure_user_schema(int(sys.argv[2]))
    res = process_upload(sys.argv[1], os.path.basename(sys.argv[1]), schema=sch)
    print(res)