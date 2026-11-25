"""
main.py
-------
Minimal FastAPI wrapper around the SQL agent so you can test it over HTTP
today, before the React frontend exists.

Run:
    uvicorn main:app --reload

Test:
    curl -X POST http://localhost:8000/ask \
        -H "Content-Type: application/json" \
        -d '{"question": "What are the top 5 products by revenue?"}'
"""

import os
import shutil
import tempfile
import traceback
from fastapi import Depends, FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr

from agent import build_sql_agent, ask
from insights import generate_insights_fast
from auth import (
    authenticate_user,
    create_token,
    create_user,
    ensure_users_table,
    get_current_user,
)
from chart_generator import generate_chart, suggest_title
from database import ensure_user_schema, get_database, user_schema_name
from file_processor import process_upload

app = FastAPI(title="AI Data Analyst")
origins = os.environ.get("CORS_ORIGINS", "http://localhost:5173").split(",")
# Allow the frontend (Vite dev server, or wherever it's deployed) to call
# this API directly. Wide open for local dev — tighten allow_origins to
# your actual deployed frontend URL before shipping this to production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Per-user cache of {db, agent} — this is the actual privacy boundary.
# Each user's SQLDatabase is scoped to their own Postgres schema (see
# database.get_database's schema param), so one user's agent can never
# see another user's tables.
#
# Caching per user (not rebuilding on every request) matters for a
# second reason: the agent's MemorySaver checkpointer lives on the agent
# object itself. A fresh agent = fresh (empty) memory. Rebuilding per
# request would silently break conversation history on every single
# message. Keyed by user_id, so refreshing one user's agent (after their
# upload) never touches anyone else's.
_user_agents: dict[int, dict] = {}


def _get_user_agent(user_id: int) -> dict:
    if user_id not in _user_agents:
        _refresh_user_agent(user_id)
    return _user_agents[user_id]


def _refresh_user_agent(user_id: int) -> dict:
    """
    (Re)build the SQLDatabase + agent for one user, scoped to their schema.
    Call this after that user uploads a file, so their new table is
    visible — SQLDatabase snapshots the schema at creation time and won't
    pick up a table created after that point on its own.

    Side effect: this also resets a fresh MemorySaver, wiping that user's
    own conversation history (not anyone else's — that's the improvement
    over the old global-agent design, where one upload wiped everyone's
    memory). A new dataset makes prior conversation context stale anyway,
    so this is an intentional tradeoff, not an oversight.
    """
    schema = ensure_user_schema(user_id)
    db = get_database(schema=schema)
    agent = build_sql_agent(db=db)
    _user_agents[user_id] = {"db": db, "agent": agent}
    return _user_agents[user_id]


@app.on_event("startup")
def startup():
    ensure_users_table()


class SignupRequest(BaseModel):
    email: EmailStr
    password: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class AuthResponse(BaseModel):
    token: str
    email: str


class QuestionRequest(BaseModel):
    question: str


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
    table_name: str


class InsightsResponse(BaseModel):
    summary: str
    sql_queries: list[str]
    charts: list[dict]


@app.get("/healthz")
def healthz():
    """
    Public liveness check — no auth required. This is what Render (or any
    host) should point its health check at, since /health below requires
    a valid user token and would make the deploy platform think a
    perfectly healthy service is broken (401 looks like "down" to most
    health checkers, not "up but you're not logged in").
    """
    return {"status": "ok"}


@app.get("/health")
def health(user: dict = Depends(get_current_user)):
    db = _get_user_agent(user["id"])["db"]
    return {"status": "ok", "tables": db.get_usable_table_names()}


@app.post("/auth/signup", response_model=AuthResponse)
def signup(req: SignupRequest):
    if len(req.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
    try:
        user = create_user(req.email, req.password)  # also creates their schema
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    token = create_token(user["id"], user["email"])
    return AuthResponse(token=token, email=user["email"])


@app.post("/auth/login", response_model=AuthResponse)
def login(req: LoginRequest):
    try:
        user = authenticate_user(req.email, req.password)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))
    token = create_token(user["id"], user["email"])
    return AuthResponse(token=token, email=user["email"])


@app.post("/upload", response_model=UploadResponse)
def upload_file(file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    # UploadFile gives us a stream, but pandas needs a real path — write
    # it to a temp file first, using the original extension so
    # file_processor can detect CSV vs Excel correctly.
    suffix = "." + file.filename.rsplit(".", 1)[-1] if "." in file.filename else ""
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name

    try:
        # schema scopes the upload to THIS user's private namespace —
        # dropping this argument would put the data back in the shared
        # public schema, visible to every user.
        schema = user_schema_name(user["id"])
        result = process_upload(tmp_path, file.filename, schema=schema)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        # Catches things like a missing pandas engine (openpyxl for .xlsx),
        # a DB connection drop mid-upload, etc. Without this, FastAPI
        # returns a bare 500 with no detail, and the frontend just shows
        # a generic "upload failed" with no way to tell what broke.
        raise HTTPException(status_code=500, detail=f"Upload failed: {e}")
    finally:
        os.remove(tmp_path)

    _refresh_user_agent(user["id"])  # new table now exists — make this user's agent aware of it

    return UploadResponse(**result)


@app.post("/insights", response_model=InsightsResponse)
def insights(req: InsightsRequest, user: dict = Depends(get_current_user)):
    user_agent = _get_user_agent(user["id"])
    db = user_agent["db"]

    if req.table_name not in db.get_usable_table_names():
        raise HTTPException(status_code=404, detail=f"Table '{req.table_name}' not found.")

    try:
        result = generate_insights_fast(
            req.table_name,
            engine=db._engine,
            schema=user_schema_name(user["id"]),
        )
    except Exception:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Couldn't generate insights for that table.")

    charts = []
    for rows in result.datasets:
        chart = generate_chart(rows, title=suggest_title(rows))
        if chart:
            charts.append(chart)

    return InsightsResponse(summary=result.summary, sql_queries=result.sql_queries, charts=charts)

@app.post("/ask", response_model=AnswerResponse)
def ask_question(req: QuestionRequest, user: dict = Depends(get_current_user)):
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="question cannot be empty")

    user_agent = _get_user_agent(user["id"])
    db, agent = user_agent["db"], user_agent["agent"]

    # thread_id is the user's actual identity, not a per-tab random ID —
    # so conversation memory follows the logged-in user across tabs,
    # devices, and browser restarts, not just within one browser tab.
    thread_id = f"user-{user['id']}"

    result = ask(req.question, agent=agent, db=db, thread_id=thread_id)
    chart = generate_chart(result.rows, title=req.question)
    return AnswerResponse(
        answer=result.answer,
        sql_queries=result.sql_queries,
        table=result.rows,
        chart=chart,
    )