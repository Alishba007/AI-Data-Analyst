"""
auth.py
-------
Minimal but real authentication: bcrypt password hashing + JWT bearer
tokens. Users table lives in the same Postgres database as everything
else. This is deliberately simple (no refresh tokens, no email
verification, no password reset flow) — enough for a portfolio project
demonstrating real auth, not a production-grade identity system.
"""

import os
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text

from database import get_engine, ensure_user_schema

# In production, set JWT_SECRET to a long random value via environment
# variable — never rely on this fallback outside local development.
JWT_SECRET = os.environ.get("JWT_SECRET")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM")
JWT_EXPIRY_HOURS = int(os.getenv("JWT_EXPIRY_HOURS"))
security = HTTPBearer()


def ensure_users_table():
    """
    Creates the users table if it doesn't exist yet. Called once at
    startup (see main.py) — simpler than a full migration tool for a
    project this size, but means schema changes later need a manual
    ALTER TABLE, there's no migration history.
    """
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT now()
            )
        """))


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def create_token(user_id: int, email: str) -> str:
    payload = {
        "sub": str(user_id),
        "email": email,
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRY_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def create_user(email: str, password: str) -> dict:
    """Raises ValueError if the email is already registered."""
    engine = get_engine()
    with engine.begin() as conn:
        existing = conn.execute(
            text("SELECT id FROM users WHERE email = :email"), {"email": email}
        ).fetchone()
        if existing:
            raise ValueError("An account with this email already exists.")

        result = conn.execute(
            text("""
                INSERT INTO users (email, password_hash)
                VALUES (:email, :password_hash)
                RETURNING id, email
            """),
            {"email": email, "password_hash": hash_password(password)},
        )
        row = result.fetchone()
        user = {"id": row[0], "email": row[1]}

    # Every user gets their own Postgres schema — this is the actual
    # privacy boundary: uploaded data goes into user_<id>, and the agent
    # is scoped to see only that schema (see database.get_database).
    ensure_user_schema(user["id"])
    return user


def authenticate_user(email: str, password: str) -> dict:
    """Raises ValueError with a generic message on any failure — deliberately
    not distinguishing "no such user" from "wrong password" in the error,
    since that distinction is exactly what lets an attacker enumerate
    which emails have accounts."""
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT id, email, password_hash FROM users WHERE email = :email"),
            {"email": email},
        ).fetchone()

    if not row or not verify_password(password, row[2]):
        raise ValueError("Incorrect email or password.")

    return {"id": row[0], "email": row[1]}


def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    """
    FastAPI dependency — add `user: dict = Depends(get_current_user)` to
    any route to require a valid JWT. Raises 401 on anything invalid
    (missing, expired, tampered, or malformed token).
    """
    token = credentials.credentials
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired, please log in again.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication token.")

    return {"id": int(payload["sub"]), "email": payload["email"]}