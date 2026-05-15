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
RATE_LIMIT = 20

def is_rate_limited(user_key: str) -> bool:
    now = datetime.now()
    window = timedelta(minutes=1)

    if "rate_limit_log" not in st.session_state:
        st.session_state["rate_limit_log"] = {}

    log = st.session_state["rate_limit_log"]

    if user_key not in log:
        log[user_key] = deque()

    while log[user_key] and now - log[user_key][0] > window:
        log[user_key].popleft()

    if len(log[user_key]) >= RATE_LIMIT:
        return True

    log[user_key].append(now)
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
# HR QUESTION CLASSIFIER
# ─────────────────────────────────────────────
HR_KEYWORDS = [
    "employee", "employees", "salary", "salaries", "department", "departments",
    "manager", "hire", "hired", "title", "gender", "count", "how many", "who is",
    "average", "total", "tenure", "staff", "headcount", "payroll", "leave",
    "promotion", "top paid", "highest", "lowest", "report", "team", "my "
]

def is_hr_question(question: str) -> bool:
    q = question.lower()
    return any(kw in q for kw in HR_KEYWORDS)

# ─────────────────────────────────────────────
# COMPANY-WIDE QUERY DETECTOR
# ─────────────────────────────────────────────
COMPANY_WIDE_PHRASES = [
    "company", "entire company", "all employees", "whole company",
    "organization", "total employees", "company-wide", "everyone", "across all"
]

def is_company_wide_query(question: str) -> bool:
    q = question.lower()
    return any(phrase in q for phrase in COMPANY_WIDE_PHRASES)

# ─────────────────────────────────────────────
# SQL SECURITY VALIDATOR
# ─────────────────────────────────────────────
def validate_sql(sql: str, emp_no: int, is_manager: bool, is_admin: bool = False) -> tuple[bool, str]:
    sql_upper = sql.upper()

    # Block all write operations
    blocked_keywords = ["DROP", "DELETE", "UPDATE", "INSERT",
                        "ALTER", "TRUNCATE", "CREATE", "REPLACE"]
    for keyword in blocked_keywords:
        if keyword in sql_upper:
            return False, "Access denied: write operations are not allowed."

    # Block schema inspection
    if "INFORMATION_SCHEMA" in sql_upper or "SHOW TABLES" in sql_upper:
        return False, "Access denied: schema inspection is not allowed."

    # Admins pass all checks
    if is_admin:
        return True, ""

    # All non-admins must have a WHERE clause
    if "WHERE" not in sql_upper:
        return False, "Access denied: query must be scoped to your data."

    # Allow manager lookup queries for everyone
    is_manager_lookup = "dept_manager" in sql.lower() and "dept_emp" in sql.lower()
    if is_manager_lookup:
        return True, ""

    # Employees only: every number in the SQL must match their own emp_no
    if not is_manager:
        numbers_in_sql = re.findall(r'\b(\d{4,6})\b', sql)
        for num in numbers_in_sql:
            if int(num) != emp_no:
                return False, "Access denied: you can only query your own data."

    # Managers: ensure query is scoped to their emp_no or department
    if is_manager:
        if str(emp_no) not in sql:
            return False, "Access denied: query must be scoped to your department."

    return True, ""

# ─────────────────────────────────────────────
# AUDIT LOG
# ─────────────────────────────────────────────
def log_audit(emp_no, email: str, question: str, sql: str, row_count: int, was_blocked: bool, block_reason: str = None):
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
    is_admin = role == "admin"
    is_manager = role == "manager"

    role_label = "🔑 Admin" if is_admin else ("👔 Manager" if is_manager else "👤 Employee")

    st.set_page_config(page_title="HR Chatbot", page_icon="🤖")
    st.title("🤖 HR AI Chatbot")
    st.markdown(f"Logged in as **{email}** | {role_label}")

    with st.sidebar:
        st.markdown(f"**Email:** {email}")
        st.markdown(f"**Emp No:** {emp_no if emp_no else 'N/A (Admin)'}")
        st.markdown(f"**Role:** {role.capitalize()}")
        if is_admin:
            st.success("🔑 Full access — all departments")
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
                if is_rate_limited(email):
                    st.warning("You are sending too many questions. Please wait a moment before trying again.")
                    st.stop()

                else:
                    try:
                        # ── Non-HR question check ──
                        if not is_hr_question(prompt):
                            msg = (
                                "🙋 I'm your HR assistant — I can only help with questions about "
                                "employees, salaries, departments, leave, or managers.\n\n"
                                "Try asking something like:\n"
                                "- *How many employees are in my department?*\n"
                                "- *What is my current salary?*\n"
                                "- *Who is my manager?*"
                            )
                            st.info(msg)
                            st.session_state.messages.append({
                                "role": "assistant",
                                "content": msg
                            })

                        else:
                            # ── Company-wide scope notice for managers ──
                            show_scope_notice = is_manager and is_company_wide_query(prompt)

                            sql = nl_to_sql(prompt, emp_no=emp_no, is_manager=is_manager, is_admin=is_admin)

                            if sql.startswith("ERROR:") or not sql.strip().upper().startswith("SELECT"):
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
                                is_safe, reason = validate_sql(
                                    sql, emp_no=emp_no, is_manager=is_manager, is_admin=is_admin
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
                                            if show_scope_notice:
                                                st.info("ℹ️ Showing results for your department only — managers don't have company-wide access.")
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
                        msg = f"Sorry, I couldn't process your question. Error: {str(e)}"
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