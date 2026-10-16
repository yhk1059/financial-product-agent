"""PostgreSQL 읽기 전용 접속과 쿼리 실행."""

from contextlib import contextmanager
from typing import Any, Iterator

import psycopg

from .config import settings


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    """PG* 환경 변수(reader 계정)로 읽기 전용 세션을 연다."""
    with psycopg.connect(connect_timeout=10, autocommit=True) as conn:
        conn.execute("SET default_transaction_read_only = on")
        conn.execute(f"SET statement_timeout = {int(settings.statement_timeout_ms)}")
        conn.execute("SET search_path = public")
        yield conn


def fetch(sql: str, params: Any = None) -> tuple[list[str], list[tuple]]:
    """쿼리를 실행하고 (컬럼명, 행 목록)을 반환한다."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        columns = [d.name for d in cur.description] if cur.description else []
        return columns, cur.fetchall() if cur.description else []
