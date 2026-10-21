"""에이전트가 호출하는 SQL 실행 도구."""

import decimal
import re
from datetime import date, datetime

import psycopg
from langchain_core.tools import tool

from . import db
from .config import settings
from .sql_guard import SQLGuardError, validate


def _fmt(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, decimal.Decimal | float):
        return f"{v:.4f}".rstrip("0").rstrip(".") if v == v else "NaN"
    if isinstance(v, date | datetime):
        return v.isoformat()
    return str(v)


def format_rows(columns: list[str], rows: list[tuple]) -> str:
    shown = rows[: settings.max_result_rows]
    lines = [" | ".join(columns)] + [" | ".join(_fmt(v) for v in r) for r in shown]
    text = "\n".join(lines)
    if len(text) > settings.max_result_chars:
        text = text[: settings.max_result_chars] + "\n…(잘림)"
    note = f"\n(총 {len(rows)}행 중 {len(shown)}행 표시)" if len(rows) > len(shown) else f"\n({len(rows)}행)"
    return text + note


def _position_context(sql: str, position: str | None, width: int = 30) -> str:
    """PostgreSQL 오류 위치(1부터 센 글자 위치)의 토큰을 >>토큰<<으로 표시한 앞뒤 문맥. 위치가 없으면 빈 문자열."""
    if not position or not position.isdigit() or not 0 < int(position) <= len(sql):
        return ""
    i = int(position) - 1
    token = re.match(r"\S{1,30}", sql[i:])
    end = i + (len(token.group()) if token else 1)
    before, after = sql[max(0, i - width) : i], sql[end : end + width]
    return f"\n  {'…' if i > width else ''}{before}>>{sql[i:end]}<<{after}{'…' if end + width < len(sql) else ''}"


def db_error_message(e: psycopg.Error, sql: str) -> str:
    """DB 오류를 LLM에 돌려줄 글로 만든다: 기본 메시지, HINT, 오류 위치 문맥."""
    diag = e.diag
    msg = (diag.message_primary if diag and diag.message_primary else str(e)).strip()
    hint = f" (HINT: {diag.message_hint})" if diag and diag.message_hint else ""
    where = _position_context(sql, diag.statement_position if diag else None)
    return f"Error: {msg}{hint}{where}"


@tool
def run_query(query: str) -> str:
    """PostgreSQL SELECT 쿼리를 실행하고 결과 행을 반환한다.

    Args:
        query: 실행할 단일 SELECT 문 (public 스키마의 허용 테이블만 사용).
    """
    try:
        safe_sql = validate(query)
    except SQLGuardError as e:
        return f"Error: {e}"
    try:
        columns, rows = db.fetch(safe_sql)
    except psycopg.Error as e:
        return db_error_message(e, safe_sql)
    return format_rows(columns, rows)
