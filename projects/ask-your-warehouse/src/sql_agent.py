# ─────────────────────────────────────────────────────────────
# src/sql_agent.py
# Purpose: the core engine. Turns a plain-English question into SQL, runs it safely,
#          fixes the SQL if it fails, and writes a plain-English answer.
#
#   ask(question)
#     1. write_sql()   – Claude writes SQL from the question + schema   (Claude call #1)
#     2. check_sql()   – safety check                                    (src/guard.py)
#     3. _run_sql()    – run on a read-only DuckDB connection            (src/guard.py)
#        ↳ if 2 or 3 fails: send the error back to write_sql() and retry (max 2 retries)
#     4. summarize()   – Claude explains the result in plain English     (Claude call #2)
#
# Used by:  app.py (web page), cli.py (command line), evals/run_evals.py (accuracy test)
# Uses:     src/schema.py, src/guard.py, src/data.py, ../../.env (API key)
# ─────────────────────────────────────────────────────────────

from dataclasses import dataclass, field   # simple classes for holding results
from pathlib import Path
from typing import Literal, Optional

import anthropic                           # official Claude API client
import pandas as pd                        # result tables (DataFrames)
from dotenv import load_dotenv             # reads ANTHROPIC_API_KEY from the .env file
from pydantic import BaseModel, Field      # describes the exact JSON shape we want back from Claude

from src.guard import UnsafeSQLError, check_sql, list_tables, open_readonly   # → src/guard.py
from src.schema import describe_dataset                                        # → src/schema.py

# ── Setup ────────────────────────────────────────────────────

# The API key lives in the repo-root .env file (C:\Study\data_ai\.env), two folders above this project.
REPO_ROOT = Path(__file__).resolve().parents[3]          # src → ask-your-warehouse → projects → data_ai
load_dotenv(REPO_ROOT / ".env")                          # puts ANTHROPIC_API_KEY into this program's environment only

# Models the user can pick in the app (app.py shows the keys in a dropdown).
MODELS = {
    "Haiku 5.5 (fast, cheapest)": "claude-haiku-5-5",
    "Sonnet 5.5 (smarter, ~20x cost)": "claude-sonnet-5-5",
}
DEFAULT_MODEL = "claude-haiku-5-5"

# Price per million tokens (input, output) in USD, used to show what each question cost.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
}

MAX_RETRIES = 2          # how many times Claude may fix a failing query
MAX_ROWS = 1000          # most rows we fetch from any query (protects memory and the web page)
ROWS_FOR_SUMMARY = 50    # how many result rows Claude sees when writing the answer

_client: Optional[anthropic.Anthropic] = None   # created on first use (see _get_client)


def _get_client() -> anthropic.Anthropic:
    """Create the Claude client once and reuse it. It reads ANTHROPIC_API_KEY from the environment."""
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


# ── What we ask Claude to return ─────────────────────────────

class SqlPlan(BaseModel):
    """The structured answer Claude must give in step 1. The SDK checks Claude's reply matches this shape."""
    can_answer: bool = Field(description="False if the question can't be answered from these tables")
    reason_if_not: str = Field(description="If can_answer is false, a short explanation for the user; else empty")
    sql: str = Field(description="One DuckDB SELECT query answering the question; empty if can_answer is false")
    chart: Literal["bar", "line", "none"] = Field(description="Best chart for the result, or 'none'")
    chart_x: str = Field(description="Result column for the x-axis / categories; empty if chart is 'none'")
    chart_y: str = Field(description="Numeric result column for the y-axis; empty if chart is 'none'")


# Instructions for step 1. The dataset's schema text is appended to this (see _sql_system_prompt).
SQL_RULES = """You are an expert data analyst who writes DuckDB SQL.
Given a question and the database schema below, write ONE read-only SELECT query that answers it.

Rules:
- Use only the tables and columns in the schema. Never invent columns.
- Use DuckDB SQL syntax (e.g. date_trunc, strftime, EXTRACT, FILTER, QUALIFY are available).
- Give every computed column a short, readable alias in snake_case (e.g. cancellation_rate).
- Round money and averages to 2 decimals, and percentages to 1 decimal (as 0-100 numbers, e.g. 37.0).
- Sort results in the most useful order. For "top N" questions use ORDER BY ... LIMIT N.
- Return at most a few hundred rows unless the user clearly wants a full listing.
- Pick a chart only when it helps: 'bar' for comparing categories, 'line' for a trend over time.
- If the question isn't about this data, set can_answer to false and explain briefly.
"""


def _sql_system_prompt(dataset: str) -> str:
    """Rules + schema. Identical for every question on the same dataset (so it can be cached by the API)."""
    return SQL_RULES + "\n" + describe_dataset(dataset)    # schema text → src/schema.py


# ── Result object ────────────────────────────────────────────

@dataclass
class Attempt:
    """One try at writing and running SQL."""
    sql: str
    error: Optional[str] = None            # None means it ran fine


@dataclass
class Answer:
    """Everything the app shows for one question."""
    question: str
    dataset: str
    model: str
    answer: str = ""                                   # plain-English answer
    sql: str = ""                                      # the final SQL that ran
    data: Optional[pd.DataFrame] = None                # the result table
    truncated: bool = False                            # True if the result had more than MAX_ROWS rows
    chart: str = "none"                                # 'bar', 'line' or 'none'
    chart_x: str = ""
    chart_y: str = ""
    attempts: list[Attempt] = field(default_factory=list)   # every try, including failed ones
    ok: bool = False                                   # True if we got a result
    input_tokens: int = 0                              # tokens used, added up over all Claude calls
    output_tokens: int = 0

    @property
    def cost_usd(self) -> float:
        """Approximate cost of this question in US dollars (from PRICES above)."""
        price_in, price_out = PRICES.get(self.model, (0, 0))
        return (self.input_tokens * price_in + self.output_tokens * price_out) / 1_000_000


# ── Step 1: Claude writes the SQL ────────────────────────────

def write_sql(question: str, dataset: str, model: str, attempts: list[Attempt], answer: Answer) -> SqlPlan:
    """Ask Claude for SQL. If earlier attempts failed, include them so Claude can fix the mistake."""
    user_text = f"Question: {question}"
    if attempts:                                                  # this is a retry → show what went wrong
        user_text += "\n\nYour previous attempt(s) failed. Fix the SQL.\n"
        for i, a in enumerate(attempts, start=1):
            user_text += f"\nAttempt {i}:\n{a.sql}\nError: {a.error}\n"

    response = _call_claude(
        model=model,
        system=_sql_system_prompt(dataset),
        user_text=user_text,
        output_format=SqlPlan,                                    # forces Claude's reply into the SqlPlan shape
        effort="medium",                                          # how hard Claude thinks; medium is a good SQL balance
    )
    _add_usage(answer, response)
    if response.stop_reason == "refusal":                         # Claude declined (very unlikely for data questions)
        return SqlPlan(can_answer=False, reason_if_not="The model declined this request.",
                       sql="", chart="none", chart_x="", chart_y="")
    return response.parsed_output                                 # a validated SqlPlan object


# ── Step 4: Claude explains the result ───────────────────────

SUMMARY_RULES = """You explain query results to a business user.
Answer the question directly in 1-3 sentences using the numbers in the result.
Mention the most important figures. Don't describe the SQL. Don't invent numbers that aren't in the result.
If the result is empty, say no matching data was found.
Format numbers with thousands separators; show money in EUR for the hotel data."""


def summarize(question: str, df: pd.DataFrame, truncated: bool, model: str, answer: Answer) -> str:
    """Ask Claude for a short plain-English answer based on the query result."""
    preview = df.head(ROWS_FOR_SUMMARY).to_csv(index=False)       # first rows as CSV text
    note = ""
    if len(df) > ROWS_FOR_SUMMARY or truncated:
        note = f"\n(Showing the first {min(len(df), ROWS_FOR_SUMMARY)} of {len(df)}{'+' if truncated else ''} rows.)"

    response = _call_claude(
        model=model,
        system=SUMMARY_RULES,
        user_text=f"Question: {question}\n\nQuery result:\n{preview}{note}",
        effort="low",                                             # summarising is easy; low effort = faster + cheaper
    )
    _add_usage(answer, response)
    # The reply is a list of blocks (thinking, text, ...). Join the text blocks.
    return "".join(b.text for b in response.content if b.type == "text").strip()


# ── Steps 2-3: check and run the SQL ─────────────────────────

def _run_sql(con, sql: str) -> tuple[pd.DataFrame, bool]:
    """Run a query and return (table, truncated). Fetches at most MAX_ROWS rows, keeping the query's order."""
    cursor = con.execute(sql)
    rows = cursor.fetchmany(MAX_ROWS + 1)                         # one extra row tells us if there were more
    columns = [d[0] for d in cursor.description]                  # column names from the result
    truncated = len(rows) > MAX_ROWS
    return pd.DataFrame(rows[:MAX_ROWS], columns=columns), truncated


# ── The main function ────────────────────────────────────────

def ask(question: str, dataset: str, model: str = DEFAULT_MODEL) -> Answer:
    """Answer one question about one dataset. This is what app.py, cli.py and evals/run_evals.py call."""
    answer = Answer(question=question, dataset=dataset, model=model)
    con = open_readonly(dataset)                                  # locked-down connection → src/guard.py
    allowed_tables = list_tables(con)                             # tables the SQL may use → src/guard.py

    try:
        for _ in range(MAX_RETRIES + 1):                          # first try + up to MAX_RETRIES fixes
            plan = write_sql(question, dataset, model, answer.attempts, answer)

            if not plan.can_answer:                               # question isn't about this data
                answer.answer = plan.reason_if_not or "I can't answer that from this dataset."
                return answer

            attempt = Attempt(sql=plan.sql.strip())
            answer.attempts.append(attempt)
            try:
                check_sql(attempt.sql, allowed_tables)            # safety rules → src/guard.py
                df, truncated = _run_sql(con, attempt.sql)
            except UnsafeSQLError as e:                           # broke a safety rule
                attempt.error = f"Blocked by safety check: {e}"
                continue                                          # let Claude try again
            except Exception as e:                                # DuckDB error, e.g. wrong column name
                attempt.error = str(e).splitlines()[0][:500]     # first line is enough for Claude to fix it
                continue

            # Success: store the result and ask Claude to explain it.
            answer.sql, answer.data, answer.truncated = attempt.sql, df, truncated
            answer.chart, answer.chart_x, answer.chart_y = plan.chart, plan.chart_x, plan.chart_y
            answer.ok = True
            answer.answer = summarize(question, df, truncated, model, answer)
            return answer

        # All tries failed.
        answer.answer = "Sorry, I couldn't write a working query for that. Try rephrasing the question."
        return answer
    finally:
        con.close()                                               # always release the database file


# ── Talking to Claude ────────────────────────────────────────

def _call_claude(model: str, system: str, user_text: str, effort: str, output_format=None):
    """One Claude API call. Handles the small differences between models in one place.

    - output_format: a Pydantic class → Claude's reply is checked against it (structured output).
    - Sonnet 5.5 gets server-side "fallbacks": if it ever declines a request, the API retries
      on another model automatically. (Haiku doesn't support this option.)
    """
    client = _get_client()
    kwargs = dict(
        model=model,
        max_tokens=16000,                                         # upper limit on reply length (includes thinking)
        # The system text is the same for every question → mark it cacheable to save cost on repeat calls.
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_text}],
        output_config={"effort": effort},
    )
    if output_format is not None:
        kwargs["output_format"] = output_format

    if model.startswith("claude-sonnet"):
        api = client.beta.messages                                # fallbacks need the beta endpoint
        kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    else:
        api = client.messages

    # .parse() validates structured output; .create() is a normal call.
    return api.parse(**kwargs) if output_format is not None else api.create(**kwargs)


def _add_usage(answer: Answer, response) -> None:
    """Add this call's token counts to the running total on the Answer."""
    usage = response.usage
    answer.input_tokens += (usage.input_tokens
                            + (usage.cache_creation_input_tokens or 0)
                            + (usage.cache_read_input_tokens or 0))
    answer.output_tokens += usage.output_tokens
