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
from .sql_rules import RULE_MARK, name_literals, result_note, rule_violations, violations

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


def sqlstate_hint(sqlstate: str | None, msg: str) -> str:
    """자주 반복되는 PostgreSQL 오류(SQLSTATE)마다 고치는 법 한 줄. 모르는 오류면 빈 문자열."""
    low = msg.lower()
    if "cannot be matched" in low and any(op in low for op in ("union", "intersect", "except")):
        return ("UNION/INTERSECT/EXCEPT 양쪽 SELECT는 컬럼 수와 타입이 같아야 합니다. 집합 결과의 개수는 "
                "SELECT COUNT(*) FROM (SELECT … INTERSECT SELECT …) t 처럼 바깥에서 세세요.")
    if sqlstate == "42803":  # grouping_error
        return ("집계 함수와 함께 SELECT한 일반 컬럼은 그 SELECT의 GROUP BY에 있어야 합니다. UNION의 각 SELECT에 "
                "GROUP BY를 따로 쓰거나, 전체 한 값만 필요하면 그 컬럼을 SELECT에서 빼세요.")
    if sqlstate in ("42883", "42804", "42846") and "text" in low:  # text 컬럼을 숫자·날짜처럼 씀
        return ("문자열(text) 컬럼은 ::numeric 또는 ::date로 바꾼 뒤 비교·계산하세요. "
                f"날짜 상수는 DATE '{settings.as_of_date}', 기간은 INTERVAL '3 years' 꼴로 씁니다.")
    if sqlstate in ("22007", "22P02") and "interval" in low or "date_add" in low:
        return (f"PostgreSQL에서 날짜 계산은 DATE '{settings.as_of_date}' + INTERVAL '3 years' 꼴로 씁니다. "
                "DATE_ADD, DATE_SUB, INTERVAL 3 YEAR(따옴표 없음)는 쓸 수 없습니다.")
    if sqlstate == "22P02":  # invalid_text_representation
        return "숫자로 바꿀 수 없는 값(빈 문자열 등)이 있습니다. NULLIF(컬럼, '')::numeric처럼 바꾸거나 그런 행을 거르세요."
    if sqlstate == "42601":  # syntax_error
        return "PostgreSQL 문법만 씁니다(DATE_ADD·IFNULL·LIMIT a,b 같은 MySQL 문법 불가)."
    if sqlstate == "42703":  # undefined_column
        return "스키마에 적힌 컬럼명만 그대로 쓰세요."
    return ""


def db_error_message(e: psycopg.Error, sql: str) -> str:
    """DB 오류를 LLM에 돌려줄 글로 만든다: 기본 메시지, HINT, SQLSTATE별 고치는 법, 오류 위치 문맥."""
    diag = e.diag
    msg = (diag.message_primary if diag and diag.message_primary else str(e)).strip()
    hint = f" (HINT: {diag.message_hint})" if diag and diag.message_hint else ""
    fix = sqlstate_hint(diag.sqlstate if diag else None, msg)
    where = _position_context(sql, diag.statement_position if diag else None)
    return f"Error: {msg}{hint}" + (f" 고치는 법: {fix}" if fix else "") + where


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


REPEAT_NOTE = "\n(같은 SQL을 오류 뒤에 그대로 다시 보냈습니다. 같은 방법을 되풀이하지 말고 다른 꼴로 고치세요.)"


def _repeats_failed_sql(query: str, messages: list | None) -> bool:
    """이번 턴에 오류가 났던 SQL과 같은 SQL인가(공백 차이만 무시)."""
    if not messages:
        return False
    start = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=-1) + 1
    failed = {m.tool_call_id for m in messages[start:] if isinstance(m, ToolMessage) and m.text.startswith("Error:")}
    norm = " ".join(query.split())
    for m in messages[start:]:
        if getattr(m, "tool_calls", None):
            for call in m.tool_calls:
                if call["id"] in failed and " ".join(call["args"].get("query", "").split()) == norm:
                    return True
    return False


def _last_question(messages: list) -> str:
    for m in reversed(messages or []):
        if isinstance(m, HumanMessage):
            return m.text
    return ""


def make_run_query(ordered: dict | None, zero_missing: frozenset = frozenset(), names: dict | None = None,
                   name_lookup=None, slot_checks: bool = False, units: dict | None = None,
                   class_ids: dict | None = None, cash: list[str] = (), zero_value: frozenset = frozenset()):
    """run_query 도구를 만든다. ordered가 None이면 규칙 강제 없이(기본), 있으면 --enforce:
    검증 뒤 sql_rules.violations(zero_missing: 0이 미입력인 컬럼)와 이름 확인(names·name_lookup이 있을 때)으로
    막고(위반이면 실행하지 않음), 결과가 LIMIT에 닿거나 이름 알림이 있으면 결과 끝에 붙인다.
    name_lookup(table, column, text, mode)는 mode 'exact'|'partial'로 저장된 값 목록을 돌려준다(db.lookup_values).

    slot_checks(--enforce): 그래프 상태(InjectedState)를 받는다. resolved(--slots)로 확인된 등급·값 묶음과 다른 SQL을
    막고(이번 턴에 slots.MAX_STRIKES번 막았으면 그 뒤로는 알림만), 마지막 질문의 말로 집합 논리 검사를 한다.
    units·class_ids·cash(sql_rules.unit_config·class_id_columns·cash_values): 종목 단위·클래스 단위·현금성 제외 검사.
    실행 전에 막은 오류는 'Error: [규칙] …'로 시작한다(graph.count_attempt가 실행 횟수에서 뺀다).
    """

    def execute(query: str, state: dict | None = None) -> str:
        repeated = _repeats_failed_sql(query, state.get("messages") if state is not None else None)
        try:
            safe_sql = validate(query)
        except SQLGuardError as e:
            return f"Error: {e}" + (REPEAT_NOTE if repeated else "")
        notes = []
        if ordered is not None:
            problems = violations(safe_sql, ordered, zero_missing, zero_value)
            problems += rule_violations(safe_sql, units, class_ids, cash,
                                        _last_question(state.get("messages")) if state is not None else "")
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
                return f"Error: {RULE_MARK} " + " ".join(problems)
        try:
            columns, rows = db.fetch(safe_sql)
        except psycopg.Error as e:
            return db_error_message(e, safe_sql) + (REPEAT_NOTE if repeated else "")
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
