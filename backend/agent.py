"""
agent.py
--------
The core LangChain SQL Agent: takes a plain-English question, decides what
SQL to run against the connected database, executes it, and returns a
natural-language answer plus the raw rows (so chart_generator.py can plot
them later).

Design notes:
- Uses the current LangChain-recommended pattern: SQLDatabaseToolkit +
  a LangGraph ReAct agent (the older `create_sql_agent` helper still
  works but is being phased out in favor of this).
- Uses Ollama as the LLM provider: runs fully locally, no API key, no
  rate limits. Requires Ollama installed (https://ollama.com) and the
  model pulled once: `ollama pull qwen2.5`. qwen2.5 is used over
  llama3.1 because it's noticeably more reliable at multi-step tool
  calling (schema lookup -> query) in this ReAct loop — llama3.1 8B
  would sometimes narrate a tool call as text instead of invoking it,
  or hallucinate table names inside real tool calls. Swap `get_llm()`
  back to ChatGroq if you'd rather use a hosted API later.
- The agent is intentionally given a system prompt that limits it to
  read-only behavior (SELECT only) — see SYSTEM_PROMPT below. Never let
  a text-to-SQL agent run unrestricted DELETE/UPDATE against a real DB.
"""

import os
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from langchain.agents import create_agent
from langchain.agents.middleware import SummarizationMiddleware
from langchain_groq import ChatGroq
from langchain_ollama import ChatOllama
from langchain_community.utilities import SQLDatabase
from langchain_community.agent_toolkits import SQLDatabaseToolkit
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import text

from database import get_database, get_engine


SYSTEM_PROMPT = """\
You are a senior data analyst agent with access to a {dialect} database.

Given a user's question:
1. Call the list-tables tool to see the ACTUAL table names available.
   Never guess, assume, or make up a table name — uploaded files get
   auto-generated names (e.g. "uploaded_sales_data_a1b2c3"), not the
   generic name you might expect. If a table you need isn't found by an
   obvious name, look at the full table list before giving up.
2. Call the schema tool on the relevant table(s) to see actual column names.
3. Write a syntactically correct {dialect} query to answer the question.
4. Double-check the query for common mistakes (wrong column names, bad joins,
   incorrect date filters) before running it.
5. Run the query using the query tool and look at the results.
6. Give a clear, concise natural-language answer based on the results —
   don't just dump the table back at the user.

Critical: you MUST actually invoke tools to do steps 1, 2, and 5 — never
describe, narrate, or print what a tool call would look like as plain text
in your answer. If you find yourself writing something like "Here is a
function call with its arguments," stop — call the tool for real instead.
A response that only describes a plan without calling any tools is wrong
and incomplete.

Hard rules:
- You may ONLY run read-only queries (SELECT). Never run INSERT, UPDATE,
  DELETE, DROP, ALTER, or any other statement that modifies data or schema.
- Unless the user asks for a specific number of rows, limit results to at
  most {top_k} rows.
- If a question is ambiguous, state the assumption you're making rather
  than asking a follow-up question.
- If the query returns no rows, say so plainly rather than guessing.
"""


@dataclass
class AgentResponse:
    """What the agent returns after answering a question."""
    answer: str
    sql_queries: list[str] = field(default_factory=list)
    rows: list[dict] | None = None  # last query's results, for charting


def _to_json_safe(value):
    """
    Postgres driver types that don't serialize cleanly by default:
    - Decimal (from NUMERIC columns) -> float
    - date/datetime -> ISO string
    Anything else passes through unchanged. Without this, FastAPI's
    JSON encoder throws on the first NUMERIC column it hits, and
    Plotly/pandas handle Decimal inconsistently.
    """
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def get_llm(temperature: float = 0.0):
    """
    Returns the LLM to use, chosen via the LLM_PROVIDER env var:

      LLM_PROVIDER=ollama (default) — local Ollama model, no API key, no
      rate limits, but needs Ollama actually running (fine for local dev,
      not viable on most free hosting — too little RAM/CPU).

      LLM_PROVIDER=groq — Groq's hosted API, free tier (no credit card,
      ~30 req/min). Use this for a deployed instance where you can't run
      Ollama yourself. Requires GROQ_API_KEY.

    Same interface either way — nothing else in this file needs to know
    which provider is active.
    """
    provider = os.environ.get("LLM_PROVIDER", "ollama").lower()

    if provider == "groq":
        return ChatGroq(
            model=os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile"),
            temperature=temperature,
            api_key=os.environ.get("GROQ_API_KEY"),
        )

    # --- Ollama (local dev default) ---
    # qwen2.5 is used because it has more reliable native tool-calling
    # support than llama3.1 8B for multi-step agent loops like this one
    # (list tables -> get schema -> run query). If you swap models, verify
    # tool-calling reliability before trusting it in a demo — some smaller
    # models will narrate a tool call as plain text instead of actually
    # invoking it, which looks like a working answer but isn't.
    #
    # num_ctx matters more than it looks: Ollama defaults to a 2048-token
    # context window regardless of what the model actually supports. A
    # single schema lookup on a wide table can already be a few hundred
    # tokens, so the tiny default risks silently truncating context mid-
    # conversation. 8192 gives real headroom; raise OLLAMA_NUM_CTX further
    # if you have tables with many columns or long conversations.
    return ChatOllama(
        model=os.environ.get("OLLAMA_MODEL", "qwen2.5"),
        temperature=temperature,
        base_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
        num_ctx=int(os.environ.get("OLLAMA_NUM_CTX", "8192")),
    )


def build_sql_agent(db: SQLDatabase | None = None, top_k: int = 20):
    """
    Wires up the LLM + database + tools into a runnable ReAct agent.
    Returns the compiled agent graph — call `.stream(...)` or `.invoke(...)`
    on it (see `ask` below for the usage pattern).

    Includes a checkpointer (MemorySaver) so the agent remembers earlier
    turns within the same conversation — e.g. "now break that down by
    product" after asking about total revenue works, because the agent
    can see the prior question and answer. Memory is scoped per
    thread_id (see `ask`'s thread_id param) so different users/sessions
    don't see each other's conversation history.

    Note: MemorySaver keeps history in the backend process's RAM — it's
    reset on every restart and doesn't scale across multiple backend
    instances. Fine for a single-instance demo; swap for a persistent
    checkpointer (e.g. Postgres-backed) before running this multi-instance
    in production.

    Context window strategy (why this doesn't blow up on long chats):
    Conversation memory grows unbounded by default — every question,
    answer, and tool result (schema dumps, query rows) stays in history
    forever. Combined with Ollama's real context limit (see num_ctx in
    get_llm), a long conversation would eventually either overflow or
    silently push early context out. SummarizationMiddleware below
    handles this: once history crosses ~3000 tokens, it uses the LLM to
    compress everything except the most recent 10 messages into a short
    summary, then continues from there. So the agent keeps the gist of
    a long conversation without keeping every raw token of it.
    """
    db = db or get_database()
    llm = get_llm()

    toolkit = SQLDatabaseToolkit(db=db, llm=llm)
    tools = toolkit.get_tools()

    system_message = SYSTEM_PROMPT.format(dialect=db.dialect, top_k=top_k)

    agent = create_agent(
        llm,
        tools,
        system_prompt=system_message,
        checkpointer=MemorySaver(),
        middleware=[
            SummarizationMiddleware(
                model=llm,
                trigger=("tokens", 3000),
                keep=("messages", 10),
            ),
        ],
    )
    return agent


def _fetch_query_rows(query: str) -> list[dict] | None:
    """
    Re-run a SQL query the agent already executed once (via its tool call)
    to get clean, structured list-of-dict rows — the agent's own tool
    returns a stringified preview, not something chart_generator.py or
    the API layer can use directly.

    Safe to re-run: the agent is restricted to SELECT-only queries by
    SYSTEM_PROMPT, so this has no additional side effects, just structured
    output this time. Returns None if re-running fails for any reason
    (e.g. the query used a temp construct that doesn't survive a fresh
    connection) rather than breaking the caller's response.
    """
    try:
        engine = get_engine()
        with engine.connect() as conn:
            result = conn.execute(text(query))
            columns = list(result.keys())
            return [
                {col: _to_json_safe(val) for col, val in zip(columns, row)}
                for row in result.fetchall()
            ]
    except Exception:
        return None


INSIGHTS_PROMPT_TEMPLATE = """\
Generate a business insights summary for the table `{table_name}`.

Explore the data from multiple angles by running SEVERAL distinct SELECT
queries (not just one) before writing your summary:
1. Overall shape: row count, and date range if there's a date column.
2. A time-based trend if there's a date/time column (e.g. totals by month
   or week) — skip this angle if there's no date column.
3. A breakdown by the most meaningful categorical column (e.g. region,
   product, category) showing which segments are largest and smallest.
4. One more angle if useful: a top-N ranking, a concentration (e.g. "the
   top 3 products account for X% of revenue"), or a notable outlier.

After running these queries, write a concise business insights summary
(4-6 sentences, plain language for a business reader — don't mention
table or column names directly) covering:
- The overall size/shape of the data
- The most important trend or pattern you found
- Which segments stand out (highest, lowest, fastest-growing)
- One concrete, actionable observation a stakeholder would care about
"""


@dataclass
class InsightsResponse:
    """What generate_insights returns: a written summary plus one dataset
    per exploratory query the agent ran, for the caller to chart."""
    summary: str
    sql_queries: list[str] = field(default_factory=list)
    datasets: list[list[dict]] = field(default_factory=list)  # one per query


def generate_insights(
    table_name: str,
    agent=None,
    db: SQLDatabase | None = None,
    thread_id: str = "insights",
) -> InsightsResponse:
    """
    Runs a multi-query exploration of one table and returns a written
    summary plus the structured results of every query the agent ran —
    so the caller (main.py) can generate a chart per angle explored,
    not just one chart for the final query like `ask()` does.

    Uses a separate thread_id from regular conversation ("insights" by
    default) so generating a report doesn't pollute the user's Q&A
    history, and vice versa.
    """
    agent = agent or build_sql_agent(db=db)
    question = INSIGHTS_PROMPT_TEMPLATE.format(table_name=table_name)

    sql_queries: list[str] = []
    final_answer = ""

    events = agent.stream(
        {"messages": [("user", question)]},
        config={"configurable": {"thread_id": thread_id}},
        stream_mode="values",
    )

    for event in events:
        last_message = event["messages"][-1]
        tool_calls = getattr(last_message, "tool_calls", None)
        if tool_calls:
            for call in tool_calls:
                if call.get("name") == "sql_db_query":
                    sql_queries.append(call["args"].get("query", ""))
        if getattr(last_message, "type", None) == "ai" and last_message.content:
            final_answer = last_message.content

    # One dataset per query, in the order the agent ran them — skip any
    # that failed to re-run rather than dropping the whole report.
    datasets = []
    for query in sql_queries:
        rows = _fetch_query_rows(query)
        if rows:
            datasets.append(rows)

    return InsightsResponse(summary=final_answer, sql_queries=sql_queries, datasets=datasets)


def ask(
    question: str,
    agent=None,
    db: SQLDatabase | None = None,
    thread_id: str = "default",
) -> AgentResponse:
    """
    The main entry point: ask a plain-English question, get back an answer.

    thread_id groups messages into the same remembered conversation — pass
    a stable ID per user session (e.g. a UUID generated once when the
    frontend loads) so follow-up questions like "break that down further"
    correctly reference what was just discussed. Different thread_ids are
    completely isolated from each other.

    Example:
        >>> response = ask("What were total sales by region last month?", thread_id="abc123")
        >>> print(response.answer)
    """
    agent = agent or build_sql_agent(db=db)

    sql_queries: list[str] = []
    final_answer = ""

    events = agent.stream(
        {"messages": [("user", question)]},
        config={"configurable": {"thread_id": thread_id}},
        stream_mode="values",
    )

    for event in events:
        last_message = event["messages"][-1]

        # Capture SQL the agent actually ran, so the frontend/report can
        # show it for transparency ("here's the query behind this answer").
        tool_calls = getattr(last_message, "tool_calls", None)
        if tool_calls:
            for call in tool_calls:
                if call.get("name") == "sql_db_query":
                    sql_queries.append(call["args"].get("query", ""))

        if getattr(last_message, "type", None) == "ai" and last_message.content:
            final_answer = last_message.content

    rows = _fetch_query_rows(sql_queries[-1]) if sql_queries else None

    return AgentResponse(answer=final_answer, sql_queries=sql_queries, rows=rows)


if __name__ == "__main__":
    # Manual smoke test:
    #   export GROQ_API_KEY=your_key
    #   export DATABASE_URL=postgresql+psycopg2://user:pass@localhost:5432/salesdb
    #   python agent.py
    import sys

    question = " ".join(sys.argv[1:]) or "How many rows are in each table?"
    print(f"Q: {question}\n")

    result = ask(question)
    print("A:", result.answer)
    if result.sql_queries:
        print("\nSQL run:")
        for q in result.sql_queries:
            print(" ", q)