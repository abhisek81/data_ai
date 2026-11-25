# ─────────────────────────────────────────────────────────────
# app.py
# Purpose: the web page. Pick a dataset, type a question (or click an example),
#          and see the answer, a chart, the result table and the SQL that was run.
# Run:     streamlit run app.py        (then open http://localhost:8501)
# Uses:    src/sql_agent.py (ask, MODELS), src/data.py (DATASETS, db_path, build_all)
# ─────────────────────────────────────────────────────────────

import datetime as dt                           # today's date, for the daily budget
import os                                       # reads optional settings from environment variables

import pandas as pd
import streamlit as st                          # turns this Python script into a web page

from src.data import DATASETS, build_all, db_path   # dataset list + database builder → src/data.py
from src.sql_agent import MODELS, ask                # the engine → src/sql_agent.py

# ── Settings from Streamlit Cloud "Secrets" ──────────────────
# Online, the API key and limits are typed into the app's Secrets box on share.streamlit.io
# (never into the code). Copy them into environment variables, which is where
# src/sql_agent.py (Claude client) and the settings below look. Locally there is usually
# no secrets file, so the .env file is used instead (loaded by src/sql_agent.py).
for _name in ("ANTHROPIC_API_KEY", "MAX_QUESTIONS_PER_SESSION", "DAILY_BUDGET_USD", "ALLOW_SONNET"):
    try:
        if _name in st.secrets:
            os.environ.setdefault(_name, str(st.secrets[_name]))
    except Exception:            # no secrets file at all (normal when running locally)
        break

# Most questions one visitor can ask per browser session.
MAX_QUESTIONS = int(os.getenv("MAX_QUESTIONS_PER_SESSION", "25"))
# Most the whole app may spend on Claude per day, across ALL visitors (US dollars).
# Reloading the page resets the per-session count above, but not this.
DAILY_BUDGET_USD = float(os.getenv("DAILY_BUDGET_USD", "1.00"))
# Set ALLOW_SONNET=false online to offer only the cheaper Haiku model.
ALLOW_SONNET = os.getenv("ALLOW_SONNET", "true").lower() == "true"
AVAILABLE_MODELS = {k: v for k, v in MODELS.items() if ALLOW_SONNET or "haiku" in v}   # MODELS → src/sql_agent.py

# Clickable example questions shown for each dataset (keys must match src/data.py DATASETS).
EXAMPLES = {
    "Hotel bookings": [
        "Which hotel has the higher cancellation rate?",
        "How does the average daily rate change by month for each hotel?",
        "Which 5 countries send the most guests who actually stayed?",
        "Which market segment has the highest cancellation rate in summer?",
        "Do guests who book further ahead cancel more often?",
    ],
    "TPC-H (sample business data)": [
        "Which 5 customers spent the most in 1997, and which country are they from?",
        "What is the total revenue by region?",
        "How many orders were placed each year?",
        "Which ship mode has the most late deliveries?",
    ],
}

# ── Page setup ───────────────────────────────────────────────

st.set_page_config(page_title="Ask Your Warehouse", page_icon="🔎", layout="wide")   # browser tab title/icon


@st.cache_resource(show_spinner="Preparing the datasets (first run only)...")
def ensure_data() -> bool:
    """Build the databases if they don't exist yet (e.g. first run on a new machine). Runs once per server."""
    if not all(db_path(name).exists() for name in DATASETS):   # any database file missing?
        build_all()                                            # download + generate → src/data.py
    return True


ensure_data()


@st.cache_resource
def spend_tracker() -> dict:
    """One shared record of today's spending, the same object for every visitor.

    It lives in the server's memory, so it resets if the app restarts (e.g. after sleeping).
    Your Anthropic Console spend limit is the backstop for that case.
    """
    return {"day": None, "spent": 0.0}


def budget_left() -> bool:
    """True if today's shared budget isn't used up yet. Starts a fresh count each new day."""
    tracker = spend_tracker()
    today = dt.date.today()
    if tracker["day"] != today:                                        # new day → reset the count
        tracker["day"], tracker["spent"] = today, 0.0
    return tracker["spent"] < DAILY_BUDGET_USD


# st.session_state survives between clicks for the same visitor. Set defaults the first time.
st.session_state.setdefault("history", [])       # list of Answer objects, newest first
st.session_state.setdefault("question", "")      # text currently in the question box

# ── Sidebar: settings ────────────────────────────────────────

with st.sidebar:
    st.header("Settings")
    dataset = st.selectbox("Dataset", list(DATASETS))                  # names from src/data.py
    st.caption(DATASETS[dataset]["about"])                             # one-line description
    model_label = st.radio("Model", list(AVAILABLE_MODELS))            # Haiku (and Sonnet if allowed) → src/sql_agent.py
    st.divider()
    asked = len(st.session_state.history)
    st.caption(f"Questions this session: {asked} / {MAX_QUESTIONS}")
    if st.button("Clear history"):
        st.session_state.history = []
        st.rerun()                                                     # redraw the page immediately

# ── Main area: title, examples, question box ─────────────────

st.title("🔎 Ask Your Warehouse")
st.write("Ask a question in plain English. Claude writes the SQL, a safety check makes sure it only "
         "reads data, DuckDB runs it, and you get the answer with the SQL it used.")

def use_example() -> None:
    """Runs when an example is clicked: copy it into the question box, then clear the pill selection."""
    st.session_state.question = st.session_state.example_pick or ""
    st.session_state.example_pick = None


# Pills wrap onto several lines on narrow screens, so long questions stay readable.
st.pills("Try an example:", EXAMPLES[dataset], key="example_pick", on_change=use_example)

with st.form("ask_form"):                                              # a form submits only when the button is clicked
    question = st.text_input("Your question", key="question",
                             placeholder="e.g. What share of bookings come from online travel agents?")
    submitted = st.form_submit_button("Ask", type="primary")

if submitted and question.strip():
    if len(st.session_state.history) >= MAX_QUESTIONS:
        st.warning(f"You've reached the limit of {MAX_QUESTIONS} questions for this session.")
    elif not budget_left():
        st.warning("This demo has reached its daily usage limit. Please try again tomorrow.")
    else:
        with st.spinner("Writing SQL and running it..."):
            result = ask(question.strip(), dataset, AVAILABLE_MODELS[model_label])   # the engine → src/sql_agent.py
        spend_tracker()["spent"] += result.cost_usd                    # add this question's cost to today's total
        st.session_state.history.insert(0, result)                     # newest result goes on top
        st.rerun()                                                     # redraw so the sidebar counter updates too


# ── Showing results ──────────────────────────────────────────

def show_chart(result) -> None:
    """Draw the chart Claude suggested, if the suggested columns exist and the values are numbers."""
    df = result.data
    x, y = result.chart_x, result.chart_y
    if result.chart == "none" or df is None or len(df) < 2:            # nothing worth charting
        return
    if x not in df.columns or y not in df.columns:                     # Claude named a column that isn't there
        return
    values = pd.to_numeric(df[y], errors="coerce")                     # numbers stored as text/decimals → floats
    if values.isna().all():                                            # not numeric at all → skip
        return
    chart_df = pd.DataFrame({x: df[x].astype(str), y: values})
    if result.chart == "line":
        st.line_chart(chart_df, x=x, y=y)
    else:
        st.bar_chart(chart_df, x=x, y=y, sort=False)                   # sort=False keeps the query's order


for i, result in enumerate(st.session_state.history):
    with st.container(border=True):                                    # one box per question
        st.markdown(f"**Q: {result.question}**")
        st.caption(f"{result.dataset} · {result.model}")

        if result.ok:
            st.success(result.answer)
            show_chart(result)
            label = f"Result table ({len(result.data):,} rows{', first 1,000 shown' if result.truncated else ''})"
            with st.expander(label, expanded=(i == 0)):                # newest result open, older ones folded
                st.dataframe(result.data, use_container_width=True, hide_index=True)
            with st.expander("SQL used"):
                st.code(result.sql, language="sql")
        else:
            st.info(result.answer)                                     # couldn't answer / not about this data

        failed = [a for a in result.attempts if a.error]
        if failed:                                                     # show self-correction when it happened
            with st.expander(f"Self-corrections ({len(failed)})"):
                for a in failed:
                    st.code(a.sql, language="sql")
                    st.caption(f"Error: {a.error}")

        st.caption(f"{result.input_tokens:,} input / {result.output_tokens:,} output tokens · ~${result.cost_usd:.4f}")
