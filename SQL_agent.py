# SQL Agent — reflection pattern (generate -> execute -> reflect -> refine -> execute)
# Targets the single event-sourced 'transactions' table (inserts / restocks / sales /
# price_updates) produced by utils.create_transactions_db, via utils.get_schema /
# utils.execute_sql / utils.print_html.

import json
import re
import utils
import pandas as pd
from dotenv import load_dotenv

_ = load_dotenv()


import aisuite as ai
client = ai.Client()


def _extract_json_object(content: str) -> dict:
    """
    Parse a model response that is supposed to be a strict JSON object.
    Models asked for "strict JSON" very often wrap it in a ```json ... ```
    fence (or add a stray sentence around it) even when told not to, which
    breaks a plain json.loads() and silently discards whatever the model
    actually said. Be liberal about finding the real JSON object before
    giving up.
    """
    text = content.strip()

    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Last resort: grab the first {...} block in the response and try that.
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if match:
        return json.loads(match.group(0))

    raise ValueError("No JSON object found in model response")


# SQL Generator by LLM
def generate_sql(question: str, schema: str, model: str) -> str:
    prompt = f"""
    You are a SQL assistant. Given the schema and the user's question, write a SQL query for SQLite.

    Schema:
    {schema}

    User question:
    {question}

    Be careful with any column that can hold signed values (e.g. a delta
    or adjustment that is negative for one kind of event and positive for
    another): decide whether you need to negate it or wrap it in ABS()
    based on what the question is actually asking for, and make sure your
    ORDER BY direction matches the ranking you intend *after* that
    transformation — "the most" of something should never resolve to a
    negative number sitting close to zero.

    Respond with the SQL only.
    """
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
    )
    return response.choices[0].message.content.strip()




# Review and refine the generated SQL
def refine_sql_external_feedback(
    question: str,
    sql_query: str,
    df_feedback: pd.DataFrame,
    schema: str,
    model: str,
) -> tuple[str, str]:
    """
    Evaluate whether the SQL result answers the user's question and,
    if necessary, propose a refined version of the query.
    Returns (feedback, refined_sql).
    """
    prompt = f"""
    You are a SQL reviewer and refiner.

    User asked:
    {question}

    Original SQL:
    {sql_query}

    SQL Output:
    {df_feedback.to_markdown(index=False)}

    Table Schema:
    {schema}

    Step 1: Briefly evaluate if the SQL output answers the user's question.
    Step 2: Sanity-check the result itself — if the question asks for a
    count, total, or "most/least" of something and the output is negative,
    zero, or the ranking looks inverted, that usually signals a sign or
    aggregation error in the query (e.g. a signed delta/adjustment column
    that should have been negated or wrapped in ABS()). Treat that as
    incorrect even though the query ran without an error.
    Step 3: If the SQL could be improved, provide a refined SQL query.
    If the original SQL is already correct, return it unchanged.

    Return a strict JSON object with two fields:
    - "feedback": brief evaluation and suggestions
    - "refined_sql": the final SQL to run
    """

    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=1.0,
    )


    content = response.choices[0].message.content
    try:
        obj = _extract_json_object(content)
        feedback = str(obj.get("feedback", "")).strip()
        refined_sql = str(obj.get("refined_sql", sql_query)).strip()
        if not refined_sql:
            refined_sql = sql_query
    except Exception:
        # The model truly didn't return a parseable JSON object (rare once
        # fences are handled above). Say so explicitly rather than dumping
        # raw JSON text as if it were human feedback, and keep the
        # original SQL since there's no reliable refined query to use.
        feedback = (
            "Could not parse a structured refinement from the model's "
            "response; keeping the original SQL unchanged. Raw response: "
            + content.strip()
        )
        refined_sql = sql_query

    return feedback, refined_sql



# SQL AI Agent
def run_sql_workflow(
    db_path: str,
    question: str,
    model_generation: str = "openai:gpt-4.1-mini",
    model_evaluation: str = "openai:gpt-4.1",
):
    """
    End-to-end workflow to generate, execute, evaluate, and refine SQL queries.

    Steps:
      1) Extract database schema
      2) Generate SQL (V1)
      3) Execute V1 → show output
      4) Reflect on V1 with execution feedback → propose refined SQL (V2)
      5) Execute V2 → show final answer
    """

    # 1) Schema
    schema = utils.get_schema(db_path)
    utils.print_html(
        schema,
        title="📘 Step 1 — Extract Database Schema"
    )

    # 2) Generate SQL (V1)
    sql_v1 = generate_sql(question, schema, model_generation)
    utils.print_html(
        sql_v1,
        title="🧠 Step 2 — Generate SQL (V1)"
    )

    # 3) Execute V1
    df_v1 = utils.execute_sql(sql_v1, db_path)
    utils.print_html(
        df_v1,
        title="🧪 Step 3 — Execute V1 (SQL Output)"
    )

    # 4) Reflect on V1 with execution feedback → refine to V2
    feedback, sql_v2 = refine_sql_external_feedback(
        question=question,
        sql_query=sql_v1,
        df_feedback=df_v1,          # external feedback: real output of V1
        schema=schema,
        model=model_evaluation,
    )
    utils.print_html(
        feedback,
        title="🧭 Step 4 — Reflect on V1 (Feedback)"
    )
    utils.print_html(
        sql_v2,
        title="🔁 Step 4 — Refined SQL (V2)"
    )

    # 5) Execute V2
    df_v2 = utils.execute_sql(sql_v2, db_path)
    utils.print_html(
        df_v2,
        title="✅ Step 5 — Execute V2 (Final Answer)"
    )
