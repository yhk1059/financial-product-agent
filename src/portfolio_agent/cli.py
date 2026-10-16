"""터미널 대화형 CLI.

사용법:
    uv run portfolio-agent              # 대화 모드
    uv run portfolio-agent -q "질문"     # 한 번만 질문
    uv run portfolio-agent --show-sql   # 실행한 SQL도 출력
"""

import argparse
import uuid

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from .graph import build_chat_agent


def ask(agent, question: str, thread_id: str, show_sql: bool = False) -> str:
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 30}
    answer = ""
    for update in agent.stream(
        {"messages": [HumanMessage(question)]}, config, stream_mode="updates"
    ):
        for node, data in update.items():
            if not data:
                continue
            if node == "select_tables" and show_sql:
                print(f"  [tables] {', '.join(data['selected_tables']) or '-'}")
            for m in data.get("messages", []):
                if show_sql and isinstance(m, AIMessage) and m.tool_calls:
                    tag = "sql(검토후)" if node == "check_query" else "sql"
                    for call in m.tool_calls:
                        print(f"  [{tag}] {call['args'].get('query')}")
                elif show_sql and isinstance(m, ToolMessage):
                    preview = m.text.splitlines()
                    print(f"  [result] {preview[-1] if preview else ''}")
                elif node == "generate_query" and isinstance(m, AIMessage) and not m.tool_calls:
                    answer = m.text
    return answer


def main() -> None:
    parser = argparse.ArgumentParser(description="금융상품 데이터 질의응답 에이전트")
    parser.add_argument("-q", "--question", help="질문 하나를 실행하고 종료")
    parser.add_argument("--show-sql", action="store_true", help="선택된 테이블과 실행 SQL 출력")
    parser.add_argument("--check", action="store_true", help="실행 전 쿼리 검토(check_query) 단계 사용")
    args = parser.parse_args()

    agent = build_chat_agent(check=args.check)
    thread_id = str(uuid.uuid4())

    if args.question:
        print(ask(agent, args.question, thread_id, args.show_sql))
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
        print("\n" + ask(agent, question, thread_id, args.show_sql))


if __name__ == "__main__":
    main()
