import streamlit as st
import sys
import os
import re
import logging
from groq import Groq
from dotenv import load_dotenv
from sqlalchemy.exc import ProgrammingError
from collections import deque
from datetime import datetime, timedelta

load_dotenv()

# ─────────────────────────────────────────────
# LOGGING SETUP
# ─────────────────────────────────────────────
logging.basicConfig(
    filename="chatbot.log",
    level=logging.ERROR,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

sys.path.append(os.path.dirname(__file__))
from auth.google_auth import (
    get_google_auth_url,
    exchange_code_for_token,
    get_user_email_from_token,
    get_user_account,
    create_session,
    validate_session,
    logout
)
from llm.nl_to_sql import nl_to_sql
from database.db import run_query

# ─────────────────────────────────────────────
# RATE LIMITER
# ─────────────────────────────────────────────
RATE_LIMIT = 20  # max questions per minute per user

def is_rate_limited(emp_no: int) -> bool:
    now = datetime.now()
    window = timedelta(minutes=1)

    if "rate_limit_log" not in st.session_state:
        st.session_state["rate_limit_log"] = {}

    log = st.session_state["rate_limit_log"]

    if emp_no not in log:
        log[emp_no] = deque()

    while log[emp_no] and now - log[emp_no][0] > window:
        log[emp_no].popleft()

    if len(log[emp_no]) >= RATE_LIMIT:
        return True

    log[emp_no].append(now)
    return False

# ─────────────────────────────────────────────
# SESSION CHECK
# ─────────────────────────────────────────────
def get_current_session():
    session_id = st.session_state.get("session_id")
    if not session_id:
        return None
    session = validate_session(session_id)
    if not session:
        st.session_state.pop("session_id", None)
        return None
    return session

# ─────────────────────────────────────────────
# LOGIN PAGE
# ─────────────────────────────────────────────
def show_login():
    st.title("🔐 HR Chatbot Login")
    st.markdown("Please log in with your Google account to continue.")

    params = st.query_params
    code = params.get("code")

    if code:
        with st.spinner("Verifying your Google account..."):
            try:
                token_data = exchange_code_for_token(code)
                if not token_data:
                    st.error("❌ Failed to get token from Google. Please try again.")
                    return

                email = get_user_email_from_token(token_data)
                if not email:
                    st.error("❌ Could not verify your Google account.")
                    return

                user = get_user_account(email)
                if not user:
                    st.error(f"❌ Your email ({email}) is not registered in the HR system. Contact your administrator.")
                    return

                session_id = create_session(
                    emp_no=user["emp_no"],
                    email=email,
                    role=user["role"]
                )
                if not session_id:
                    st.error("❌ Failed to create session. Please try again.")
                    return

                st.session_state["session_id"] = session_id
                st.session_state["emp_no"] = user["emp_no"]
                st.session_state["role"] = user["role"]
                st.session_state["email"] = email

                st.query_params.clear()
                st.rerun()

            except Exception as e:
                st.error(f"❌ Login failed: {str(e)}")
    else:
       auth_url = get_google_auth_url()
       st.link_button("🔵 Login with Google", auth_url)

# ─────────────────────────────────────────────
# INTENT DETECTION
# ─────────────────────────────────────────────
def is_data_question(question: str) -> bool:
    try:
        client = Groq(api_key=os.getenv("GROQ_API_KEY"))
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a classifier. Respond with only 'yes' or 'no'. "
                        "Does the following message require querying an HR database to answer? "
                        "Be generous — if the message is asking about employees, salaries, "
                        "departments, leave, or any HR topic, answer 'yes'."
                    )
                },
                {"role": "user", "content": question}
            ]
        )
        answer = response.choices[0].message.content.strip().lower()
        return answer == "yes"
    except Exception as e:
        logging.error(f"Intent detection error | question={question} | error={e}")
        return True

# ─────────────────────────────────────────────
# SQL SECURITY VALIDATOR
# ─────────────────────────────────────────────
def validate_sql(sql: str, emp_no: int, is_manager: bool) -> tuple[bool, str]:
    sql_upper = sql.upper()

    blocked_keywords = [
        "DROP", "DELETE", "UPDATE", "INSERT",
        "ALTER", "TRUNCATE", "CREATE", "REPLACE"
    ]
    for keyword in blocked_keywords:
        if keyword in sql_upper:
            return False, "Access denied: write operations are not allowed."

    if "INFORMATION_SCHEMA" in sql_upper or "SHOW TABLES" in sql_upper:
        return False, "Access denied: schema inspection is not allowed."

    numbers_in_sql = re.findall(r'\b(\d{5})\b', sql)

    if not is_manager:
        for num in numbers_in_sql:
            if int(num) != emp_no:
                return False, "Access denied: you can only query your own data."
    else:
        for num in numbers_in_sql:
            if int(num) != emp_no:
                return False, "Access denied: cannot query specific employee data outside your department."

    return True, ""

# ─────────────────────────────────────────────
# AUDIT LOG
# ─────────────────────────────────────────────
def log_audit(emp_no: int, email: str, question: str, sql: str, row_count: int, was_blocked: bool, block_reason: str = None):
    try:
        from database.db import get_engine
        from sqlalchemy import text
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO audit_log (emp_no, email, question, sql_generated, row_count, was_blocked, block_reason)
                VALUES (:emp_no, :email, :question, :sql_generated, :row_count, :was_blocked, :block_reason)
            """), {
                "emp_no": emp_no,
                "email": email,
                "question": question,
                "sql_generated": sql,
                "row_count": row_count,
                "was_blocked": was_blocked,
                "block_reason": block_reason
            })
            conn.commit()
    except Exception as e:
        logging.error(f"Audit log failed | emp_no={emp_no} | error={e}")
# ─────────────────────────────────────────────
# CHAT INTERFACE
# ─────────────────────────────────────────────
def show_chat(session):
    emp_no = session["emp_no"]
    role = session["role"]
    email = session["email"]
    is_manager = role == "manager"

    st.set_page_config(page_title="HR Chatbot", page_icon="🤖")
    st.title("🤖 HR AI Chatbot")
    st.markdown(f"Logged in as **{email}** | {'👔 Manager' if is_manager else '👤 Employee'}")

    with st.sidebar:
        st.markdown(f"**Email:** {email}")
        st.markdown(f"**Emp No:** {emp_no}")
        st.markdown(f"**Role:** {role.capitalize()}")
        if st.button("🚪 Logout"):
            logout(st.session_state["session_id"])
            for key in ["session_id", "emp_no", "role", "email"]:
                st.session_state.pop(key, None)
            st.rerun()

    st.markdown("---")

    if "messages" not in st.session_state:
        st.session_state.messages = []

    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
            if "dataframe" in message:
                st.dataframe(message["dataframe"])
            if "sql" in message:
                with st.expander("Generated SQL"):
                    st.code(message["sql"], language="sql")

    if prompt := st.chat_input("Ask a question about HR data..."):
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            with st.spinner("Thinking..."):
                if is_rate_limited(emp_no):
                    st.warning("You are sending too many questions. Please wait a moment before trying again.")
                    st.stop()

                else:
                    try:
                        if not is_data_question(prompt):
                            msg = (
                                "Hello! I'm your HR assistant. "
                                "Ask me anything about employees, salaries, departments or leave data."
                            )
                            st.markdown(msg)
                            st.session_state.messages.append({
                                "role": "assistant",
                                "content": msg
                            })

                        else:
                            sql = nl_to_sql(prompt, emp_no=emp_no, is_manager=is_manager)

                            if sql.startswith("ERROR:"):
                                logging.error(
                                    f"Groq error | emp_no={emp_no} | question={prompt} | error={sql}"
                                )
                                st.error(f"⚠️ {sql}")
                                st.session_state.messages.append({
                                    "role": "assistant",
                                    "content": f"⚠️ {sql}"
                                })

                            else:
                                is_safe, reason = validate_sql(
                                    sql, emp_no=emp_no, is_manager=is_manager
                                )

                                if not is_safe:
                                    logging.error(
                                        f"Security block | emp_no={emp_no} | question={prompt} | sql={sql} | reason={reason}"
                                    )
                                    st.warning(reason)
                                    log_audit(emp_no, email, prompt, sql, 0, True, reason)
                                    st.session_state.messages.append({
                                        "role": "assistant",
                                        "content": reason,
                                        "sql": sql
                                    })

                                else:
                                    try:
                                        df = run_query(sql)

                                        if df is None:
                                            msg = "⚠️ A database error occurred. Please try again later."
                                            logging.error(f"DB returned None | emp_no={emp_no} | sql={sql}")
                                            st.error(msg)
                                            st.session_state.messages.append({
                                                "role": "assistant",
                                                "content": msg,
                                                "sql": sql
                                            })

                                        elif df.empty:
                                            msg = "No results found for your question."
                                            st.markdown(msg)
                                            st.session_state.messages.append({
                                                "role": "assistant",
                                                "content": msg,
                                                "sql": sql
                                            })

                                        else:
                                            st.markdown("Here are the results:")
                                            st.dataframe(df)
                                            log_audit(emp_no, email, prompt, sql, len(df), False)
                                            with st.expander("Generated SQL"):
                                                st.code(sql, language="sql")
                                            st.session_state.messages.append({
                                                "role": "assistant",
                                                "content": "Here are the results:",
                                                "dataframe": df,
                                                "sql": sql
                                            })

                                    except ProgrammingError as e:
                                        logging.error(
                                            f"ProgrammingError | emp_no={emp_no} | question={prompt} | sql={sql} | error={e}"
                                        )
                                        msg = (
                                            "⚠️ I couldn't generate a valid query for that question. "
                                            "Try rephrasing it."
                                        )
                                        st.warning(msg)
                                        st.session_state.messages.append({
                                            "role": "assistant",
                                            "content": msg,
                                            "sql": sql
                                        })

                    except Exception as e:
                        logging.error(
                            f"Unhandled error | emp_no={emp_no} | question={prompt} | error={e}"
                        )
                        msg = "Sorry, I couldn't process your question. Please try rephrasing it."
                        st.error(msg)
                        st.session_state.messages.append({
                            "role": "assistant",
                            "content": msg
                        })

# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────
session = get_current_session()

if session is None:
    show_login()
else:
    show_chat(session)