"""
Central configuration: loads DB credentials from .env so nothing is hardcoded.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "bankscope")
DB_USER = os.getenv("DB_USER", "bankscope_user")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

SQLALCHEMY_DATABASE_URL = (
    f"postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
)

BANKS = {
    "hdfc_bank": "HDFC Bank",
    "icici_bank": "ICICI Bank",
    "sbi": "State Bank of India",
    "axis_bank": "Axis Bank",
    "kotak_mahindra": "Kotak Mahindra Bank",
}

RAW_REPORTS_DIR = Path(__file__).resolve().parent.parent / "data" / "raw_reports"
