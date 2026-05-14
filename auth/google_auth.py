import os
import logging
import secrets
from datetime import datetime, timedelta
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from sqlalchemy import text
from database.db import get_engine
import streamlit as st

logging.basicConfig(
    filename="hr_chatbot.log",
    level=logging.ERROR,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

GOOGLE_CLIENT_ID = st.secrets["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = st.secrets["GOOGLE_CLIENT_SECRET"]

def get_redirect_uri():
    try:
        return st.secrets["GOOGLE_REDIRECT_URI"]
    except:
        return os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8501")

def get_google_auth_url():
    base_url = "https://accounts.google.com/o/oauth2/v2/auth"
    params = (
        f"?client_id={GOOGLE_CLIENT_ID}"
        f"&redirect_uri={get_redirect_uri()}"
        f"&response_type=code"
        f"&scope=openid%20email%20profile"
        f"&access_type=offline"
    )
    return base_url + params

def exchange_code_for_token(code: str):
    import requests
    try:
        response = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": get_redirect_uri(),
                "grant_type": "authorization_code",
            }
        )
        return response.json()
    except Exception as e:
        logging.error(f"Token exchange failed: {e}")
        return None

def get_user_email_from_token(token_data: dict):
    import requests
    try:
        access_token = token_data.get("access_token")
        if not access_token:
            logging.error("No access_token in token_data")
            return None
        response = requests.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"}
        )
        user_info = response.json()
        return user_info.get("email")
    except Exception as e:
        logging.error(f"get_user_email_from_token failed: {e}")
        return None

def get_user_account(email: str):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            result = conn.execute(text("""
                SELECT emp_no, role FROM user_accounts
                WHERE email = :email
            """), {"email": email})
            row = result.fetchone()
            if row:
                return {
                    "emp_no": row.emp_no,   # will be None for admin — that's fine
                    "role": row.role
                }
            return None
    except Exception as e:
        logging.error(f"get_user_account failed for {email}: {e}")
        return None

def create_session(emp_no, email: str, role: str):
    """
    emp_no can be None for admin users.
    Make sure the sessions table allows NULL emp_no:
        ALTER TABLE sessions ALTER COLUMN emp_no DROP NOT NULL;
    """
    try:
        session_id = secrets.token_hex(32)
        expires_at = datetime.now() + timedelta(hours=8)
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO sessions (session_id, emp_no, email, role, expires_at)
                VALUES (:session_id, :emp_no, :email, :role, :expires_at)
            """), {
                "session_id": session_id,
                "emp_no": emp_no,       # None is fine if column is nullable
                "email": email,
                "role": role,
                "expires_at": expires_at
            })
            conn.commit()
        return session_id
    except Exception as e:
        logging.error(f"create_session failed for {email}: {e}")
        return None

def validate_session(session_id: str):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            result = conn.execute(text("""
                SELECT emp_no, email, role FROM sessions
                WHERE session_id = :session_id
                AND expires_at > NOW()
            """), {"session_id": session_id})
            row = result.fetchone()
            if row:
                return {
                    "emp_no": row.emp_no,
                    "email": row.email,
                    "role": row.role
                }
            return None
    except Exception as e:
        logging.error(f"validate_session failed: {e}")
        return None

def logout(session_id: str):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("""
                DELETE FROM sessions WHERE session_id = :session_id
            """), {"session_id": session_id})
            conn.commit()
    except Exception as e:
        logging.error(f"logout failed: {e}")
