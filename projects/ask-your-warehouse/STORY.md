# How Ask Your Warehouse was built

The high-level story of the project, from idea to live app. For setup and technical details, see the [README](README.md).

> *I built an AI analyst that turns plain-English questions into safe, read-only SQL. It corrects its own mistakes, scored 40/40 on a test set I wrote, and runs live for about $0.0004 a question.*

**▶ [Try it live](https://ask-your-warehouse-abhisek.streamlit.app)**

---

### 1. The idea
- Business users have questions about data but can't write SQL.
- Goal: let anyone ask in plain English and get a trustworthy answer, with the SQL shown so the answer can be checked.

### 2. The data
- Picked a public **hotel bookings dataset** (119k bookings), close to my hospitality background, plus **TPC-H**, the standard multi-table business benchmark.
- Loaded both into **DuckDB**, a lightweight embedded database that needs no server.

### 3. Teaching the AI the data
- Built an automatic **schema description**: tables, columns, types, real example values, and notes on tricky columns (e.g. "adr = average daily rate", "revenue = price × (1 − discount)").
- Better context means more accurate SQL.

### 4. The core engine
- The question goes to **Claude**, which returns structured output: the SQL plus a suggested chart.
- The query runs on DuckDB, and Claude turns the result into a 1–3 sentence answer.
- **Self-correction:** if a query fails, the error goes back to Claude to fix, up to 2 retries.

### 5. Safety first
- **Two layers:** a SQL parser allows only a single read-only SELECT on known tables, and the database itself is opened read-only with file and network access switched off.
- Tested it against DROP, DELETE, file reads and multi-statement attacks; all blocked.

### 6. The web app
- A **Streamlit** page with a dataset picker, example questions, the answer, a chart, the result table, the SQL used, and the cost per question.

### 7. Measuring accuracy
- Wrote **40 test questions** with hand-written correct SQL, graded by comparing result *values* rather than SQL text.
- Result: **40/40 correct** with Claude Haiku, at about $0.0004 per question ([latest run](evals/last_run.md)).
- Checked that the grader itself fails wrong answers.

### 8. Going live
- Deployed free on **Streamlit Community Cloud**, with the API key kept in its Secrets.
- Added **cost guardrails**: cheapest model only, 15 questions per visitor, and a $1/day cap across all visitors.
- Version-controlled on GitHub with pull requests, and linked from the README.
