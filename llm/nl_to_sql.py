import os
import time
import logging
import streamlit as st
from groq import Groq, APITimeoutError, RateLimitError
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    filename="hr_chatbot.log",
    level=logging.ERROR,
    format="%(asctime)s - %(levelname)s - %(message)s"
)


def get_groq_client():
    return Groq(api_key=st.secrets["GROQ_API_KEY"])

SCHEMA_DESCRIPTION = """
You are an expert PostgreSQL SQL assistant. Convert natural language questions to SQL queries.

DATABASE SCHEMA:
- employees(emp_no, birth_date, first_name, last_name, gender, hire_date)
- departments(dept_no, dept_name)
- dept_emp(emp_no, dept_no, from_date, to_date) - links employees to departments
- dept_manager(emp_no, dept_no, from_date, to_date) - links managers to departments
- salaries(emp_no, salary, from_date, to_date)
- titles(emp_no, title, from_date, to_date)
- leave_requests(leave_id, emp_no, leave_type, start_date, end_date, status)

IMPORTANT RULES:
- ALWAYS filter current records using to_date = '9999-01-01' in dept_emp, dept_manager, salaries, titles. This is mandatory for every query involving these tables, no exceptions.
- The employees table has NO to_date column. Never filter employees by to_date. Use hire_date directly.
- The leave_requests table has NO to_date column. Never add to_date filter on leave_requests.
- "currently on leave" means CURRENT_DATE BETWEEN leave_requests.start_date AND leave_requests.end_date. Never use to_date for this.
- leave_type values are: 'Annual', 'Sick', 'Maternity', 'Paternity', 'Unpaid'
- status values are: 'Approved', 'Pending', 'Rejected'
- gender values are: 'M', 'F'
- Return ONLY the SQL query. No explanation, no markdown, no backticks.
- Never use DROP, DELETE, UPDATE or INSERT statements.
- NEVER return SELECT 'Invalid question' AS result under any circumstances. Always attempt to generate valid SQL. If truly unable, return SELECT 'I cannot answer that question.' AS result.
"""


def _call_groq(messages: list) -> str:
    client = get_groq_client()
    try:
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages
        )
        return response.choices[0].message.content.strip()

    except APITimeoutError:
        logging.warning("Groq API timeout - retrying once after 2s backoff")
        time.sleep(2)
        try:
            response = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=messages
            )
            return response.choices[0].message.content.strip()
        except APITimeoutError:
            logging.error("Groq API timeout on retry - giving up")
            raise RuntimeError(
                "The AI service timed out. Please wait a moment and try again."
            )

    except RateLimitError as e:
        wait_time = getattr(e, 'retry_after', None)
        if wait_time:
            msg = f"Rate limit reached. Please wait {wait_time} seconds before trying again."
        else:
            msg = "Rate limit reached. Please wait a moment before trying again."
        logging.error(f"Groq rate limit hit: {e}")
        raise RuntimeError(msg)


def nl_to_sql(question: str, emp_no, is_manager: bool = False, is_admin: bool = False) -> str:
    try:
        if is_admin:
            role_instruction = (
                "The user is an ADMIN (HR Manager) with full access to all data.\n"
                "They can query ANY employee, ANY department, ANY salary — no restrictions.\n"
                "Do NOT add any emp_no or dept_no WHERE clause restrictions.\n"
                "Just write the most accurate SQL for the question asked.\n"
                "For counts, averages, and aggregates — query the entire company, no scoping.\n"
            )
        elif is_manager:
            role_instruction = (
                f"The user is a MANAGER with emp_no {emp_no}.\n\n"
                f"Their department is found via: SELECT dept_no FROM dept_manager WHERE emp_no = {emp_no} AND to_date = '9999-01-01'\n\n"
                "They have THREE types of access:\n\n"
                "1. PERSONAL queries (my salary, my leave, my title, my hire date):\n"
                f"   - Restrict using WHERE emp_no = {emp_no}\n\n"
                "2. TEAM/DEPARTMENT queries (my team, who reports to me, employees in my department, leave in my department):\n"
                f"   - Only query employees whose emp_no exists in dept_emp WHERE dept_no = (SELECT dept_no FROM dept_manager WHERE emp_no = {emp_no} AND to_date = '9999-01-01') AND to_date = '9999-01-01'\n"
                "   - NEVER query a specific dept_no that was mentioned by the user directly.\n\n"
                "3. AGGREGATE/STATISTICAL queries (count, average salary, headcount, gender breakdown, top paid):\n"
                "   - ALWAYS scope to the manager's department only.\n"
                f"   - Example: SELECT COUNT(*) FROM employees WHERE emp_no IN (SELECT emp_no FROM dept_emp WHERE dept_no = (SELECT dept_no FROM dept_manager WHERE emp_no = {emp_no} AND to_date = '9999-01-01') AND to_date = '9999-01-01')\n"
                f"   - NEVER write a query without a WHERE clause. Every single query must be scoped.\n"
                f"   - If the question contains any emp_no number that is not {emp_no}, return: SELECT 'Access denied: cannot query specific employee data outside your department.' AS result\n\n"
                "CRITICAL RULES:\n"
                f"   - EVERY query MUST have a WHERE clause scoping to the manager's department or their own emp_no.\n"
                f"   - A query with no WHERE clause is NEVER acceptable. No exceptions.\n"
                f"   - NEVER use a hardcoded emp_no other than {emp_no} in any WHERE clause.\n"
            )
        else:
            role_instruction = (
                f"The user is an EMPLOYEE with emp_no {emp_no}.\n"
                "They can ONLY query their own personal data.\n\n"
                "Apply restrictions based on the table being queried:\n"
                f"- For salaries, titles, dept_emp, dept_manager: WHERE emp_no = {emp_no} AND to_date = '9999-01-01'\n"
                f"- For employees table: WHERE emp_no = {emp_no} (NO to_date)\n"
                f"- For leave_requests: WHERE emp_no = {emp_no} (NO to_date)\n"
                "- EXCEPTION: For 'who is my manager' queries, find the manager of the employee's department:\n"
                f"  SELECT e.first_name, e.last_name FROM employees e JOIN dept_manager dm ON e.emp_no = dm.emp_no WHERE dm.dept_no = (SELECT dept_no FROM dept_emp WHERE emp_no = {emp_no} AND to_date = '9999-01-01') AND dm.to_date = '9999-01-01'\n"
                "Never return data belonging to any other employee except for manager lookup.\n"
                f"If the question references any emp_no other than {emp_no}, return: SELECT 'Access denied: you can only query your own data.' AS result\n"
            )

        full_prompt = f"{SCHEMA_DESCRIPTION}\n\nACCESS RESTRICTION:\n{role_instruction}\n\nQuestion: {question}"

        messages = [
            {
                "role": "system",
                "content": "You are an expert PostgreSQL query writer for an HR system. Always enforce the access restrictions given. Return ONLY the raw SQL query - no explanation, no markdown, no backticks, no 'Invalid question' responses ever."
            },
            {
                "role": "user",
                "content": full_prompt
            }
        ]

        sql = _call_groq(messages)
        sql = sql.replace("```sql", "").replace("```", "").strip()
        return sql

    except RuntimeError as e:
        logging.error(f"nl_to_sql RuntimeError | emp_no={emp_no} | question={question} | error={e}")
        return f"ERROR: {e}"

    except Exception as e:
        logging.error(f"nl_to_sql error | emp_no={emp_no} | is_manager={is_manager} | question={question} | error={e}")
        return f"ERROR: {str(e)}"








