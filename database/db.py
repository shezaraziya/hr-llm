import streamlit as st
from sqlalchemy import create_engine, text
import pandas as pd
from urllib.parse import quote_plus
import logging
import re

logging.basicConfig(
    filename="hr_chatbot.log",
    level=logging.ERROR,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

@st.cache_resource
def get_engine():
    try:
        user = quote_plus(st.secrets["DB_USER"])
        password = quote_plus(st.secrets["DB_PASSWORD"])
        host = st.secrets["DB_HOST"]
        port = st.secrets["DB_PORT"]
        db = st.secrets["DB_NAME"]

        engine = create_engine(
            f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{db}",
            connect_args={
                "sslmode": "require",
                "connect_timeout": 10,
            }
        )
        return engine
    except Exception as e:
        logging.error(f"Failed to create database engine: {e}")
        raise

def run_query(query, params=None):
    try:
        engine = get_engine()

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
