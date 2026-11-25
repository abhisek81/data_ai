# ─────────────────────────────────────────────────────────────
# cli.py
# Purpose: ask a question from the terminal, without the web page. Handy for quick tests.
# Usage:   python cli.py "Which hotel has the higher cancellation rate?"
#          python cli.py --dataset tpch --model sonnet "Top 5 customers by revenue"
# Uses:    src/sql_agent.py (ask, MODELS), src/data.py (DATASETS)
# ─────────────────────────────────────────────────────────────

import argparse                    # reads options typed after the command

from src.data import DATASETS                  # dataset names → src/data.py
from src.sql_agent import MODELS, ask          # the engine → src/sql_agent.py

# Short names you can type on the command line → full names used in the code.
DATASET_SHORT = {"hotels": "Hotel bookings", "tpch": "TPC-H (sample business data)"}
MODEL_SHORT = {"haiku": MODELS["Haiku 5.5 (fast, cheapest)"], "sonnet": MODELS["Sonnet 5.5 (smarter, ~20x cost)"]}


def main() -> None:
    parser = argparse.ArgumentParser(description="Ask a question about a dataset in plain English.")
    parser.add_argument("question", help="your question, in quotes")
    parser.add_argument("--dataset", choices=DATASET_SHORT, default="hotels")
    parser.add_argument("--model", choices=MODEL_SHORT, default="haiku")
    args = parser.parse_args()

    result = ask(args.question, DATASET_SHORT[args.dataset], MODEL_SHORT[args.model])   # run the engine

    # Show every attempt, so you can see self-correction in action.
    for i, attempt in enumerate(result.attempts, start=1):
        print(f"--- SQL attempt {i} ---\n{attempt.sql}")
        if attempt.error:
            print(f"!! error: {attempt.error}")
    if result.data is not None:
        print(f"\n--- Result ({len(result.data)} rows{', truncated' if result.truncated else ''}) ---")
        print(result.data.head(15).to_string(index=False))   # first 15 rows as a text table
    print(f"\n--- Answer ---\n{result.answer}")
    print(f"\n[{result.model} | {result.input_tokens:,} in / {result.output_tokens:,} out tokens"
          f" | ~${result.cost_usd:.4f} | chart: {result.chart}]")


if __name__ == "__main__":
    main()
