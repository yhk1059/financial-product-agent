"""LangGraph SQL 에이전트 그래프.

START → select_tables → generate_query ─┬─(도구 호출 없음)→ END
                              ↑          └─(도구 호출)→ [check_query] → run_query ─┘

check_query는 선택 사항(기본 끔): LLM 호출이 한 번 더 늘어 CLOVA Studio 분당 토큰 한도에 쉽게 걸린다.

LangGraph SQL Agent 튜토리얼 구조를 따르되, HCX-007이 tool_choice를 지원하지 않아
- 테이블 선택은 구조화 출력(json_schema)으로,
- 스키마 조회는 도구 호출 대신 프롬프트에 스키마 카드를 직접 넣는 방식으로,
- 쿼리 검토는 구조화 출력으로 SQL을 다시 받아 도구 호출 인자를 교체하는 방식으로 구현한다.
"""

import logging
from typing import Literal

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from pydantic import BaseModel, Field

from .config import ALLOWED_TABLES, settings
from .llm import get_llm, invoke_with_retry
from .prompts import check_prompt, generate_prompt, select_prompt
from .schema_catalog import schema_card, table_overview
from .tools import run_query

log = logging.getLogger(__name__)

TableName = Literal[ALLOWED_TABLES]  # type: ignore[valid-type]


class AgentState(MessagesState):
    selected_tables: list[str]
    attempts: int


class TableSelection(BaseModel):
    tables: list[TableName] = Field(description="질문에 답하는 데 필요한 테이블 이름 목록")


class CheckedQuery(BaseModel):
    query: str = Field(description="검토 후 실행할 최종 SQL")


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


def generate_query(state: AgentState) -> dict:
    tables = state.get("selected_tables") or []
    schema = schema_card(tables) if tables else "(이 질문에는 테이블 조회가 필요하지 않습니다.)"
    give_up = state.get("attempts", 0) >= settings.max_query_attempts
    messages = [SystemMessage(generate_prompt(tables, schema, give_up)), *state["messages"]]
    # 대화에 tool 메시지가 있으면 CLOVA API는 tools 정의를 요구하므로 한도 도달 시에도 도구는 바인딩해 둔다.
    response = invoke_with_retry(get_llm().bind_tools([run_query]), messages)
    if give_up and response.tool_calls:
        response = AIMessage(
            content=response.text or "쿼리 실행 한도에 도달해 답변을 완성하지 못했습니다. 질문을 더 구체적으로 다시 해 주세요.",
            id=response.id,
        )
    return {"messages": [response]}


def check_query(state: AgentState) -> dict:
    last = state["messages"][-1]
    checker = get_llm().with_structured_output(CheckedQuery, method="json_schema")
    tables = state.get("selected_tables") or list(ALLOWED_TABLES)
    system = check_prompt(_last_question(state["messages"]), tables, schema_card(tables))
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


def build_graph(checkpointer=None, check: bool = False):
    builder = StateGraph(AgentState)
    builder.add_node(select_tables)
    builder.add_node(generate_query)
    builder.add_node("run_query", ToolNode([run_query], name="run_query"))
    builder.add_edge(START, "select_tables")
    builder.add_edge("select_tables", "generate_query")
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


def build_chat_agent(check: bool = False):
    """멀티턴 대화용 (InMemorySaver 체크포인터)."""
    return build_graph(checkpointer=InMemorySaver(), check=check)
