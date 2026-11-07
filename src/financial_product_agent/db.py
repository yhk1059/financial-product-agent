"""PostgreSQL 읽기 전용 접속과 쿼리 실행."""

import os
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


def connect_knowledge() -> psycopg.Connection:
    """KNOWLEDGE_PGUSER/KNOWLEDGE_PGPASSWORD(knowledge 스키마 전용 쓰기 계정)로 접속한다. 적재·임베딩·채점 스크립트용."""
    user, password = os.getenv("KNOWLEDGE_PGUSER"), os.getenv("KNOWLEDGE_PGPASSWORD")
    if not user or not password:
        raise SystemExit(".env에 KNOWLEDGE_PGUSER와 KNOWLEDGE_PGPASSWORD가 필요합니다.")
    return psycopg.connect(user=user, password=password, connect_timeout=10)


def fetch(sql: str, params: Any = None) -> tuple[list[str], list[tuple]]:
    """쿼리를 실행하고 (컬럼명, 행 목록)을 반환한다."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        columns = [d.name for d in cur.description] if cur.description else []
        return columns, cur.fetchall() if cur.description else []


def _checked(table: str, column: str) -> str:
    from .schema_catalog import load_catalog

    columns = {c["name"] for c in load_catalog()["tables"].get(table, {}).get("columns", [])}
    if table not in ALLOWED_TABLES or column not in columns:
        raise ValueError(f"조회할 수 없는 컬럼: {table}.{column}")
    return column


def lookup_values(table: str, column: str, text: str, mode: str, limit: int = 10) -> list[str]:
    """컬럼에 저장된 값 중 text와 맞는 것을 짧은 순으로 돌려준다 (파라미터 바인딩, 읽기 전용).

    mode: 'exact' = 정확히 같음, 'normalized' = 공백·대소문자·법인 표기((주), (유), ㈜, 주식회사)를 무시하고 같음
    (slots.normalize_name과 같은 규칙), 'partial' = 공백·대소문자를 무시하고 text를 포함함.
    테이블·컬럼 이름은 허용 테이블과 스키마 카탈로그에 있을 때만 쓴다(문자열 조립 방지).
    """
    col = sql.Identifier(_checked(table, column))
    if mode == "exact":
        cond, param = sql.SQL("{} = %s").format(col), text
    elif mode == "normalized":
        from .slots import normalize_name

        cond = sql.SQL(r"regexp_replace(lower({}), '\s+|\(주\)|\(유\)|㈜|주식회사', '', 'g') = %s").format(col)
        param = normalize_name(text)
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


def lookup_ids(table: str, column: str, value: str, id_column: str, limit: int = 11) -> list[str]:
    """column = value인 행의 id_column 값들 (정렬, 중복 제거)."""
    query = sql.SQL("SELECT DISTINCT {id} FROM {table} WHERE {col} = %s ORDER BY {id} LIMIT %s").format(
        id=sql.Identifier(_checked(table, id_column)), col=sql.Identifier(_checked(table, column)),
        table=sql.Identifier("public", table))
    with connect() as conn, conn.cursor() as cur:
        cur.execute(query, (value, limit))
        return [str(r[0]) for r in cur.fetchall()]


class DbLookup:
    """slots.Lookup의 DB 구현."""

    values = staticmethod(lookup_values)
    ids = staticmethod(lookup_ids)
