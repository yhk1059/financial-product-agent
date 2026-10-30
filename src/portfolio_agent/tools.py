"""에이전트가 호출하는 SQL 실행 도구."""

import decimal
import re
from datetime import date, datetime

from typing import Annotated

import psycopg
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from . import db
from .config import settings
from .sql_guard import SQLGuardError, validate
from .slots import MAX_STRIKES, SLOT_ERROR_MARK, slot_violations
from .sql_rules import name_literals, result_note, violations

NAME_CANDIDATES = 5  # 없는 이름에 붙이는 부분 일치 후보 수


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


def _name_checks(sql: str, names: dict, lookup) -> tuple[list[str], list[str]]:
    """이름 컬럼에 =/IN으로 쓴 값이 DB에 정확히 있는지 본다 → (막을 오류, 결과에 붙일 알림).

    정확히 없고 부분 일치 후보가 있으면 오류(후보를 보여 주고 정확히 일치시켜 다시 조회하게 한다).
    후보도 없으면 막지 않고 알림만 붙인다(데이터에 없는 이름일 수 있다).
    구성종목은 막지 않는다: '편입 없음'이 정답일 수 있다.
    """
    errors, notes = [], []
    for kind, table, column, value in name_literals(sql, names):
        if lookup(table, column, value, "exact"):
            continue
        cands = lookup(table, column, value, "partial")[:NAME_CANDIDATES]
        shown = ", ".join(f"'{c}'" for c in cands)
        if cands and kind != "구성종목":
            errors.append(f"{kind} 이름 '{value}'는 {table}.{column}에 정확히 일치하는 값이 없습니다. 저장된 값 후보: {shown}. "
                          "질문이 가리키는 것이 후보 중 하나면 그 값으로 정확히 일치시켜(=) 다시 조회하고, "
                          "어떻게 해석했는지 답변에 밝히세요. 부분 일치(LIKE) 결과를 그대로 집계하지 않습니다.")
        else:
            notes.append(f"\n(알림: {kind} 이름 '{value}'는 {table}.{column}에 정확히 일치하는 값이 없음"
                         + (f". 비슷한 표기: {shown})" if cands else ")"))
    return errors, notes


def make_run_query(ordered: dict | None, zero_missing: frozenset = frozenset(), names: dict | None = None,
                   name_lookup=None, slot_checks: bool = False):
    """run_query 도구를 만든다. ordered가 None이면 규칙 강제 없이(기본), 있으면 --enforce:
    검증 뒤 sql_rules.violations(zero_missing: 0이 미입력인 컬럼)와 이름 확인(names·name_lookup이 있을 때)으로
    막고(위반이면 실행하지 않음), 결과가 LIMIT에 닿거나 이름 알림이 있으면 결과 끝에 붙인다.
    name_lookup(table, column, text, mode)는 mode 'exact'|'partial'로 저장된 값 목록을 돌려준다(db.lookup_values).

    slot_checks(--enforce --slots): 그래프 상태(InjectedState)의 resolved로 확인된 등급·값 묶음과 다른 SQL도 막는다.
    이번 턴에 칸 검사로 slots.MAX_STRIKES번 막았으면 그 뒤로는 막지 않고 알림만 붙인다(칸이 틀렸을 때 무한 반복 방지).
    """

    def execute(query: str, state: dict | None = None) -> str:
        try:
            safe_sql = validate(query)
        except SQLGuardError as e:
            return f"Error: {e}"
        notes = []
        if ordered is not None:
            problems = violations(safe_sql, ordered, zero_missing)
            if names and name_lookup is not None:
                try:
                    errors, notes = _name_checks(safe_sql, names, name_lookup)
                except psycopg.Error:  # 이름 확인 조회가 실패해도 본 쿼리는 실행한다
                    errors, notes = [], []
                problems += errors
            if state is not None and (slot_errors := slot_violations(safe_sql, state.get("resolved") or [])):
                if _slot_strikes(state.get("messages") or []) < MAX_STRIKES:
                    problems += slot_errors
                else:
                    notes += [f"\n(알림: {e})" for e in slot_errors]
            if problems:
                return "Error: " + " ".join(problems)
        try:
            columns, rows = db.fetch(safe_sql)
        except psycopg.Error as e:
            return db_error_message(e, safe_sql)
        text = format_rows(columns, rows)
        return text + result_note(len(rows)) + "".join(notes) if ordered is not None else text

    if slot_checks:
        @tool
        def run_query(query: str, state: Annotated[dict, InjectedState]) -> str:
            """PostgreSQL SELECT 쿼리를 실행하고 결과 행을 반환한다.

            Args:
                query: 실행할 단일 SELECT 문 (public 스키마의 허용 테이블만 사용).
            """
            return execute(query, state)

        return run_query

    @tool
    def run_query(query: str) -> str:
        """PostgreSQL SELECT 쿼리를 실행하고 결과 행을 반환한다.

        Args:
            query: 실행할 단일 SELECT 문 (public 스키마의 허용 테이블만 사용).
        """
        return execute(query)

    return run_query


def _slot_strikes(messages: list) -> int:
    """이번 턴(마지막 사용자 질문 이후)에 칸 검사로 막은 횟수."""
    start = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=-1) + 1
    return sum(isinstance(m, ToolMessage) and m.text.startswith("Error:") and SLOT_ERROR_MARK in m.text
               for m in messages[start:])


run_query = make_run_query(None)
