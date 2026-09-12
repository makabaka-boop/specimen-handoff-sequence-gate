"""PostgreSQL access: connection factory, idempotent schema bootstrap."""
import os
import time
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:postgres@db:5432/lab"
)

# batches.expected_seq 永远指向“下一个应使用的序号”，从 1 开始；
# 当 expected_seq = total + 1 时 state 变为 complete。
STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS batches (
        id            UUID PRIMARY KEY,
        state         TEXT NOT NULL DEFAULT 'open'
                          CHECK (state IN ('open', 'complete')),
        expected_seq  INTEGER NOT NULL DEFAULT 1,
        total         INTEGER NOT NULL CHECK (total BETWEEN 2 AND 20),
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS batch_items (
        batch_id  UUID NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
        position  INTEGER NOT NULL,
        barcode   TEXT NOT NULL,
        PRIMARY KEY (batch_id, position),
        UNIQUE (batch_id, barcode)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS confirmations (
        id            BIGSERIAL PRIMARY KEY,
        batch_id      UUID NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
        seq           INTEGER NOT NULL,
        barcode       TEXT NOT NULL,
        confirmed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (batch_id, seq),
        UNIQUE (batch_id, barcode)
    )
    """,
]


def connect() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


@contextmanager
def session() -> Iterator[psycopg.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def init_database(attempts: int = 60, delay_seconds: float = 1.0) -> None:
    """Wait for Postgres then apply DDL idempotently (api container entry)."""
    last_error: Exception | None = None
    for _ in range(attempts):
        try:
            with session() as conn:
                for statement in STATEMENTS:
                    conn.execute(statement)
                conn.commit()
            return
        except psycopg.OperationalError as exc:  # db still starting
            last_error = exc
            time.sleep(delay_seconds)
    raise RuntimeError(f"database unavailable after {attempts} attempts: {last_error}")
