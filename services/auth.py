import os
import secrets
import hashlib

import psycopg
from dotenv import load_dotenv


load_dotenv(override=True)

POSTGRES_URL = os.getenv("POSTGRES_URL")

if not POSTGRES_URL:
    raise RuntimeError(
        "POSTGRES_URL is not set. Add it to your .env file or environment."
    )


def _hash_api_key(api_key):
    return hashlib.sha256(api_key.encode()).hexdigest()


def check_postgres_connection():
    """Return True if PostgreSQL is reachable and accepts a query."""
    try:
        with psycopg.connect(
            POSTGRES_URL,
            connect_timeout=3,
        ) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()

        return True

    except Exception:
        return False


def _add_key_table():
    with psycopg.connect(POSTGRES_URL) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS api_keys (
                    id BIGSERIAL PRIMARY KEY,
                    key_hash TEXT UNIQUE NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )


def _save_key_to_db(api_key):
    key_hash = _hash_api_key(api_key)

    with psycopg.connect(POSTGRES_URL) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO api_keys (key_hash)
                VALUES (%s)
                """,
                (key_hash,),
            )


def is_valid_api_key(api_key):
    """Return True if the supplied API key exists in PostgreSQL."""
    if not api_key:
        return False

    key_hash = _hash_api_key(api_key)

    with psycopg.connect(POSTGRES_URL) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1
                FROM api_keys
                WHERE key_hash = %s
                LIMIT 1
                """,
                (key_hash,),
            )

            return cursor.fetchone() is not None


def generate_api_key():
    api_key = f"atna_{secrets.token_hex(32)}"

    _add_key_table()
    _save_key_to_db(api_key)

    return api_key


if __name__ == "__main__":
    api_key = generate_api_key()
    print(f"Generated API key: {api_key}")
