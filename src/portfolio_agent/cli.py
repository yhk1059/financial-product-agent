"""터미널 대화형 CLI.

사용법:
    uv run portfolio-agent              # 대화 모드
    uv run portfolio-agent -q "질문"     # 한 번만 질문
    uv run portfolio-agent --show-sql   # 실행한 SQL도 출력
    uv run portfolio-agent --ontology abc   # 온톨로지 지식을 프롬프트에 넣음 (off|ab|abc, 기본 off)
"""

import argparse
import time
import uuid
from dataclasses import dataclass, field

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from . import llm
from .config import ONTOLOGY_MODES, settings
from .graph import build_chat_agent


class _UsageCounter(BaseCallbackHandler):
    """그래프 안의 모든 LLM 호출(구조화 출력 포함)의 호출 수와 토큰을 센다.

    input 토큰은 합계와 호출당 최대값을 따로 센다 (최대값이 분당 토큰 한도에 걸리는지 보는 기준).
    """

    def __init__(self) -> None:
        self.calls = 0
        self.tokens: int | None = None
        self.input_tokens: int | None = None
        self.max_input_tokens: int | None = None

    def on_llm_end(self, response, **kwargs) -> None:
        self.calls += 1
        for gens in response.generations:
            for g in gens:
                usage = getattr(getattr(g, "message", None), "usage_metadata", None)
                if usage:
                    self.tokens = (self.tokens or 0) + usage.get("total_tokens", 0)
                    n = usage.get("input_tokens", 0)
                    self.input_tokens = (self.input_tokens or 0) + n
                    self.max_input_tokens = max(self.max_input_tokens or 0, n)


@dataclass
class AskTrace:
    """질문 하나의 실행 기록 (golden/design.md 5.3)."""

    answer: str = ""
    selected_tables: list[str] = field(default_factory=list)
    queries: list[dict] = field(default_factory=list)  # {"sql", "ok", "output"}
    gave_up: bool = False
    llm_calls: int = 0
    tokens: int | None = None
    input_tokens: int | None = None
    max_input_tokens: int | None = None  # LLM 호출 하나의 input 토큰 최대값
    knowledge_layers: dict = field(default_factory=dict)  # 온톨로지 층별 항목 id (off면 빈 dict)
    seconds: float = 0.0
    retry_wait_seconds: float = 0.0


def ask(agent, question: str, thread_id: str, show_sql: bool = False) -> AskTrace:
    usage = _UsageCounter()
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 30, "callbacks": [usage]}
    trace = AskTrace()
    pending_sql: dict[str, str] = {}  # tool_call_id → 실행될 SQL (check_query가 같은 id로 교체)
    attempts = 0
    start, wait_before = time.time(), llm.retry_wait_seconds()
    for update in agent.stream(
        {"messages": [HumanMessage(question)]}, config, stream_mode="updates"
    ):
        for node, data in update.items():
            if not data:
                continue
            attempts = data.get("attempts", attempts)  # count_attempt·check_query가 갱신
            if node == "select_tables":
                trace.selected_tables = data["selected_tables"]
                if show_sql:
                    print(f"  [tables] {', '.join(data['selected_tables']) or '-'}")
            elif node == "retrieve_knowledge":
                trace.knowledge_layers = data.get("knowledge_layers") or {}
                if show_sql:
                    layers = trace.knowledge_layers
                    print(f"  [knowledge] C층 {len(layers.get('C', []))}개, 확장 {len(layers.get('expanded', []))}개"
                          + ("" if layers else " (조립 실패, 온톨로지 없이 진행)"))
            for m in data.get("messages", []):
                if isinstance(m, AIMessage) and m.tool_calls:
                    for call in m.tool_calls:
                        pending_sql[call["id"]] = call["args"].get("query", "")
                        if show_sql:
                            tag = "sql(검토후)" if node == "check_query" else "sql"
                            print(f"  [{tag}] {call['args'].get('query')}")
                elif isinstance(m, ToolMessage):
                    trace.queries.append(
                        {
                            "sql": pending_sql.get(m.tool_call_id, ""),
                            "ok": not m.text.startswith("Error:"),
                            "output": m.text,
                        }
                    )
                    if show_sql:
                        preview = m.text.splitlines()
                        print(f"  [result] {preview[-1] if preview else ''}")
                elif node == "generate_query" and isinstance(m, AIMessage):
                    trace.answer = m.text
    # 실행 횟수가 한도에 닿은 뒤의 답변은 give_up 모드에서 나온다(graph.generate_query).
    trace.gave_up = attempts >= settings.max_query_attempts
    trace.llm_calls, trace.tokens = usage.calls, usage.tokens
    trace.input_tokens, trace.max_input_tokens = usage.input_tokens, usage.max_input_tokens
    trace.seconds = round(time.time() - start, 2)
    trace.retry_wait_seconds = llm.retry_wait_seconds() - wait_before
    return trace


def main() -> None:
    parser = argparse.ArgumentParser(description="금융상품 데이터 질의응답 에이전트")
    parser.add_argument("-q", "--question", help="질문 하나를 실행하고 종료")
    parser.add_argument("--show-sql", action="store_true", help="선택된 테이블과 실행 SQL 출력")
    parser.add_argument("--check", action="store_true", help="실행 전 쿼리 검토(check_query) 단계 사용")
    parser.add_argument("--ontology", choices=ONTOLOGY_MODES, default="off",
                        help="온톨로지 지식: off(넣지 않음), ab(테이블·공통), abc(+질문 검색)")
    args = parser.parse_args()

    agent = build_chat_agent(check=args.check, ontology=args.ontology)
    thread_id = str(uuid.uuid4())

    if args.question:
        print(ask(agent, args.question, thread_id, args.show_sql).answer)
        return

    print("금융상품 데이터 에이전트입니다. 질문을 입력하세요. (종료: exit, 새 대화: /new)")
    while True:
        try:
            question = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not question:
            continue
        if question in {"exit", "quit", "종료"}:
            break
        if question == "/new":
            thread_id = str(uuid.uuid4())
            print("새 대화를 시작합니다.")
            continue
        print("\n" + ask(agent, question, thread_id, args.show_sql).answer)


if __name__ == "__main__":
    main()
