# ─────────────────────────────────────────────────────────────
# evals/run_evals.py
# Purpose: measures accuracy. Asks the app every question in evals/questions.yaml and
#          checks the app's result table against the hand-written correct query.
# Run:     python -m evals.run_evals                 (Haiku, both datasets)
#          python -m evals.run_evals --model sonnet  (compare with Sonnet)
# Output:  a pass/fail line per question, a score, and evals/last_run.md (a report you can commit)
# Uses:    src/sql_agent.py (ask, MODELS), src/guard.py (open_readonly), evals/questions.yaml
# ─────────────────────────────────────────────────────────────

import argparse
import datetime as dt
import math                                         # isclose() for comparing numbers with a tolerance
from concurrent.futures import ThreadPoolExecutor   # asks several questions at the same time (faster)
from decimal import Decimal
from pathlib import Path

import pandas as pd
import yaml                                         # reads questions.yaml

from src.guard import open_readonly                 # read-only DB connection → src/guard.py
from src.sql_agent import MODELS, ask               # the engine being tested → src/sql_agent.py

EVALS_DIR = Path(__file__).resolve().parent         # this folder (evals/)
MODEL_SHORT = {"haiku": MODELS["Haiku 5.5 (fast, cheapest)"], "sonnet": MODELS["Sonnet 5.5 (smarter, ~20x cost)"]}


# ── Comparing results ────────────────────────────────────────

def _normalize(value):
    """Turn a cell into something comparable: numbers → float, text → trimmed lower-case, NULL → None."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, (bool,)):
        return float(value)
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    return str(value).strip().lower()


def _same(a, b) -> bool:
    """True if two normalized cells match. Numbers may differ by 1% (rounding to 1 vs 2 decimals, etc.)."""
    if isinstance(a, float) and isinstance(b, float):
        return math.isclose(a, b, rel_tol=0.01, abs_tol=0.01)
    return a == b


def _column_values(df: pd.DataFrame, col: str, ordered: bool) -> list:
    """All values of one column, normalized; sorted unless the row order matters."""
    values = [_normalize(v) for v in df[col].tolist()]
    return values if ordered else sorted(values, key=lambda v: (v is None, str(type(v)), v if v is not None else 0))


def results_match(expected: pd.DataFrame, actual: pd.DataFrame, ordered: bool, check_columns) -> tuple[bool, str]:
    """Check the app's table against the correct one. Returns (passed, reason-if-failed).

    Rules:
    - Column names don't matter; each expected column must match SOME actual column by its values.
    - ordered questions: the first rows of the app's result must equal the expected rows, in order
      (extra rows afterwards are allowed, e.g. a full sorted list for "which is highest").
    - other questions: same number of rows, any order.
    """
    if actual is None:
        return False, "no result"
    if ordered:
        if len(actual) < len(expected):
            return False, f"expected at least {len(expected)} rows, got {len(actual)}"
        actual = actual.head(len(expected))                     # compare only the first rows
    elif len(actual) != len(expected):
        return False, f"expected {len(expected)} rows, got {len(actual)}"

    for col in (check_columns or expected.columns):
        want = _column_values(expected, col, ordered)
        found = any(
            all(_same(w, g) for w, g in zip(want, _column_values(actual, a_col, ordered)))
            for a_col in actual.columns
        )
        if not found:
            return False, f"no column matches expected '{col}' = {want[:5]}"
    return True, ""


# ── Running the test ─────────────────────────────────────────

def run_case(dataset: str, case: dict, model: str) -> dict:
    """Ask one question and grade it."""
    con = open_readonly(dataset)
    expected = con.execute(case["expected_sql"]).df()          # the correct answer, from the hand-written SQL
    con.close()

    result = ask(case["question"], dataset, model)             # what the app produces → src/sql_agent.py
    passed, reason = results_match(expected, result.data, case.get("ordered", False), case.get("check_columns"))
    return {
        "dataset": dataset, "question": case["question"], "passed": passed, "reason": reason,
        "sql": result.sql or (result.attempts[-1].sql if result.attempts else ""),
        "retries": sum(1 for a in result.attempts if a.error), "cost": result.cost_usd,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure how often the app returns the correct result.")
    parser.add_argument("--model", choices=MODEL_SHORT, default="haiku")
    parser.add_argument("--workers", type=int, default=4, help="questions asked in parallel")
    args = parser.parse_args()
    model = MODEL_SHORT[args.model]

    suite = yaml.safe_load((EVALS_DIR / "questions.yaml").read_text(encoding="utf-8"))
    jobs = [(dataset, case) for dataset, cases in suite.items() for case in cases]   # flatten to one list

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda job: run_case(job[0], job[1], model), jobs))   # keeps the original order

    # Print a line per question, then totals per dataset.
    for r in results:
        mark = "PASS" if r["passed"] else "FAIL"
        extra = f"  ({r['retries']} self-correction)" if r["retries"] else ""
        print(f"[{mark}] {r['dataset'][:6]} | {r['question']}{extra}")
        if not r["passed"]:
            print(f"        reason: {r['reason']}\n        sql: {' '.join(r['sql'].split())}")

    lines = [f"# Eval results\n", f"- Date: {dt.date.today()}", f"- Model: `{model}`"]
    print()
    for dataset in suite:
        rs = [r for r in results if r["dataset"] == dataset]
        score = sum(r["passed"] for r in rs)
        summary = f"{dataset}: {score}/{len(rs)} correct ({100 * score / len(rs):.0f}%)"
        print(summary)
        lines.append(f"- {summary}")
    total = sum(r["passed"] for r in results)
    cost = sum(r["cost"] for r in results)
    print(f"TOTAL: {total}/{len(results)} correct | cost ~${cost:.3f}")
    lines += [f"- **Total: {total}/{len(results)} correct** (cost ~${cost:.3f})", "",
              "| Result | Dataset | Question | Note |", "|---|---|---|---|"]
    for r in results:
        note = r["reason"] or (f"{r['retries']} self-correction(s)" if r["retries"] else "")
        lines.append(f"| {'✅' if r['passed'] else '❌'} | {r['dataset']} | {r['question']} | {note} |")

    (EVALS_DIR / "last_run.md").write_text("\n".join(lines) + "\n", encoding="utf-8")   # report file for the README


if __name__ == "__main__":
    main()
