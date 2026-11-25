# ─────────────────────────────────────────────────────────────
# src/guard.py
# Purpose: safety checks so AI-written SQL can only READ the data, never change it,
#          and can't reach files or the internet. Two layers of protection:
#            1. check_sql()      – inspects the SQL text before it runs
#            2. open_readonly()  – opens the database in read-only mode with
#                                  file/network access switched off
# Used by:  src/sql_agent.py (check_sql, open_readonly, UnsafeSQLError)
# Uses:     src/data.py (db_path)
# ─────────────────────────────────────────────────────────────

import duckdb                     # the database engine
import sqlglot                    # parses SQL text into a tree we can inspect
from sqlglot import exp           # "expression" node types: Select, Insert, Table, ...

from src.data import db_path      # where each dataset's database file lives → src/data.py

# Statement types that change data or settings. If any appear anywhere in the query, we refuse it.
FORBIDDEN_NODES = (
    exp.Insert, exp.Update, exp.Delete, exp.Merge,   # change rows
    exp.Create, exp.Drop, exp.Alter,                  # change tables
    exp.Copy, exp.Command, exp.Set, exp.Pragma,       # export files, run commands, change settings
    exp.Attach, exp.Detach, exp.Use,                  # connect to other databases
)

# Table functions that read files or the web (e.g. SELECT * FROM read_csv('C:/secret.csv')).
FORBIDDEN_FUNCTIONS = {
    "read_csv", "read_csv_auto", "read_parquet", "parquet_scan", "read_json", "read_json_auto",
    "read_text", "read_blob", "glob", "sniff_csv", "read_xlsx", "query_table", "query",
}


class UnsafeSQLError(Exception):
    """Raised when a query breaks a safety rule. The message explains which rule (sent back to Claude to fix)."""


def check_sql(sql: str, allowed_tables: set[str]) -> None:
    """Raise UnsafeSQLError unless `sql` is exactly one read-only query on the dataset's own tables.

    allowed_tables: lower-case table names in the current dataset (src/sql_agent.py passes these in).
    """
    try:
        statements = sqlglot.parse(sql, read="duckdb")   # turn the text into syntax trees (one per statement)
    except sqlglot.errors.ParseError as e:
        raise UnsafeSQLError(f"SQL could not be parsed: {e}") from e

    statements = [s for s in statements if s is not None]   # a trailing ";" produces an empty entry; drop it
    if len(statements) != 1:                                 # "SELECT ...; DROP TABLE ..." has two statements
        raise UnsafeSQLError("Exactly one SQL statement is allowed.")
    tree = statements[0]

    if not isinstance(tree, exp.Query):                      # exp.Query covers SELECT, UNION, INTERSECT, EXCEPT (incl. WITH ...)
        raise UnsafeSQLError("Only SELECT queries are allowed.")

    for node in tree.walk():                                 # visit every part of the query, including subqueries
        if isinstance(node, FORBIDDEN_NODES):
            raise UnsafeSQLError(f"'{node.key.upper()}' is not allowed; queries must be read-only.")
        if isinstance(node, exp.Func) and _func_name(node) in FORBIDDEN_FUNCTIONS:
            raise UnsafeSQLError(f"Function '{_func_name(node)}' is not allowed (no file or web access).")

    # Names defined by the query itself with WITH ... AS (...) are fine to select from.
    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}

    for table in tree.find_all(exp.Table):                   # every FROM / JOIN target
        name = table.name.lower()
        if not name:                                         # e.g. FROM read_csv(...) parses as a table with no name
            continue                                         # (already caught by the function check above)
        if name not in allowed_tables and name not in cte_names:
            raise UnsafeSQLError(
                f"Unknown table '{table.name}'. Available tables: {', '.join(sorted(allowed_tables))}."
            )


def _func_name(node: exp.Func) -> str:
    """Lower-case name of a SQL function node, e.g. 'read_csv'."""
    if isinstance(node, exp.Anonymous):                      # functions sqlglot doesn't know by name
        return str(node.this).lower()
    return node.sql_name().lower()                           # known functions, e.g. 'SUM' → 'sum'


def open_readonly(dataset: str) -> duckdb.DuckDBPyConnection:
    """Open a dataset's database so that even a query that slipped past check_sql can't do harm."""
    return duckdb.connect(
        str(db_path(dataset)),                               # file path → src/data.py
        read_only=True,                                      # no writes to the database file
        config={"enable_external_access": False},            # no reading local files, no web requests
    )


def list_tables(con: duckdb.DuckDBPyConnection) -> set[str]:
    """Lower-case names of all tables in the open database (the 'allowed_tables' for check_sql)."""
    rows = con.execute(
        "SELECT lower(table_name) FROM information_schema.tables WHERE table_schema = 'main'"
    ).fetchall()
    return {r[0] for r in rows}
