"""에이전트가 호출하는 SQL 실행 도구."""

import decimal
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
        msg = (e.diag.message_primary if e.diag and e.diag.message_primary else str(e)).strip()
        hint = f" (HINT: {e.diag.message_hint})" if e.diag and e.diag.message_hint else ""
        return f"Error: {msg}{hint}"
    return format_rows(columns, rows)
