"""
auth.py
-------
bcrypt password hashing + JWT bearer tokens.

Hardening vs. the old version:
- JWT algorithm is fixed to HS256 in code (never read from env -> no
  algorithm-confusion), and the app refuses to start without a strong secret.
  Previously a missing JWT_ALGORITHM / JWT_EXPIRY_HOURS crashed at import.
- Tokens carry iss/aud/iat/exp, and all of them are required on decode.
- Emails are normalized (lower-case) with a unique index on lower(email),
  so "A@x.com" and "a@x.com" can't become two accounts.
- Constant-ish time login: a dummy bcrypt check runs when the email doesn't
  exist, so response time doesn't reveal which emails are registered.
- bcrypt only uses the first 72 bytes (and bcrypt>=5 raises on longer input):
  passwords over 72 bytes are rejected explicitly instead of crashing.
- Signup is race-safe (unique constraint + IntegrityError, no check-then-insert).
- A deleted user's token stops working (existence check, cached 60 s).
"""

import os
import threading
import time
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from database import ensure_user_schema, get_engine

# ---------------------------------------------------------------------------
# Config (fail closed)
# ---------------------------------------------------------------------------
JWT_SECRET = os.environ.get("JWT_SECRET", "")
if len(JWT_SECRET) < 32:
    raise RuntimeError(
        "JWT_SECRET must be set to at least 32 random characters. "
        "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(48))\""
    )

JWT_ALGORITHM = "HS256"                      # fixed in code on purpose
JWT_ISSUER = "ai-data-analyst"
JWT_AUDIENCE = "ai-data-analyst-web"
JWT_EXPIRY_HOURS = min(max(int(os.environ.get("JWT_EXPIRY_HOURS", "4")), 1), 24)

MIN_PASSWORD_CHARS = 8
MAX_PASSWORD_BYTES = 72                      # bcrypt limit
BCRYPT_ROUNDS = 12

bearer = HTTPBearer(auto_error=False)
_DUMMY_HASH = bcrypt.hashpw(b"not-a-real-password", bcrypt.gensalt(BCRYPT_ROUNDS)).decode("utf-8")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def normalize_email(email: str) -> str:
    return email.strip().lower()


def validate_password(password: str):
    """Raises ValueError with a user-safe message. Call before create_user."""
    if len(password) < MIN_PASSWORD_CHARS:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_CHARS} characters.")
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise ValueError(f"Password is too long (max {MAX_PASSWORD_BYTES} bytes).")


def hash_password(password: str) -> str:
    validate_password(password)
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(BCRYPT_ROUNDS)).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    raw = password.encode("utf-8")
    if len(raw) > MAX_PASSWORD_BYTES:
        return False
    try:
        return bcrypt.checkpw(raw, password_hash.encode("utf-8"))
    except ValueError:
        return False


def ensure_users_table():
    """Create the users table and a case-insensitive unique index on email."""
    with get_engine().begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT now()
            )
        """))
    try:
        with get_engine().begin() as conn:
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS users_email_lower_idx ON users (lower(email))"))
    except Exception:  # pre-existing mixed-case duplicates: fix the data, then restart
        import logging
        logging.getLogger("auth").warning(
            "Could not create the lower(email) unique index; check for duplicate emails.")


def create_token(user_id: int, email: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "email": email,
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "iat": now,
        "exp": now + timedelta(hours=JWT_EXPIRY_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
def create_user(email: str, password: str) -> dict:
    """Raises ValueError if the password is invalid or the email is taken."""
    email = normalize_email(email)
    pw_hash = hash_password(password)  # validates length too
    try:
        with get_engine().begin() as conn:
            row = conn.execute(
                text("""INSERT INTO users (email, password_hash)
                        VALUES (:email, :ph) RETURNING id, email"""),
                {"email": email, "ph": pw_hash},
            ).fetchone()
    except IntegrityError:
        raise ValueError("An account with this email already exists.")

    user = {"id": row[0], "email": row[1]}
    ensure_user_schema(user["id"])
    return user


def authenticate_user(email: str, password: str) -> dict:
    """Generic failure, and the same amount of bcrypt work whether or not the
    email exists, so neither the message nor the timing reveals which it was."""
    email = normalize_email(email)
    with get_engine().connect() as conn:
        row = conn.execute(
            text("SELECT id, email, password_hash FROM users WHERE lower(email) = :email"),
            {"email": email},
        ).fetchone()

    ok = verify_password(password, row[2] if row else _DUMMY_HASH)
    if not row or not ok:
        raise ValueError("Incorrect email or password.")
    return {"id": row[0], "email": row[1]}


# ---------------------------------------------------------------------------
# Request authentication
# ---------------------------------------------------------------------------
_exists_cache: dict[int, float] = {}
_exists_lock = threading.Lock()
_EXISTS_TTL = 60


def _user_exists(user_id: int) -> bool:
    now = time.monotonic()
    with _exists_lock:
        ts = _exists_cache.get(user_id)
        if ts and now - ts < _EXISTS_TTL:
            return True
    with get_engine().connect() as conn:
        found = conn.execute(text("SELECT 1 FROM users WHERE id = :id"), {"id": user_id}).fetchone()
    if found:
        with _exists_lock:
            if len(_exists_cache) > 5000:
                _exists_cache.clear()
            _exists_cache[user_id] = now
        return True
    return False


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> dict:
    """Dependency: add `user: dict = Depends(get_current_user)` to require a valid JWT."""
    if credentials is None:
        raise _unauthorized("Not authenticated.")
    try:
        payload = jwt.decode(
            credentials.credentials,
            JWT_SECRET,
            algorithms=[JWT_ALGORITHM],
            audience=JWT_AUDIENCE,
            issuer=JWT_ISSUER,
            options={"require": ["exp", "iat", "sub", "iss", "aud"]},
            leeway=10,
        )
    except jwt.ExpiredSignatureError:
        raise _unauthorized("Session expired, please log in again.")
    except jwt.InvalidTokenError:
        raise _unauthorized("Invalid authentication token.")

    try:
        user_id = int(payload["sub"])
    except (KeyError, TypeError, ValueError):
        raise _unauthorized("Invalid authentication token.")

    if not _user_exists(user_id):
        raise _unauthorized("Invalid authentication token.")
    return {"id": user_id, "email": payload.get("email", "")}