"""
migrate.py — Copy all data from Railway MySQL to Supabase PostgreSQL
Run from your hr-llm folder: python migrate.py
"""

from sqlalchemy import create_engine, text
from urllib.parse import quote_plus
import pandas as pd

# ─── SOURCE: Railway MySQL ───────────────────────────────────
MYSQL_URL = (
    "mysql+pymysql://root:sFjiKWtfgjvXfLOjczEZYwNqjGMnCBAS"
    "@yamabiko.proxy.rlwy.net:37976/railway"
)

# ─── TARGET: Supabase PostgreSQL ─────────────────────────────
PG_PASSWORD = quote_plus("Hr@Llm#2026$Supa!")
PG_URL = (
    f"postgresql+psycopg2://postgres.mtpphiehgobdxwmtcjdn:{PG_PASSWORD}"
    "@aws-1-ap-southeast-1.pooler.supabase.com:5432/postgres"
)

# Tables in order (respects foreign keys)
TABLES = [
    "employees",
    "departments",
    "dept_emp",
    "dept_manager",
    "salaries",
    "titles",
    "leave_requests",
    "user_accounts",
    "sessions",
    "audit_log",
]

def migrate():
    print("Connecting to Railway MySQL...")
    mysql_engine = create_engine(MYSQL_URL)

    print("Connecting to Supabase PostgreSQL...")
    pg_engine = create_engine(PG_URL)

    for table in TABLES:
        try:
            print(f"\n→ Migrating {table}...", end=" ")
            df = pd.read_sql(f"SELECT * FROM {table}", mysql_engine)
            if df.empty:
                print(f"empty, skipping.")
                continue

            df.to_sql(
                table,
                pg_engine,
                if_exists="append",
                index=False,
                method="multi",
                chunksize=500
            )
            print(f"done! ({len(df)} rows)")

        except Exception as e:
            print(f"FAILED: {e}")

    print("\n✅ Migration complete!")

if __name__ == "__main__":
    migrate()
