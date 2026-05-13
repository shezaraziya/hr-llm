import streamlit as st
from sqlalchemy import create_engine, text
import pandas as pd
from dotenv import load_dotenv
from urllib.parse import quote_plus
import os
import logging
import re


load_dotenv()

logging.basicConfig(
    filename="hr_chatbot.log",
    level=logging.ERROR,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

@st.cache_resource
def get_engine():
    try:
        user = os.getenv("DB_USER", "hr_app")
        password = quote_plus(os.getenv("DB_PASSWORD"))
        host = os.getenv("DB_HOST", "localhost")
        port = os.getenv("DB_PORT", "3306")
        db = os.getenv("DB_NAME", "employees")
        engine = create_engine(
            f"mysql+mysqlconnector://{user}:{password}@{host}:{port}/{db}",
            connect_args={
                "connection_timeout": 10,
                "read_timeout": 30,
            }
        )
        return engine
    except Exception as e:
        logging.error(f"Failed to create database engine: {e}")
        raise

def run_query(query, params=None):
    try:
        engine = get_engine()
        
        # Safety net — strip any existing LIMIT and enforce LIMIT 500
        query_stripped = query.strip().rstrip(';')
        
        if not re.search(r'\bLIMIT\b', query_stripped, re.IGNORECASE):
            query_stripped += ' LIMIT 500'
        
        with engine.connect() as conn:
            if params:
                param_dict = {f"param_{i}": v for i, v in enumerate(params)}
                query_stripped = query_stripped.replace("%s", ":param_0")
                df = pd.read_sql(text(query_stripped), conn, params=param_dict)
            else:
                df = pd.read_sql(text(query_stripped), conn)
        return df
    except Exception as e:
        logging.error(f"run_query error | query={query} | params={params} | error={e}")
        raise
