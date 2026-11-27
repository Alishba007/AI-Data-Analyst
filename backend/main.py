"""
main.py
-------
FastAPI wrapper around the SQL agent, hardened against the OWASP API Top 10.

Run:
    ENV=production uvicorn main:app --host 0.0.0.0 --port 8000

Env vars:
    ENV              production | development (docs/openapi are disabled in production)
    CORS_ORIGINS     comma-separated allowed frontend origins (never "*")
    TRUST_PROXY      1 if behind ONE reverse proxy (Render, etc.) so client IP is read
                     from X-Forwarded-For
    JWT_SECRET       >= 32 random chars (checked here; used by auth.py)
"""

import logging
import os
import re
import shutil
import tempfile
import threading
import time
import zipfile
from collections import defaultdict, deque
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field

from agent import (
    LLMBusyError,
    SessionStore,
    TTLCache,
    UserSession,
    ask,
    build_session,
)
from auth import (authenticate_user, create_token, create_user,
                  ensure_users_table, get_current_user, validate_password)
from chart_generator import generate_chart, suggest_title
from database import get_engine, user_schema_name
from file_processor import process_upload
from insights import generate_insights_fast

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("api")

IS_PROD = os.environ.get("ENV", "development").lower() == "production"
TRUST_PROXY = os.environ.get("TRUST_PROXY", "0") == "1"

MAX_UPLOAD_BYTES = 10 * 1024 * 1024        # 10 MB
MAX_UNZIPPED_BYTES = 100 * 1024 * 1024     # zip-bomb guard for .xlsx
MAX_JSON_BODY_BYTES = 100 * 1024
ALLOWED_EXT = {".csv", ".xlsx", ".xls"}


# =============================================================================
# App setup
# =============================================================================
@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_users_table()
    secret = os.environ.get("JWT_SECRET", "")
    if IS_PROD and len(secret) < 32:
        # adjust the env var name if auth.py reads a different one
        logger.warning("JWT_SECRET is missing or shorter than 32 chars!")
    yield


app = FastAPI(
    title="AI Data Analyst",
    lifespan=lifespan,
    docs_url=None if IS_PROD else "/docs",
    redoc_url=None if IS_PROD else "/redoc",
    openapi_url=None if IS_PROD else "/openapi.json",
)

origins = [o.strip() for o in os.environ.get(
    "CORS_ORIGINS",
    "https://ai-data-analyst-two-tau.vercel.app,"
    "https://ai-data-analyst-fvjau0xnm-alishba-raza.vercel.app",
).split(",") if o.strip()]

# Token is sent in the Authorization header, so cookies/credentials are not needed.
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    # Reject oversized bodies early (uploads get their own, larger limit + streaming check)
    try:
        length = int(request.headers.get("content-length", 0))
    except ValueError:
        return JSONResponse({"detail": "Bad request."}, status_code=400)
    limit = MAX_UPLOAD_BYTES + 1024 * 1024 if request.url.path == "/upload" else MAX_JSON_BODY_BYTES
    if length > limit:
        return JSONResponse({"detail": "Request too large."}, status_code=413)

    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    if IS_PROD:
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    return response


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse({"detail": "Internal server error."}, status_code=500)


# =============================================================================
# Rate limiting (in-process sliding window; use Redis if you run >1 worker)
# =============================================================================
class SlidingWindowLimiter:
    def __init__(self, max_calls: int, window_s: int):
        self.max_calls, self.window = max_calls, window_s
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def check(self, key: str):
        now = time.monotonic()
        with self.lock:
            dq = self.hits[key]
            while dq and now - dq[0] > self.window:
                dq.popleft()
            if len(dq) >= self.max_calls:
                raise HTTPException(
                    status_code=429,
                    detail="Too many requests. Please slow down and try again shortly.",
                    headers={"Retry-After": str(self.window)},
                )
            dq.append(now)
            if len(self.hits) > 10_000:  # bound memory
                for k in [k for k, v in self.hits.items() if not v or now - v[-1] > self.window]:
                    del self.hits[k]


LOGIN_IP = SlidingWindowLimiter(10, 60)
LOGIN_EMAIL = SlidingWindowLimiter(8, 900)      # brute-force lockout per account
SIGNUP_IP = SlidingWindowLimiter(5, 3600)
ASK_USER = SlidingWindowLimiter(10, 60)
INSIGHTS_USER = SlidingWindowLimiter(3, 60)
UPLOAD_USER = SlidingWindowLimiter(5, 60)


def _client_ip(request: Request) -> str:
    if TRUST_PROXY:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[-1].strip()   # rightmost = added by our own proxy
    return request.client.host if request.client else "unknown"


def user_limit(limiter: SlidingWindowLimiter):
    def dep(user: dict = Depends(get_current_user)) -> dict:
        limiter.check(f"u{user['id']}")
        return user
    return dep


# =============================================================================
# Per-user sessions (schema text + 3-turn memory), LRU bounded
# =============================================================================
SESSIONS = SessionStore()
_INSIGHTS_CACHE = TTLCache(max_items=128, ttl=1800)


def _refresh_session(user_id: int) -> UserSession:
    """Rebuild after an upload: new table, new grants, fresh (empty) memory."""
    session = build_session(get_engine(), user_id)
    SESSIONS.put(session)
    return session


def _get_session(user_id: int) -> UserSession:
    return SESSIONS.get(user_id) or _refresh_session(user_id)


# =============================================================================
# Schemas
# =============================================================================
class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(max_length=200)




class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(max_length=128)


class AuthResponse(BaseModel):
    token: str
    email: str


class QuestionRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)


class AnswerResponse(BaseModel):
    answer: str
    sql_queries: list[str]
    table: list[dict] | None = None
    chart: dict | None = None


class UploadResponse(BaseModel):
    table_name: str
    row_count: int
    columns: list[str]


class InsightsRequest(BaseModel):
    table_name: str = Field(min_length=1, max_length=200)


class InsightsResponse(BaseModel):
    summary: str
    sql_queries: list[str]
    charts: list[dict]


# =============================================================================
# Health
# =============================================================================
@app.get("/healthz")
def healthz():
    """Public liveness check for the host's health probe."""
    return {"status": "ok"}


@app.get("/health")
def health(user: dict = Depends(get_current_user)):
    return {"status": "ok", "tables": _get_session(user["id"]).tables}


# =============================================================================
# Auth
# =============================================================================
@app.post("/auth/signup", response_model=AuthResponse)
def signup(req: SignupRequest, request: Request):
    SIGNUP_IP.check(f"ip:{_client_ip(request)}")
    if len(req.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
    try:
        user = create_user(req.email, req.password)
    except ValueError as e:
        logger.info("signup rejected: %s", e)
        # generic message: don't reveal whether an email is already registered
        raise HTTPException(status_code=409, detail="Unable to create an account with those details.")
    token = create_token(user["id"], user["email"])
    return AuthResponse(token=token, email=user["email"])


@app.post("/auth/login", response_model=AuthResponse)
def login(req: LoginRequest, request: Request):
    LOGIN_IP.check(f"ip:{_client_ip(request)}")
    LOGIN_EMAIL.check(f"email:{req.email.lower()}")
    try:
        user = authenticate_user(req.email, req.password)
    except ValueError:
        logger.info("failed login for %s from %s", req.email, _client_ip(request))
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    token = create_token(user["id"], user["email"])
    return AuthResponse(token=token, email=user["email"])


# =============================================================================
# Upload
# =============================================================================
def _validate_file_content(path: str, ext: str):
    """Don't trust the extension: check magic bytes and guard against zip bombs."""
    with open(path, "rb") as f:
        head = f.read(8192)
    if not head:
        raise ValueError("The file is empty.")
    if ext == ".xlsx":
        if not head.startswith(b"PK\x03\x04"):
            raise ValueError("The file is not a valid .xlsx workbook.")
        try:
            with zipfile.ZipFile(path) as z:
                infos = z.infolist()
                if len(infos) > 2000:
                    raise ValueError("The workbook has too many parts.")
                if sum(i.file_size for i in infos) > MAX_UNZIPPED_BYTES:
                    raise ValueError("The workbook is too large once decompressed.")
                if any(i.compress_size and i.file_size / i.compress_size > 200 for i in infos):
                    raise ValueError("The workbook looks malformed.")
        except zipfile.BadZipFile:
            raise ValueError("The file is not a valid .xlsx workbook.")
    elif ext == ".xls":
        if not head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            raise ValueError("The file is not a valid .xls workbook.")
    else:  # .csv
        if b"\x00" in head:
            raise ValueError("The file does not look like a CSV text file.")


@app.post("/upload", response_model=UploadResponse)
def upload_file(
    file: UploadFile = File(...),
    user: dict = Depends(user_limit(UPLOAD_USER)),
):
    name = os.path.basename(file.filename or "")
    ext = os.path.splitext(name)[1].lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(status_code=400, detail="Only .csv, .xlsx and .xls files are supported.")
    safe_name = re.sub(r"[^\w.\- ]", "_", name)[:100]

    # Stream to a private temp file (never named after user input), enforcing the size cap.
    written = 0
    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
        tmp_path = tmp.name
        while chunk := file.file.read(1024 * 1024):
            written += len(chunk)
            if written > MAX_UPLOAD_BYTES:
                tmp.close()
                os.remove(tmp_path)
                raise HTTPException(status_code=413, detail="File too large (max 10 MB).")
            tmp.write(chunk)

    try:
        _validate_file_content(tmp_path, ext)
        # schema scopes the upload to THIS user's private namespace
        result = process_upload(tmp_path, safe_name, schema=user_schema_name(user["id"]))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logger.exception("upload failed for user %s", user["id"])
        raise HTTPException(status_code=500, detail="Upload failed. Please check the file and try again.")
    finally:
        os.remove(tmp_path)

    _refresh_session(user["id"])  # new table + fresh role grants + schema text
    return UploadResponse(**result)


# =============================================================================
# Insights
# =============================================================================
@app.post("/insights", response_model=InsightsResponse)
def insights(req: InsightsRequest, user: dict = Depends(user_limit(INSIGHTS_USER))):
    session = _get_session(user["id"])
    if req.table_name not in session.tables:
        raise HTTPException(status_code=404, detail="Table not found.")

    cache_key = (user["id"], session.version, req.table_name)
    cached = _INSIGHTS_CACHE.get(cache_key)
    if cached:
        return cached

    try:
        result = generate_insights_fast(
            req.table_name,
            engine=get_engine(),
            user_id=user["id"],
            schema=session.schema,
        )
    except Exception:
        logger.exception("insights failed for user %s", user["id"])
        raise HTTPException(status_code=500, detail="Couldn't generate insights for that table.")

    charts = []
    for rows in result.datasets:
        try:
            chart = generate_chart(rows, title=suggest_title(rows))
        except Exception:
            logger.exception("chart generation failed")
            continue
        if chart:
            charts.append(chart)

    response = InsightsResponse(summary=result.summary, sql_queries=result.sql_queries, charts=charts)
    _INSIGHTS_CACHE.set(cache_key, response)
    return response


# =============================================================================
# Ask
# =============================================================================
@app.post("/ask", response_model=AnswerResponse)
def ask_question(req: QuestionRequest, user: dict = Depends(user_limit(ASK_USER))):
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty.")

    session = _get_session(user["id"])
    try:
        result = ask(req.question, session, get_engine())
    except LLMBusyError:
        raise HTTPException(
            status_code=429,
            detail="The AI service is busy. Please try again in a few seconds.",
            headers={"Retry-After": "10"},
        )
    except Exception:
        logger.exception("ask failed for user %s", user["id"])
        raise HTTPException(status_code=500, detail="Something went wrong answering that question.")

    chart = None
    if result.rows:
        try:
            chart = generate_chart(result.rows, title=req.question)
        except Exception:
            logger.exception("chart generation failed")

    return AnswerResponse(
        answer=result.answer,
        sql_queries=result.sql_queries,
        table=result.rows,
        chart=chart,
    )