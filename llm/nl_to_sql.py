import os
import logging
from datetime import datetime
from groq import Groq
from dotenv import load_dotenv

load_dotenv()

# Setup error logging to file with timestamp
logging.basicConfig(
    filename="hr_chatbot.log",
    level=logging.ERROR,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# Initialize Groq client
try:
    client = Groq(api_key=os.getenv("GROQ_API_KEY"))
except Exception as e:
    logging.error(f"Failed to initialize Groq client: {e}")
    raise

SCHEMA_DESCRIPTION = """
The database has these tables:
- employees (emp_no, birth_date, first_name, last_name, gender, hire_date)
- departments (dept_no, dept_name)
- dept_emp (emp_no, dept_no, from_date, to_date)
- dept_manager (dept_no, emp_no, from_date, to_date)
- salaries (emp_no, salary, from_date, to_date)
- titles (emp_no, title, from_date, to_date)
- leave_records (emp_no, leave_date, leave_type, status)
"""

def nl_to_sql(question: str, emp_no: int, is_manager: bool = False) -> str:
    """
    Converts a natural language question to SQL.
    Enforces role-based access:
    - Employees can only query their own data
    - Managers can query their own data and their staff's data
    """
    try:
        # Build role-based restriction instructions
        if is_manager:
            role_instruction = f"""The user is a MANAGER with emp_no {emp_no}.
They can query their own data and data of employees who report to them.
Always restrict queries using:
WHERE de.emp_no = {emp_no} (for their own data)
OR emp_no IN (SELECT emp_no FROM dept_emp WHERE dept_no IN 
    (SELECT dept_no FROM dept_manager WHERE emp_no = {emp_no}))
"""
        else:
            role_instruction = f"""The user is an EMPLOYEE with emp_no {emp_no}.
They can ONLY query their own data.
Always add this restriction to every query: WHERE emp_no = {emp_no}
Never allow access to other employees' data.
"""

        prompt = f"""You are an SQL expert for an HR system. Given this database schema:
{SCHEMA_DESCRIPTION}

Access rules:
{role_instruction}

Convert this question to a valid MySQL query following the access rules strictly.
Return ONLY the SQL query, nothing else.

Question: {question}"""

        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": "You are an expert MySQL query writer for an HR system. Always enforce data access restrictions. Return only the SQL query with no explanation."},
                {"role": "user", "content": prompt}
            ]
        )

        sql = response.choices[0].message.content.strip()
        sql = sql.replace("```sql", "").replace("```", "").strip()
        return sql

    except Exception as e:
        logging.error(f"nl_to_sql failed | emp_no={emp_no} | question={question} | error={e}")
        raise Exception("Sorry, I was unable to process your question. Please try again or contact support.")


if __name__ == "__main__":
    # Test as regular employee
    print("=== Testing as Employee ===")
    print(nl_to_sql("What is my salary?", emp_no=10001, is_manager=False))

    # Test as manager
    print("\n=== Testing as Manager ===")
    print(nl_to_sql("How many employees are in my department?", emp_no=10001, is_manager=True))

