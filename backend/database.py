"""
database.py
-----------
Engine + per-user schema helpers.

Changes vs. the old version:
- get_engine() used to call create_engine() on EVERY call, so every request
  built a brand-new connection pool. Under load that exhausts Postgres
  connections (a self-inflicted DoS). The engine is now created once and cached.
- Pool settings (pre_ping, recycle, timeout) so dead connections and pool
  exhaustion fail fast instead of hanging.
- Accepts postgres:// and postgresql:// URLs (Heroku/Render style).
- No LangChain SQLDatabase any more: agent.py builds its own compact schema
  and runs every query through a locked-down, per-user role.
"""

import os
from functools import lru_cache

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine


def _normalize_url(url: str | None) -> str:
    if not url:
        raise RuntimeError("DATABASE_URL is not set.")
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


@lru_cache(maxsize=4)
def _make_engine(url: str) -> Engine:
    return create_engine(
        url,
        pool_size=5,
        max_overflow=5,
        pool_timeout=10,      # fail fast instead of hanging when the pool is full
        pool_recycle=1800,
        pool_pre_ping=True,   # drop dead connections transparently
    )


def get_engine(database_url: str | None = None) -> Engine:
    """Shared, cached SQLAlchemy engine (one connection pool per process)."""
    return _make_engine(_normalize_url(database_url or os.environ.get("DATABASE_URL")))


def user_schema_name(user_id: int) -> str:
    """Deterministic per-user Postgres schema name, e.g. user_3."""
    return f"user_{int(user_id)}"


def ensure_user_schema(user_id: int) -> str:
    """Create the user's private schema if missing. Safe to call repeatedly.
    user_id is cast to int, so the f-string below can never carry injection."""
    schema = user_schema_name(user_id)
    with get_engine().begin() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
    return schema


if __name__ == "__main__":
    # Sanity check: python database.py
    eng = get_engine()
    with eng.connect() as conn:
        print("Connected:", conn.execute(text("SELECT version()")).scalar())
    print("Schemas:", [s for s in inspect(eng).get_schema_names() if s.startswith("user_")])