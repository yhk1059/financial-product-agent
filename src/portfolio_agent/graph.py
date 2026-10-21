"""LangGraph SQL 에이전트 그래프.

START → select_tables → [retrieve_knowledge] → generate_query ─┬─(도구 호출 없음)→ END
                                                    ↑          └─(도구 호출)→ [check_query | count_attempt] → run_query ─┘

check_query는 선택 사항(기본 끔): LLM 호출이 한 번 더 늘어 CLOVA Studio 분당 토큰 한도에 쉽게 걸린다.
retrieve_knowledge는 온톨로지 모드(config.ONTOLOGY_MODES)가 off가 아닐 때만 넣는다(기본 off).
- ab: 선택된 테이블의 A층(개념·속성·관계, 속성 전용 규칙)과 B층(공통 규칙, 기준일)
- abc: ab + 마지막 사용자 질문으로 검색한 C층 용어·규칙(search.search)
조립된 글은 generate_query·check_query 프롬프트의 {rules} 자리와 스키마 카드 뒤에 들어간다. LLM 호출은 늘지 않는다.

LangGraph SQL Agent 튜토리얼 구조를 따르되, HCX-007이 tool_choice를 지원하지 않아
- 테이블 선택은 구조화 출력(json_schema)으로,
- 스키마 조회는 도구 호출 대신 프롬프트에 스키마 카드를 직접 넣는 방식으로,
- SQL 생성도 도구 호출 대신 구조화 출력(QueryStep: action/sql/answer)으로 받고,
  action=query이면 코드가 run_query 도구 호출을 만들어 실행을 강제한다
  (모델이 '조회하겠습니다'라고 글만 쓰고 끝나는 실패를 구조적으로 막는다),
- 쿼리 검토는 구조화 출력으로 SQL을 다시 받아 도구 호출 인자를 교체하는 방식으로 구현한다.
"""

import logging
import uuid
from typing import Literal

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from pydantic import BaseModel, Field

from .config import ALLOWED_TABLES, ONTOLOGY_MODES, settings
from .context import build_context, get_knowledge
from .llm import get_llm, invoke_with_retry
from .prompts import check_prompt, generate_prompt, select_prompt
from .schema_catalog import schema_card, table_overview
from .search import search
from .tools import run_query

log = logging.getLogger(__name__)

TableName = Literal[ALLOWED_TABLES]  # type: ignore[valid-type]


class AgentState(MessagesState):
    selected_tables: list[str]
    attempts: int
    knowledge_rules: str   # 온톨로지 조립 규칙 ({rules} 자리). 비면 domain_rules.md 규칙을 쓴다
    knowledge_tables: str  # 스키마 카드 뒤에 붙이는 A층 블록
    knowledge_layers: dict  # 층별 항목 id (보고용)


class TableSelection(BaseModel):
    tables: list[TableName] = Field(description="질문에 답하는 데 필요한 테이블 이름 목록")


class CheckedQuery(BaseModel):
    query: str = Field(description="검토 후 실행할 최종 SQL")


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
    """선택된 테이블(와 abc면 질문 검색 결과)로 온톨로지 글을 조립한다. 실패하면 빈 글(off와 같은 프롬프트)."""
    try:
        c_ids = search(_last_question(state["messages"])) if mode == "abc" else []
        ctx = build_context(get_knowledge(), state.get("selected_tables") or [], c_ids)
    except Exception:
        log.exception("knowledge retrieval failed; continuing without ontology")
        return {"knowledge_rules": "", "knowledge_tables": "", "knowledge_layers": {}}
    return {"knowledge_rules": ctx.rules_text, "knowledge_tables": ctx.tables_text, "knowledge_layers": ctx.layers}


def _with_knowledge(state: AgentState, card: str) -> str:
    extra = state.get("knowledge_tables")
    return card + "\n\n" + extra if extra else card


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


def generate_query(state: AgentState) -> dict:
    tables = state.get("selected_tables") or []
    schema = _with_knowledge(state, schema_card(tables)) if tables else "(이 질문에는 테이블 조회가 필요하지 않습니다.)"
    give_up = state.get("attempts", 0) >= settings.max_query_attempts
    rules = state.get("knowledge_rules") or None
    messages = [SystemMessage(generate_prompt(tables, schema, give_up, rules=rules)), *_llm_history(state["messages"])]
    step, error = _structured_step(FinalAnswer if give_up else QueryStep, messages)
    if step is None:
        log.warning("generate_query output invalid twice: %s", error)
        text = GIVE_UP_FAILED if give_up else GENERATION_FAILED
        return {"messages": [AIMessage(text, response_metadata={"generation_error": error})]}
    if isinstance(step, FinalAnswer) or step.action == "answer":
        return {"messages": [AIMessage(step.answer.strip())]}
    # 실행은 LLM이 아니라 코드가 정한다: run_query 도구 호출을 직접 만들어 이후 노드(검토·실행)로 넘긴다.
    msg_id = str(uuid.uuid4())
    call = {"name": "run_query", "args": {"query": step.sql.strip()}, "id": f"call_{msg_id}"}
    return {"messages": [AIMessage(content="", tool_calls=[call], id=msg_id)]}


def check_query(state: AgentState) -> dict:
    last = state["messages"][-1]
    checker = get_llm().with_structured_output(CheckedQuery, method="json_schema")
    tables = state.get("selected_tables") or list(ALLOWED_TABLES)
    system = check_prompt(_last_question(state["messages"]), tables, _with_knowledge(state, schema_card(tables)),
                          rules=state.get("knowledge_rules") or None)
    tool_calls = []
    for call in last.tool_calls:
        original = call["args"].get("query", "")
        try:
            checked = invoke_with_retry(checker, [SystemMessage(system), HumanMessage(original)]).query.strip()
        except Exception:
            log.exception("query check failed; keeping original")
            checked = original
        tool_calls.append({**call, "args": {"query": checked or original}})
    # 같은 id로 반환하면 MessagesState가 원래 메시지를 교체한다.
    revised = AIMessage(content=last.content, tool_calls=tool_calls, id=last.id)
    return {"messages": [revised], "attempts": state.get("attempts", 0) + 1}


def route_after_generate(state: AgentState) -> Literal["check_query", "__end__"]:
    last = state["messages"][-1]
    return "check_query" if isinstance(last, AIMessage) and last.tool_calls else END


def build_graph(checkpointer=None, check: bool = False, ontology: str = "off"):
    if ontology not in ONTOLOGY_MODES:
        raise ValueError(f"ontology는 {', '.join(ONTOLOGY_MODES)} 중 하나여야 합니다: {ontology!r}")
    builder = StateGraph(AgentState)
    builder.add_node(select_tables)
    builder.add_node(generate_query)
    builder.add_node("run_query", ToolNode([run_query], name="run_query"))
    builder.add_edge(START, "select_tables")
    if ontology == "off":
        builder.add_edge("select_tables", "generate_query")
    else:
        builder.add_node("retrieve_knowledge", lambda s: retrieve_knowledge(s, ontology))
        builder.add_edge("select_tables", "retrieve_knowledge")
        builder.add_edge("retrieve_knowledge", "generate_query")
    if check:
        builder.add_node(check_query)
        builder.add_conditional_edges("generate_query", route_after_generate)
        builder.add_edge("check_query", "run_query")
    else:
        builder.add_node("count_attempt", lambda s: {"attempts": s.get("attempts", 0) + 1})
        builder.add_conditional_edges(
            "generate_query",
            lambda s: "count_attempt" if route_after_generate(s) == "check_query" else END,
        )
        builder.add_edge("count_attempt", "run_query")
    builder.add_edge("run_query", "generate_query")
    return builder.compile(checkpointer=checkpointer)


def build_chat_agent(check: bool = False, ontology: str = "off"):
    """멀티턴 대화용 (InMemorySaver 체크포인터)."""
    return build_graph(checkpointer=InMemorySaver(), check=check, ontology=ontology)
