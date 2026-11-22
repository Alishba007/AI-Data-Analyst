"""
database.py
------------
Wraps a PostgreSQL connection as a LangChain SQLDatabase object.
This is what the agent "sees" — table names, schemas, sample rows — so it
can write correct SQL without you telling it the schema by hand.
"""

import os
from langchain_community.utilities import SQLDatabase
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

# Example: postgresql+psycopg2://user:password@localhost:5432/salesdb
DATABASE_URL = os.environ.get("DATABASE_URL")


def get_engine(database_url: str = DATABASE_URL) -> Engine:
    """
    Raw SQLAlchemy engine, separate from the LangChain SQLDatabase wrapper
    below. Needed for things LangChain doesn't do — e.g. pandas' to_sql()
    in file_processor.py takes a SQLAlchemy engine/connection, not a
    SQLDatabase object.
    """
    return create_engine(database_url)


# Tables the SQL agent must never see or query — mainly the users table,
# which holds password hashes. This was a real gap: before this fix, the
# agent could list and query the users table like any other table the
# moment auth.py created it, since get_database() exposed every table by
# default. Add any other sensitive/system tables here as the app grows.
SYSTEM_TABLES = ["users"]


def user_schema_name(user_id: int) -> str:
    """Deterministic per-user Postgres schema name, e.g. user_3."""
    return f"user_{user_id}"


def ensure_user_schema(user_id: int) -> str:
    """Creates the user's private schema if it doesn't exist. Call this
    once at signup (see auth.py) and it's safe to call again anytime."""
    schema = user_schema_name(user_id)
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
    return schema


def get_database(
    database_url: str = DATABASE_URL,
    schema: str | None = None,
    include_tables: list[str] | None = None,
    sample_rows_in_table_info: int = 3,
) -> SQLDatabase:
    """
    Create a LangChain SQLDatabase connection.

    schema:
        Pass a user's schema (see user_schema_name) to scope the agent's
        visibility to ONLY that user's tables — this is the actual
        privacy boundary between users. Omitting it exposes every schema,
        which is only appropriate for the shared "public" fallback or
        admin/debugging use, never for a real per-user request.
    include_tables:
        Further restrict within a schema if needed. Leave as None to
        expose every table in `schema` except SYSTEM_TABLES.
    sample_rows_in_table_info:
        How many sample rows of each table to show the LLM. This is what
        lets it guess things like date formats or category spellings
        without querying first. 3 is a good default; more = more tokens.
    """
    engine = get_engine(database_url)
    # ignore_tables is only relevant (and only valid) when NOT scoped to a
    # user schema — SQLDatabase errors if a table named in ignore_tables
    # doesn't exist in the target schema, and a user's schema will never
    # contain "users" (that table lives in the public schema, not here).
    ignore = SYSTEM_TABLES if schema is None else None
    return SQLDatabase(
        engine,
        schema=schema,
        include_tables=include_tables,
        ignore_tables=ignore,
        sample_rows_in_table_info=sample_rows_in_table_info,
    )


if __name__ == "__main__":
    # Quick sanity check: run `python database.py` to confirm the
    # connection works and see what schema the agent will be given.
    db = get_database()
    print("Dialect:", db.dialect)
    print("Usable tables:", db.get_usable_table_names())
    print("\n--- Table info the LLM will see ---\n")
    print(db.get_table_info())