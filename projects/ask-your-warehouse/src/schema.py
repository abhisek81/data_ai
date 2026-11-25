# ─────────────────────────────────────────────────────────────
# src/schema.py
# Purpose: writes a plain-text description of a dataset's tables for Claude:
#          table names, columns, data types, example values, and short notes on
#          what tricky columns mean. Good descriptions = correct SQL.
# Used by:  src/sql_agent.py (describe_dataset)
# Uses:     src/data.py (DATASETS), src/guard.py (open_readonly)
# ─────────────────────────────────────────────────────────────

from functools import lru_cache   # remembers a function's result so we only build each description once

from src.data import DATASETS           # dataset list and descriptions → defined in src/data.py
from src.guard import open_readonly     # opens the database with the same safe settings as queries → src/guard.py
                                        # (DuckDB refuses two connections to one file with different settings)

# Hand-written notes for columns whose meaning isn't obvious from the name.
# Keyed by dataset → table → column. Anything not listed just gets its name and type.
COLUMN_NOTES = {
    "Hotel bookings": {
        "bookings": {
            "hotel": "'City Hotel' or 'Resort Hotel'",
            "is_canceled": "1 = booking was cancelled, 0 = not cancelled",
            "lead_time": "days between booking and arrival date",
            "arrival_date": "arrival date (DATE). Prefer this over the separate arrival_date_* columns",
            "arrival_date_month": "full month name text, e.g. 'July'",
            "stays_in_weekend_nights": "nights booked falling on Sat/Sun",
            "stays_in_week_nights": "nights booked falling Mon-Fri",
            "total_nights": "stays_in_weekend_nights + stays_in_week_nights",
            "meal": "BB = bed & breakfast, HB = half board, FB = full board, SC/Undefined = no meal package",
            "country": "guest's country as ISO 3166-1 alpha-3 code, e.g. 'PRT' = Portugal, 'GBR' = United Kingdom",
            "market_segment": "how the booking was sold, e.g. 'Online TA' (online travel agent), 'Groups', 'Direct'",
            "distribution_channel": "booking channel, e.g. 'TA/TO' (travel agents/tour operators), 'Direct', 'Corporate'",
            "is_repeated_guest": "1 = guest has stayed before, 0 = first time",
            "reserved_room_type": "room type code booked (letters A-L, anonymised)",
            "assigned_room_type": "room type code actually given; differs from reserved_room_type when changed",
            "deposit_type": "'No Deposit', 'Non Refund' or 'Refundable'",
            "agent": "ID of the travel agency (NULL = none)",
            "company": "ID of the company that paid (NULL = none)",
            "customer_type": "'Transient', 'Contract', 'Transient-Party' or 'Group'",
            "adr": "average daily rate in EUR = total lodging price / nights. Use AVG(adr) for average price per night",
            "reservation_status": "'Check-Out' (stayed), 'Canceled' or 'No-Show'",
            "reservation_status_date": "date the reservation_status was last set",
        },
    },
    "TPC-H (sample business data)": {
        "orders": {
            "o_orderstatus": "'F' = fulfilled, 'O' = open, 'P' = partially fulfilled",
            "o_totalprice": "total order value",
            "o_orderpriority": "e.g. '1-URGENT', '2-HIGH', '3-MEDIUM', '4-NOT SPECIFIED', '5-LOW'",
        },
        "lineitem": {
            "l_extendedprice": "quantity x unit price, BEFORE discount",
            "l_discount": "fraction, e.g. 0.05 = 5%. Revenue = l_extendedprice * (1 - l_discount)",
            "l_returnflag": "'R' = returned, 'A' / 'N' = not returned",
        },
        "customer": {
            "c_mktsegment": "market segment, e.g. 'BUILDING', 'AUTOMOBILE'",
            "c_acctbal": "account balance",
        },
        "nation": {"n_name": "country name in UPPER CASE, e.g. 'GERMANY'"},
        "region": {"r_name": "region name in UPPER CASE, e.g. 'EUROPE', 'ASIA'"},
    },
}

# How TPC-H tables join together. Claude needs this because the column names don't make it obvious.
TPCH_JOINS = """Join keys:
  customer.c_custkey = orders.o_custkey
  orders.o_orderkey = lineitem.l_orderkey
  lineitem.l_partkey = part.p_partkey
  lineitem.l_suppkey = supplier.s_suppkey
  partsupp.ps_partkey = part.p_partkey AND partsupp.ps_suppkey = supplier.s_suppkey
  customer.c_nationkey = nation.n_nationkey
  supplier.s_nationkey = nation.n_nationkey
  nation.n_regionkey = region.r_regionkey"""


@lru_cache(maxsize=None)          # build each dataset's description once, then reuse it (also keeps the text identical → cache-friendly for Claude)
def describe_dataset(dataset: str) -> str:
    """Return the full schema description for one dataset as plain text."""
    con = open_readonly(dataset)                                  # read-only: describing never changes data (src/guard.py)
    notes = COLUMN_NOTES.get(dataset, {})                         # notes for this dataset (may be empty)

    lines = [f"Dataset: {dataset}", DATASETS[dataset]["about"], ""]   # header + description from src/data.py

    # List every table in the database, in a fixed (alphabetical) order so the text never changes between runs.
    tables = [r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main' ORDER BY table_name"
    ).fetchall()]

    for table in tables:
        row_count = con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
        lines.append(f"TABLE {table}  ({row_count:,} rows)")

        # Column names and types, in the table's own column order.
        columns = con.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = ? ORDER BY ordinal_position", [table]
        ).fetchall()

        for name, dtype in columns:
            line = f"  - {name} {dtype}"
            note = notes.get(table, {}).get(name)                     # hand-written note, if any
            if note:
                line += f"  -- {note}"
            elif dtype == "VARCHAR":
                line += _example_values(con, table, name)             # show real values for text columns
            lines.append(line)
        lines.append("")

    if dataset.startswith("TPC-H"):
        lines.append(TPCH_JOINS)                                      # join hints only matter for TPC-H

    con.close()
    return "\n".join(lines)


def _example_values(con, table: str, column: str) -> str:
    """For a text column with few distinct values, list them; otherwise show 3 examples.

    Knowing the exact spelling of values (e.g. 'Online TA') stops Claude guessing wrong filters.
    """
    distinct = con.execute(f'SELECT count(DISTINCT "{column}") FROM "{table}"').fetchone()[0]
    if distinct <= 12 and not column.endswith("comment"):             # small set → list all of them (comments are filler text)
        values = [r[0] for r in con.execute(
            f'SELECT DISTINCT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL ORDER BY 1'
        ).fetchall()]
        return "  -- values: " + ", ".join(repr(v) for v in values)
    if column.endswith(("comment", "address", "phone")):              # free text / random filler → examples don't help
        return "  -- free text"
    examples = [r[0] for r in con.execute(                            # large set → 3 sample values
        f'SELECT DISTINCT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL ORDER BY 1 LIMIT 3'
    ).fetchall()]
    return f"  -- {distinct:,} distinct values, e.g. " + ", ".join(repr(v) for v in examples)


# Run directly (python -m src.schema) to print the descriptions and check them by eye.
if __name__ == "__main__":
    for name in DATASETS:
        print(describe_dataset(name))
        print("=" * 70)
