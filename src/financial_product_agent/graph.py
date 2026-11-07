"""LangGraph SQL 에이전트 그래프.

START → select_tables → [retrieve_knowledge] → [analyze_question → resolve_slots] → generate_query ─┬─(도구 호출 없음)→ END
                                                                                  ↑          └─(도구 호출)→ run_query → count_attempt ─┘

analyze_question → resolve_slots(--slots, 기본 끔; docs/plan_query_slots_design.md)는
LLM이 질문의 이름·등급 조건·범주 값을 원문 그대로 칸(slots.QuestionSlots)에 넣고, 코드(slots.resolve)가 온톨로지·카탈로그·
읽기 전용 DB 조회로 SQL 조각을 만들어 generate_query 프롬프트 끝의 '## 확인된 값'에 넣는다. 턴마다 한 번(재시도에는
다시 뽑지 않는다), LLM 호출이 하나 늘어난다. 어느 쪽이 실패해도 칸 없는 프롬프트와 같다.
enforce와 함께 쓰면 run_query가 확인된 등급·값 묶음과 다른 SQL을 막고(턴당 slots.MAX_STRIKES번까지),
답변의 [적용 조건]에 이름 해석을 붙인다.

retrieve_knowledge는 온톨로지 모드(config.ONTOLOGY_MODES)가 off가 아닐 때만 넣는다(기본 off).
- ab: 선택된 테이블의 A층(개념·속성·관계, 속성 전용 규칙)과 B층(공통 규칙, 기준일)
- abc: ab + 마지막 사용자 질문으로 검색한 C층 용어·규칙(search.search)
- oracle(평가 전용): ab + 입력 state의 oracle_ids(문항의 정답 항목)를 C층으로. 검색이 완벽할 때를 재는 진단용
조립된 글은 generate_query 프롬프트의 {rules} 자리와 스키마 카드 뒤에 들어간다. LLM 호출은 늘지 않는다.

LangGraph SQL Agent 튜토리얼 구조를 따르되, HCX-007이 tool_choice를 지원하지 않아
- 테이블 선택은 구조화 출력(json_schema)으로,
- 스키마 조회는 도구 호출 대신 프롬프트에 스키마 카드를 직접 넣는 방식으로,
- SQL 생성도 도구 호출 대신 구조화 출력(QueryStep: action/sql/answer)으로 받고,
  action=query이면 코드가 run_query 도구 호출을 만들어 실행을 강제한다
  (모델이 '조회하겠습니다'라고 글만 쓰고 끝나는 실패를 구조적으로 막는다).

enforce(기본 끔, sql_rules)는 기계적으로 판별되는 규칙을 프롬프트 대신 코드가 맡는다. LLM 호출은 늘지 않는다.
- generate_query가 도구 호출 SQL의 ORDER BY … DESC를 DESC NULLS LAST로 바꾼다(기록된 SQL = 실행된 SQL)
- run_query가 등급 컬럼 문자열 대소 비교·현재 시각 함수·0이 미입력인 컬럼(R-zero-missing)을 0 제외 없이
  계산에 쓰기·DB에 정확히 없는 상품명/발행기관명(부분 일치 후보가 있을 때)을 Error:로 돌려주고,
  LIMIT에 닿은 결과와 정확히 없는 이름(구성종목 포함)에 알림을 붙인다
- 최종 답변(answer·give_up) 끝에 이번 턴의 성공한 SQL에서 찾은 [적용 조건](기준일, 결측·0 제외)을 붙인다
"""

import logging
import uuid
from typing import Literal

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from pydantic import BaseModel, Field

from .config import ALLOWED_TABLES, EVAL_ONTOLOGY_MODES, settings
from .context import build_context, get_knowledge
from .llm import get_llm, invoke_with_retry
from .prompts import ANALYZE_QUESTION, generate_prompt, select_prompt
from .schema_catalog import load_catalog, schema_card, table_overview
from .slots import QuestionSlots, name_notes, render_resolved, resolve
from . import db
from .search import search
from .sql_rules import (RULE_MARK, applied_conditions, cash_values, class_id_columns, name_columns, ordered_columns,
                        rewrite, unit_config, zero_missing_columns, zero_value_columns)
from .tools import make_run_query, run_query

log = logging.getLogger(__name__)

TableName = Literal[ALLOWED_TABLES]  # type: ignore[valid-type]


class AgentState(MessagesState):
    selected_tables: list[str]
    attempts: int
    knowledge_rules: str   # 온톨로지 조립 규칙 ({rules} 자리). 비면 domain_rules.md 규칙을 쓴다
    knowledge_tables: str  # 스키마 카드 뒤에 붙이는 A층 블록
    knowledge_layers: dict  # 층별 항목 id (보고용)
    knowledge_oos: str      # C층의 '데이터에 없는 개념' 줄. 비면 프롬프트에 그 절이 없다
    oracle_ids: list[str]   # oracle 모드에서 C층으로 쓸 항목 id (평가 스크립트가 입력으로 넣음)
    slots: dict             # analyze_question 원출력 (QuestionSlots.model_dump()). 실패면 {}
    resolved: list[dict]    # slots.resolve 결과. enforce 칸 검사가 읽는다
    resolved_text: str      # '## 확인된 값' 블록 줄. 비면 generate_query 프롬프트는 칸 없는 것과 같다


class TableSelection(BaseModel):
    tables: list[TableName] = Field(description="질문에 답하는 데 필요한 테이블 이름 목록")


class QueryStep(BaseModel):
    action: Literal["query", "answer"] = Field(
        description="query: sql을 실행해 데이터를 조회한다. answer: 조회 없이 또는 지금까지의 결과로 최종 답변한다.")
    sql: str = Field(description="action이 query일 때 실행할 단일 SELECT 문. answer이면 빈 문자열")
    answer: str = Field(description="action이 answer일 때 사용자에게 보낼 최종 답변. query이면 빈 문자열")


class FinalAnswer(BaseModel):
    """쿼리 실행 한도에 닿은 뒤(give_up)에만 쓴다. sql 필드가 없어 더 실행할 수 없다."""

    answer: str = Field(description="사용자에게 보낼 최종 답변")


GENERATION_FAILED = "SQL을 만들지 못했습니다. 질문을 바꿔 다시 시도해 주세요."
GIVE_UP_FAILED = "쿼리 실행 한도에 도달해 답변을 완성하지 못했습니다. 질문을 더 구체적으로 다시 해 주세요."


def _last_question(messages: list[AnyMessage]) -> str:
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            return m.text
    return ""


def _recent_dialogue(messages: list[AnyMessage], n: int = 6) -> str:
    """테이블 선택용: 최근 사용자 질문과 최종 답변만 텍스트로 요약."""
    turns = [
        f"{'사용자' if isinstance(m, HumanMessage) else '어시스턴트'}: {m.text[:300]}"
        for m in messages
        if isinstance(m, HumanMessage) or (isinstance(m, AIMessage) and not m.tool_calls and m.text)
    ]
    return "\n".join(turns[-n:])


def select_tables(state: AgentState) -> dict:
    llm = get_llm().with_structured_output(TableSelection, method="json_schema")
    try:
        result = invoke_with_retry(
            llm,
            [
                SystemMessage(select_prompt(table_overview())),
                HumanMessage(_recent_dialogue(state["messages"])),
            ],
        )
        tables = list(dict.fromkeys(result.tables))
    except Exception:
        log.exception("table selection failed; using all tables")
        tables = list(ALLOWED_TABLES)
    if "domestic_etf_holdings" in tables and "pref01n001" not in tables:
        tables.append("pref01n001")
    return {"selected_tables": tables, "attempts": 0}


def retrieve_knowledge(state: AgentState, mode: str) -> dict:
    """선택된 테이블(와 abc면 질문 검색 결과, oracle이면 주어진 정답 항목)로 온톨로지 글을 조립한다.

    실패하면 빈 글(off와 같은 프롬프트).
    """
    try:
        if mode == "abc":
            c_ids = search(_last_question(state["messages"]))
        elif mode == "oracle":
            c_ids = list(state.get("oracle_ids") or [])
        else:
            c_ids = []
        ctx = build_context(get_knowledge(), state.get("selected_tables") or [], c_ids)
    except Exception:
        log.exception("knowledge retrieval failed; continuing without ontology")
        return {"knowledge_rules": "", "knowledge_tables": "", "knowledge_layers": {}, "knowledge_oos": ""}
    return {"knowledge_rules": ctx.rules_text, "knowledge_tables": ctx.tables_text, "knowledge_layers": ctx.layers,
            "knowledge_oos": ctx.oos_text}


def _with_knowledge(state: AgentState, card: str) -> str:
    extra = state.get("knowledge_tables")
    return card + "\n\n" + extra if extra else card


def analyze_question(state: AgentState) -> dict:
    """질문에 적힌 이름·등급 조건·범주 값을 원문 그대로 뽑는다(판단하지 않는다). 실패하면 빈 칸."""
    llm = get_llm().with_structured_output(QuestionSlots, method="json_schema")
    try:
        result = invoke_with_retry(llm, [SystemMessage(ANALYZE_QUESTION), HumanMessage(_recent_dialogue(state["messages"]))])
    except Exception:
        log.exception("question analysis failed; continuing without slots")
        return {"slots": {}}
    return {"slots": result.model_dump()}


def resolve_slots(state: AgentState, lookup=None) -> dict:
    """칸을 코드가 확인된 값(SQL 조각)으로 바꾼다. 어떤 예외든 빈 결과(칸 없는 프롬프트와 같다)."""
    slots = state.get("slots") or {}
    if not slots:
        return {"resolved": [], "resolved_text": ""}
    try:
        resolved = resolve(slots, get_knowledge(), load_catalog(), lookup or db.DbLookup(),
                           state.get("selected_tables") or [], _recent_dialogue(state["messages"]),
                           question=_last_question(state["messages"]))
    except Exception:
        log.exception("slot resolution failed; continuing without slots")
        return {"resolved": [], "resolved_text": ""}
    return {"resolved": resolved, "resolved_text": render_resolved(resolved)}


def _llm_history(messages: list[AnyMessage]) -> list[AnyMessage]:
    """LLM에 보낼 대화 사본: 도구 호출과 도구 결과를 글로 바꾼다 (state는 그대로 둔다).

    구조화 출력 호출에는 tools 정의가 없고, CLOVA API는 그런 호출에서 대화에 tool 메시지가 있으면 거부한다.
    """
    history = []
    for m in messages:
        if isinstance(m, AIMessage) and m.tool_calls:
            sql = "\n\n".join(call["args"].get("query", "") for call in m.tool_calls)
            history.append(AIMessage(f"[실행한 SQL]\n{sql}"))
        elif isinstance(m, ToolMessage):
            history.append(HumanMessage(f"[실행 결과]\n{m.text}"))
        else:
            history.append(m)
    return history


def _invalid_step(step) -> str | None:
    """구조화 출력이 쓸 수 없는 모양이면 이유를, 괜찮으면 None을 돌려준다."""
    if isinstance(step, QueryStep):
        if step.action == "query" and not step.sql.strip():
            return "action=query인데 sql이 비어 있음"
        if step.action == "answer" and not step.answer.strip():
            return "action=answer인데 answer가 비어 있음"
        return None
    if isinstance(step, FinalAnswer):
        return None if step.answer.strip() else "answer가 비어 있음"
    return f"예상하지 못한 출력: {type(step).__name__}"


def _structured_step(schema: type[BaseModel], messages: list[AnyMessage]) -> tuple[BaseModel | None, str]:
    """구조화 출력으로 한 단계를 받는다. 출력이 이상하면 한 번 다시 부르고, 그래도 이상하면 (None, 오류 요약)."""
    llm = get_llm().with_structured_output(schema, method="json_schema")
    errors = []
    for _ in range(2):
        try:
            step = invoke_with_retry(llm, messages)
        except Exception as e:  # 파싱·검증 실패
            errors.append(f"{type(e).__name__}: {e}"[:300])
            continue
        problem = _invalid_step(step)
        if problem is None:
            return step, ""
        errors.append(problem)
    return None, " / ".join(errors)


def _turn_sqls(messages: list[AnyMessage]) -> list[str]:
    """이번 턴(마지막 사용자 질문 이후)에 성공한(Error:가 아닌) SQL들."""
    start = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=-1) + 1
    turn = messages[start:]
    ok = {m.tool_call_id for m in turn if isinstance(m, ToolMessage) and not m.text.startswith("Error:")}
    return [call["args"].get("query", "") for m in turn if isinstance(m, AIMessage)
            for call in m.tool_calls if call["id"] in ok]


def _zero_missing() -> frozenset:
    try:
        return zero_missing_columns(get_knowledge())
    except Exception:
        log.exception("knowledge unavailable; rewrite skips zero exclusion")
        return frozenset()


def generate_query(state: AgentState, enforce: bool = False, zero_missing: frozenset = frozenset()) -> dict:
    tables = state.get("selected_tables") or []
    schema = _with_knowledge(state, schema_card(tables)) if tables else "(이 질문에는 테이블 조회가 필요하지 않습니다.)"
    give_up = state.get("attempts", 0) >= settings.max_query_attempts
    rules = state.get("knowledge_rules") or None
    prompt = generate_prompt(tables, schema, give_up, rules=rules, resolved=state.get("resolved_text") or "",
                             oos=state.get("knowledge_oos") or "")
    messages = [SystemMessage(prompt), *_llm_history(state["messages"])]
    step, error = _structured_step(FinalAnswer if give_up else QueryStep, messages)
    if step is None:
        log.warning("generate_query output invalid twice: %s", error)
        text = GIVE_UP_FAILED if give_up else GENERATION_FAILED
        return {"messages": [AIMessage(text, response_metadata={"generation_error": error})]}
    if isinstance(step, FinalAnswer) or step.action == "answer":
        answer = step.answer.strip()
        if enforce:
            sqls = _turn_sqls(state["messages"])
            answer += applied_conditions(sqls, name_notes(state.get("resolved") or [], sqls))
        return {"messages": [AIMessage(answer)]}
    # 실행은 LLM이 아니라 코드가 정한다: run_query 도구 호출을 직접 만들어 이후 노드(실행)로 넘긴다.
    msg_id = str(uuid.uuid4())
    sql = rewrite(step.sql.strip(), zero_missing) if enforce else step.sql.strip()
    call = {"name": "run_query", "args": {"query": sql}, "id": f"call_{msg_id}"}
    return {"messages": [AIMessage(content="", tool_calls=[call], id=msg_id)]}


def route_after_generate(state: AgentState) -> Literal["run_query", "__end__"]:
    last = state["messages"][-1]
    return "run_query" if isinstance(last, AIMessage) and last.tool_calls else END


FREE_RULE_BLOCKS = 3  # 실행 전 규칙 차단은 턴마다 이만큼까지 실행 횟수에 넣지 않는다(그 뒤로는 센다: 무한 반복 방지)


def count_attempts(messages: list[AnyMessage]) -> int:
    """이번 턴의 실행 횟수: 실행한(또는 DB 오류가 난) 쿼리 수 + FREE_RULE_BLOCKS를 넘긴 규칙 차단 수."""
    start = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=-1) + 1
    results = [m.text for m in messages[start:] if isinstance(m, ToolMessage)]
    blocked = sum(t.startswith(f"Error: {RULE_MARK}") for t in results)
    return len(results) - blocked + max(0, blocked - FREE_RULE_BLOCKS)


def _enforce_tool():
    """enforce용 run_query. knowledge를 못 읽으면 knowledge가 필요한 검사(등급·0=미입력·이름·단위·현금성) 없이 계속한다."""
    try:
        kn = get_knowledge()
        return make_run_query(ordered_columns(kn), zero_missing_columns(kn), name_columns(kn), db.lookup_values,
                              slot_checks=True, units=unit_config(kn), class_ids=class_id_columns(kn), cash=cash_values(kn),
                              zero_value=zero_value_columns(kn))
    except Exception:
        log.exception("knowledge unavailable; skipping grade, zero, name, unit and cash checks")
        return make_run_query({}, slot_checks=True)


def build_graph(checkpointer=None, ontology: str = "off", enforce: bool = False, slots: bool = False):
    if ontology not in EVAL_ONTOLOGY_MODES:
        raise ValueError(f"ontology는 {', '.join(EVAL_ONTOLOGY_MODES)} 중 하나여야 합니다: {ontology!r}")
    builder = StateGraph(AgentState)
    builder.add_node(select_tables)
    if enforce:
        # 노드 이름은 generate_query로 둔다: cli.ask가 이 이름에서 답변을 모은다.
        zero_missing = _zero_missing()
        builder.add_node("generate_query", lambda s: generate_query(s, enforce=True, zero_missing=zero_missing))
        builder.add_node("run_query", ToolNode([_enforce_tool()], name="run_query"))
    else:
        builder.add_node(generate_query)
        builder.add_node("run_query", ToolNode([run_query], name="run_query"))
    builder.add_edge(START, "select_tables")
    prev = "select_tables"
    if ontology != "off":
        builder.add_node("retrieve_knowledge", lambda s: retrieve_knowledge(s, ontology))
        builder.add_edge(prev, "retrieve_knowledge")
        prev = "retrieve_knowledge"
    if slots:
        builder.add_node(analyze_question)
        builder.add_node(resolve_slots)
        builder.add_edge(prev, "analyze_question")
        builder.add_edge("analyze_question", "resolve_slots")
        prev = "resolve_slots"
    builder.add_edge(prev, "generate_query")
    # 실행 횟수는 실행 뒤에 센다: 코드가 실행 전에 막은 규칙 오류는 FREE_RULE_BLOCKS까지 한도에서 뺀다.
    builder.add_node("count_attempt", lambda s: {"attempts": count_attempts(s["messages"])})
    builder.add_conditional_edges("generate_query", route_after_generate)
    builder.add_edge("run_query", "count_attempt")
    builder.add_edge("count_attempt", "generate_query")
    return builder.compile(checkpointer=checkpointer)


def build_chat_agent(ontology: str = "off", enforce: bool = False, slots: bool = False):
    """멀티턴 대화용 (InMemorySaver 체크포인터)."""
    return build_graph(checkpointer=InMemorySaver(), ontology=ontology, enforce=enforce, slots=slots)
