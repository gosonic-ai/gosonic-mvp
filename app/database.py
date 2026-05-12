import psycopg

from app.config import DATABASE_URL


def get_database_url():
    return DATABASE_URL


def get_connection():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL not configured")

    return psycopg.connect(DATABASE_URL)