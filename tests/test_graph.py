"""그래프 구성과 온톨로지 연결 테스트 (DB·LLM 불필요)."""

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from portfolio_agent import graph
from portfolio_agent.context import Context


def test_off_has_no_retrieve_node():
    assert "retrieve_knowledge" not in graph.build_graph().nodes
    assert "retrieve_knowledge" not in graph.build_graph(ontology="off").nodes


@pytest.mark.parametrize("mode", ["ab", "abc"])
def test_on_has_retrieve_node(mode):
    assert "retrieve_knowledge" in graph.build_graph(ontology=mode).nodes
    assert "retrieve_knowledge" in graph.build_graph(check=True, ontology=mode).nodes


def test_invalid_mode():
    with pytest.raises(ValueError):
        graph.build_graph(ontology="c")
    with pytest.raises(ValueError):
        graph.build_chat_agent(ontology="on")


@pytest.fixture
def calls(monkeypatch):
    log = {"search": [], "build": []}

    def fake_search(question):
        log["search"].append(question)
        return ["term-x"]

    def fake_build(kn, tables, c_ids):
        log["build"].append((tables, c_ids))
        return Context(rules_text="규칙", tables_text="테이블", layers={"C": c_ids, "expanded": []})

    monkeypatch.setattr(graph, "search", fake_search)
    monkeypatch.setattr(graph, "get_knowledge", lambda: object())
    monkeypatch.setattr(graph, "build_context", fake_build)
    return log


STATE = {"messages": [HumanMessage("첫 질문"), HumanMessage("마지막 질문")], "selected_tables": ["prbd01n001"]}


def test_ab_does_not_search(calls):
    out = graph.retrieve_knowledge(STATE, "ab")
    assert calls["search"] == []
    assert calls["build"] == [(["prbd01n001"], [])]
    assert out == {"knowledge_rules": "규칙", "knowledge_tables": "테이블",
                   "knowledge_layers": {"C": [], "expanded": []}}


def test_abc_searches_last_question(calls):
    out = graph.retrieve_knowledge(STATE, "abc")
    assert calls["search"] == ["마지막 질문"]
    assert calls["build"] == [(["prbd01n001"], ["term-x"])]
    assert out["knowledge_layers"]["C"] == ["term-x"]


def test_failure_returns_empty(calls, monkeypatch):
    def boom():
        raise RuntimeError("DB 없음")

    monkeypatch.setattr(graph, "get_knowledge", boom)
    assert graph.retrieve_knowledge(STATE, "abc") == {
        "knowledge_rules": "", "knowledge_tables": "", "knowledge_layers": {}}


def test_empty_knowledge_keeps_off_prompt(monkeypatch):
    """조립 글이 비면 generate_query 프롬프트는 off와 같다."""
    seen = []

    class FakeLLM:
        def bind_tools(self, tools):
            return self

    def fake_invoke(llm, messages):
        seen.append(messages[0].content)
        return AIMessage("답")

    monkeypatch.setattr(graph, "get_llm", lambda: FakeLLM())
    monkeypatch.setattr(graph, "invoke_with_retry", fake_invoke)
    monkeypatch.setattr(graph, "schema_card", lambda tables: "(카드)")
    base = {"messages": [HumanMessage("q")], "selected_tables": ["prbd01n001"], "attempts": 0}
    graph.generate_query(base)
    graph.generate_query({**base, "knowledge_rules": "", "knowledge_tables": ""})
    graph.generate_query({**base, "knowledge_rules": "규칙글", "knowledge_tables": "A층"})
    assert seen[0] == seen[1]
    assert "## 도메인 규칙\n규칙글\n" in seen[2] and "(카드)\n\nA층" in seen[2]
