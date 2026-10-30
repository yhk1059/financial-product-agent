"""PostgreSQL 읽기 전용 접속과 쿼리 실행."""

from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg import sql

from .config import ALLOWED_TABLES, settings


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


def lookup_values(table: str, column: str, text: str, mode: str, limit: int = 10) -> list[str]:
    """컬럼에 저장된 값 중 text와 맞는 것을 짧은 순으로 돌려준다 (파라미터 바인딩, 읽기 전용).

    mode: 'exact' = 정확히 같음, 'partial' = 공백·대소문자를 무시하고 text를 포함함.
    테이블·컬럼 이름은 허용 테이블과 스키마 카탈로그에 있을 때만 쓴다(문자열 조립 방지).
    """
    from .schema_catalog import load_catalog

    columns = {c["name"] for c in load_catalog()["tables"].get(table, {}).get("columns", [])}
    if table not in ALLOWED_TABLES or column not in columns:
        raise ValueError(f"조회할 수 없는 컬럼: {table}.{column}")
    col = sql.Identifier(column)
    if mode == "exact":
        cond, param = sql.SQL("{} = %s").format(col), text
    elif mode == "partial":
        pattern = text.lower().replace(" ", "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        cond, param = sql.SQL("replace(lower({}), ' ', '') LIKE %s").format(col), f"%{pattern}%"
    else:
        raise ValueError(f"mode는 exact|partial: {mode!r}")
    query = sql.SQL("SELECT {col} FROM {table} WHERE {cond} GROUP BY {col} ORDER BY length({col}), {col} LIMIT %s").format(
        col=col, table=sql.Identifier("public", table), cond=cond)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(query, (param, limit))
        return [r[0] for r in cur.fetchall()]
