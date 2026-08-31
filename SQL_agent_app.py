
"""
Streamlit front end for the SQL Agent (SQL_agent.py + utils.py) — branded
as "QueryLens".

Reflection pattern (same as SQL_agent.run_sql_workflow), rendered as a
chat UI instead of notebook print_html cards:
  1) Extract schema
  2) Generate SQL (V1)
  3) Execute V1
  4) Reflect on V1's real output -> refine to V2
  5) Execute V2 (final answer)

Each chat turn re-runs that pipeline. Follow-up messages carry the
previous question + final SQL as context, so the user can iterate
("now group that by month") or ask something new in the same thread.

Targets the single event-sourced `transactions` table that
utils.create_transactions_db produces (inserts / restocks / sales /
price_updates), but works against any SQLite DB with a `transactions`
table matching that shape, since utils.get_schema is hardcoded to it.

Run with:  streamlit run SQL_agent_app.py
"""

import math
import os
import traceback

import numpy as np
import streamlit as st
from dotenv import load_dotenv
from PIL import Image, ImageDraw

import utils
from SQL_agent import generate_sql, refine_sql_external_feedback

load_dotenv()

APP_DIR = os.path.dirname(__file__)
DEFAULT_DB = os.path.join(APP_DIR, "products.db")
LOGO_PATH = os.path.join(APP_DIR, "assets", "logo.png")

APP_NAME = "QueryLens"

MODEL_OPTIONS = [
    "openai:gpt-4.1-mini",
    "openai:gpt-4.1",
    "openai:gpt-4o-mini",
    "openai:gpt-4o",
]

EXAMPLE_QUESTIONS = [
    "Which category has sold the most units?",
    "What is the current stock on hand for each product?",
    "Which brand has generated the most revenue from sales?",
    "How many times was the price changed for each product?",
]


# ---------------------------------------------------------------- logo
# The QueryLens mark: a magnifying glass over data rows on a blue->purple
# gradient, circular "avatar" style (matching the other agent apps in
# this family, e.g. Vega). Generated once into assets/logo.png
# (regenerated only if that file is missing), so the app is
# self-contained — no separate logo-generation script needed.

def _make_gradient(size: int, c1: tuple, c2: tuple) -> Image.Image:
    """Diagonal linear gradient, top-left (c1) to bottom-right (c2)."""
    x = np.linspace(0, 1, size)
    y = np.linspace(0, 1, size)
    xx, yy = np.meshgrid(x, y)
    t = (xx + yy) / 2.0
    arr = np.zeros((size, size, 3), dtype=np.uint8)
    for i in range(3):
        arr[:, :, i] = (c1[i] + (c2[i] - c1[i]) * t).astype(np.uint8)
    return Image.fromarray(arr, mode="RGB").convert("RGBA")


def make_logo(out_path: str) -> None:
    """Draws the logo at 4x supersampling (for clean anti-aliased edges at
    favicon size) and saves it to out_path."""
    size = 256
    scale = 4
    s = size * scale
    color_a = (37, 99, 235)    # blue-600
    color_b = (124, 58, 237)   # violet-600
    white = (255, 255, 255, 255)

    gradient = _make_gradient(s, color_a, color_b)

    mask = Image.new("L", (s, s), 0)
    mdraw = ImageDraw.Draw(mask)
    mdraw.ellipse([0, 0, s - 1, s - 1], fill=255)

    base = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    base = Image.composite(gradient, base, mask)

    draw = ImageDraw.Draw(base)

    # Three "data row" bars, lower-left, subtly rounded, semi-opaque white
    row_x0 = int(s * 0.20)
    row_x1 = int(s * 0.62)
    row_h = int(s * 0.045)
    row_gap = int(s * 0.09)
    row_y_start = int(s * 0.62)
    row_widths = [1.0, 0.8, 0.62]
    for i, w in enumerate(row_widths):
        y0 = row_y_start + i * row_gap
        y1 = y0 + row_h
        x1 = row_x0 + int((row_x1 - row_x0) * w)
        draw.rounded_rectangle([row_x0, y0, x1, y1], radius=row_h // 2,
                                fill=(255, 255, 255, 210))

    # Magnifying glass — circle + handle, overlapping the rows top-right
    cx, cy = int(s * 0.60), int(s * 0.40)
    r = int(s * 0.20)
    ring_w = int(s * 0.045)
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=white, width=ring_w)

    handle_len = int(s * 0.20)
    angle = 45  # degrees, pointing to lower-right
    rad = math.radians(angle)
    hx0 = cx + int((r - ring_w * 0.2) * math.cos(rad))
    hy0 = cy + int((r - ring_w * 0.2) * math.sin(rad))
    hx1 = hx0 + int(handle_len * math.cos(rad))
    hy1 = hy0 + int(handle_len * math.sin(rad))
    draw.line([hx0, hy0, hx1, hy1], fill=white, width=int(ring_w * 1.15))
    cap_r = int(ring_w * 0.6)  # round the handle end
    draw.ellipse([hx1 - cap_r, hy1 - cap_r, hx1 + cap_r, hy1 + cap_r], fill=white)

    # Small AI "spark" accent, top-right
    spark_cx, spark_cy = int(s * 0.80), int(s * 0.18)
    spark_r_outer, spark_r_inner = int(s * 0.055), int(s * 0.02)
    pts = []
    for k in range(8):
        ang = math.pi / 4 * k
        rr = spark_r_outer if k % 2 == 0 else spark_r_inner
        pts.append((spark_cx + rr * math.cos(ang), spark_cy + rr * math.sin(ang)))
    draw.polygon(pts, fill=(253, 224, 71, 255))  # amber-ish spark

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    base.resize((size, size), Image.LANCZOS).save(out_path)


def ensure_logo(path: str) -> str:
    """Generates the logo the first time it's needed; reuses it after."""
    if not os.path.exists(path):
        make_logo(path)
    return path


# ---------------------------------------------------------------- helpers

def strip_fences_for_display(raw: str) -> str:
    """Cosmetic only — utils.execute_sql already strips ```sql fences
    before running the query; this just keeps the code block clean when
    we show the raw model output in the UI."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[: -3]
    return text.strip()


def is_error_df(df) -> bool:
    """utils.execute_sql returns a one-column DataFrame named 'error'
    instead of raising when a query fails."""
    return list(df.columns) == ["error"]


def build_contextual_question(prior_turn: dict | None, message: str) -> str:
    """Fold the previous turn into the new prompt so the agent can tell
    an iteration ("now group by month") from a fresh question."""
    if not prior_turn:
        return message
    return f"""Conversation context (for reference only):
Previous question: {prior_turn['question']}
Previous final SQL: {prior_turn['sql_v2']}

New message from the user: {message}

If the new message refines, filters, or builds on the previous question,
adjust the previous SQL accordingly. If it's an unrelated new question,
ignore the previous SQL and answer the new question directly using the
schema."""


def run_pipeline(question_for_model: str, display_question: str, db_path: str,
                  model_generation: str, model_evaluation: str) -> dict:
    """Runs the 5-step reflection pipeline and returns a turn dict used
    both for chat rendering and as context for the next turn."""
    schema = utils.get_schema(db_path)
    sql_v1_raw = generate_sql(question_for_model, schema, model_generation)
    df_v1 = utils.execute_sql(sql_v1_raw, db_path)
    feedback, sql_v2_raw = refine_sql_external_feedback(
        question=question_for_model,
        sql_query=sql_v1_raw,
        df_feedback=df_v1,
        schema=schema,
        model=model_evaluation,
    )
    df_v2 = utils.execute_sql(sql_v2_raw, db_path)
    return {
        "question": display_question,
        "schema": schema,
        "sql_v1": strip_fences_for_display(sql_v1_raw),
        "df_v1": df_v1,
        "feedback": feedback,
        "sql_v2": strip_fences_for_display(sql_v2_raw),
        "df_v2": df_v2,
        "final_ok": not is_error_df(df_v2),
    }


def render_turn(turn: dict) -> None:
    """Renders one assistant turn as separate step-by-step panels (not
    bundled into one collapsed tab), then the final answer up front."""
    with st.expander("📘 Step 1 — Database schema", expanded=False):
        st.code(turn["schema"], language="text")

    with st.expander("🧠 Step 2 — Generated SQL (V1)", expanded=True):
        st.code(turn["sql_v1"], language="sql")

    with st.expander("🧪 Step 3 — V1 output", expanded=True):
        if is_error_df(turn["df_v1"]):
            st.error(f"V1 failed to execute: {turn['df_v1']['error'].iloc[0]}")
        else:
            st.dataframe(turn["df_v1"], use_container_width=True)

    with st.expander("🧭 Step 4 — Reflection", expanded=True):
        st.write(turn["feedback"])
        st.code(turn["sql_v2"], language="sql")

    st.markdown("**✅ Final answer**")
    if turn["final_ok"]:
        st.dataframe(turn["df_v2"], use_container_width=True)
    else:
        st.error(f"Final SQL failed to execute: {turn['df_v2']['error'].iloc[0]}")


# ------------------------------------------------------- flow diagram
# Mirrors the visual style used across this agent family (e.g. Vega):
# small rounded nodes color-coded by role, connected left to right.

_NODE_STYLES = {
    "input": "background:#fde2e2; color:#9b1c1c; font-weight:600;",
    "llm": "background:#e5e7eb; color:#1f2937; font-weight:600;",
    "artifact": "background:#ffffff; color:#111827; font-weight:500; "
                "border:1px solid #d1d5db;",
    "execute": "background:#d1fae5; color:#065f46; font-weight:600;",
}


def _node(label: str, kind: str) -> str:
    style = _NODE_STYLES[kind]
    return (
        f'<div style="{style} border-radius:10px; padding:10px 14px; '
        f'font-size:13px; line-height:1.3; text-align:center; '
        f'white-space:pre-line; min-width:86px;">{label}</div>'
    )


def _arrow() -> str:
    return '<div style="color:#9ca3af; font-size:18px; padding:0 2px;">→</div>'


def render_flow_diagram() -> None:
    nodes = [
        ("input", "User question\n+ schema"),
        ("llm", "LLM\nWrite SQL"),
        ("artifact", "SQL V1"),
        ("execute", "Execute\nV1"),
        ("artifact", "SQL V1 +\noutput/error"),
        ("llm", "LLM\nReflect, refine SQL"),
        ("artifact", "SQL V2"),
        ("execute", "Execute\nV2"),
    ]
    parts = []
    for i, (kind, label) in enumerate(nodes):
        if i > 0:
            parts.append(_arrow())
        parts.append(_node(label, kind))
    html = (
        '<div style="display:flex; align-items:center; gap:8px; '
        'flex-wrap:wrap;">' + "".join(parts) + "</div>"
    )
    st.markdown(html, unsafe_allow_html=True)


# ------------------------------------------------------------------- UI

ensure_logo(LOGO_PATH)

st.set_page_config(page_title=APP_NAME, page_icon=LOGO_PATH, layout="wide")

header_col1, header_col2 = st.columns([1, 10], vertical_alignment="center")
with header_col1:
    st.image(LOGO_PATH, width=56)
with header_col2:
    st.markdown(
        f'<div style="line-height:1.2;">'
        f'<div style="font-size:1.3rem; font-weight:700;">{APP_NAME}</div>'
        f'<div style="color:#6b7280;">Your AI SQL Agent</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

with st.chat_message("assistant", avatar="✏️"):
    st.markdown(
        f"Hi, I'm {APP_NAME} 🔍 — I turn your question and a database into "
        f"a SQL query, run it, then critique my own first draft and refine "
        f"it. **Pick a database in the sidebar and ask away.**"
    )

if not (os.getenv("OPENAI_API_KEY") or os.getenv("ANTHROPIC_API_KEY")):
    st.warning(
        "No API key found in the environment (e.g. OPENAI_API_KEY). "
        "Add one to a .env file next to SQL_agent_app.py before running a query.",
        icon="⚠️",
    )

with st.expander(f"ℹ️ How does {APP_NAME} work?", expanded=False):
    render_flow_diagram()
    st.markdown(
        f"""
{APP_NAME} follows a **reflection pattern**: it doesn't just write SQL
once — it critiques its own output and improves it.

1. **Write** — the model receives your question and the database schema,
   and drafts a SQL query (V1).
2. **Execute (V1)** — that query runs immediately against the database,
   producing a first-draft result (or an error).
3. **Reflect** — a second pass looks at the *actual output or error*
   alongside your original question, and writes feedback plus an
   improved query.
4. **Execute (V2)** — the refined query runs, producing the final answer.
"""
    )

with st.sidebar:
    st.header("Database")
    db_source = st.radio(
        "Source",
        ["Generate a sample transactions DB", "Upload a .db / .sqlite file"],
        index=0,
    )

    db_path = None
    if db_source == "Generate a sample transactions DB":
        col1, col2 = st.columns(2)
        n_products = col1.number_input("Products", min_value=5, max_value=1000, value=100, step=5)
        n_txns = col2.number_input("Txns/product", min_value=5, max_value=500, value=50, step=5)
        if st.button("Generate / regenerate"):
            with st.spinner("Creating transactions.db..."):
                utils.create_transactions_db(DEFAULT_DB, n_products=n_products, n_txns_per_product=n_txns)
            st.success(f"Created {os.path.basename(DEFAULT_DB)} "
                       f"({n_products} products × ~{n_txns} events each).")
        if os.path.exists(DEFAULT_DB):
            db_path = DEFAULT_DB
            st.caption(f"Using {os.path.basename(DEFAULT_DB)}")
        else:
            st.info("Click 'Generate / regenerate' to create the sample database.")
    else:
        uploaded_db = st.file_uploader("SQLite database file", type=["db", "sqlite", "sqlite3"])
        if uploaded_db is not None:
            local_path = os.path.join(APP_DIR, f"_uploaded_{uploaded_db.name}")
            with open(local_path, "wb") as f:
                f.write(uploaded_db.getvalue())
            db_path = local_path
            st.caption(f"Using uploaded file: {uploaded_db.name}")
            st.caption("Note: the schema viewer targets a `transactions` table specifically.")

    st.divider()
    st.header("Models")
    model_generation = st.selectbox("Generation model (writes V1)", MODEL_OPTIONS, index=0)
    model_evaluation = st.selectbox("Evaluation model (reflects & refines to V2)", MODEL_OPTIONS, index=1)
    st.caption("Model IDs follow aisuite's `provider:model` format.")

    st.divider()
    if st.button("View schema"):
        if db_path:
            try:
                st.code(utils.get_schema(db_path), language="text")
            except Exception as exc:  # noqa: BLE001
                st.error(f"Could not read schema: {exc}")
        else:
            st.info("Generate or upload a database first.")

    st.divider()
    if st.button("Clear conversation"):
        st.session_state.chat_turns = []
        st.rerun()

# ------------------------------------------------------- chat: keep going

if "chat_turns" not in st.session_state:
    st.session_state.chat_turns = []  # list of turn dicts, most recent last

st.subheader("💬 Chat")
st.caption(
    "Ask a new question, or keep going on the last one — "
    "e.g. \"now break that down by month\" or \"only show the top 5\"."
)

if not st.session_state.chat_turns:
    st.markdown("**Try one of these:**")
    ex_cols = st.columns(len(EXAMPLE_QUESTIONS))
    example_clicked = None
    for col, ex in zip(ex_cols, EXAMPLE_QUESTIONS):
        if col.button(ex, use_container_width=True, disabled=not db_path):
            example_clicked = ex
else:
    example_clicked = None

for turn in st.session_state.chat_turns:
    with st.chat_message("user"):
        st.markdown(turn["question"])
    with st.chat_message("assistant", avatar="✏️"):
        render_turn(turn)

prompt = st.chat_input("Ask a question about your data...", disabled=not db_path)
new_message = prompt or example_clicked

if new_message:
    with st.chat_message("user"):
        st.markdown(new_message)

    prior_turn = st.session_state.chat_turns[-1] if st.session_state.chat_turns else None
    contextual_question = build_contextual_question(prior_turn, new_message)

    with st.chat_message("assistant", avatar="✏️"):
        try:
            with st.spinner("Writing SQL, running it, and refining..."):
                turn = run_pipeline(
                    contextual_question, new_message, db_path,
                    model_generation, model_evaluation,
                )
            render_turn(turn)
            st.session_state.chat_turns.append(turn)
        except Exception:  # noqa: BLE001 - show the full traceback instead of a silent failure
            st.error("Something went wrong running the workflow.")
            st.code(traceback.format_exc(), language="text")
