"""그래프 구성과 온톨로지 연결 테스트 (DB·LLM 불필요)."""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph import END

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


class FakeLLM:
    """with_structured_output가 자기 자신을 돌려주고, 받은 스키마를 기록한다."""

    def __init__(self):
        self.schemas = []

    def with_structured_output(self, schema, method=None):
        assert method == "json_schema"
        self.schemas.append(schema)
        return self


@pytest.fixture
def fake_llm(monkeypatch):
    """invoke_with_retry가 outputs를 차례로 돌려준다(예외면 던진다). 호출된 메시지는 llm.calls에 남긴다."""
    llm = FakeLLM()
    llm.outputs, llm.calls = [], []

    def fake_invoke(runnable, messages):
        llm.calls.append(messages)
        out = llm.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(graph, "get_llm", lambda: llm)
    monkeypatch.setattr(graph, "invoke_with_retry", fake_invoke)
    monkeypatch.setattr(graph, "schema_card", lambda tables: "(카드)")
    return llm


BASE = {"messages": [HumanMessage("q")], "selected_tables": ["prbd01n001"], "attempts": 0}


def _step(action="query", sql="", answer=""):
    return graph.QueryStep(action=action, sql=sql, answer=answer)


def test_empty_knowledge_keeps_off_prompt(fake_llm):
    """조립 글이 비면 generate_query 프롬프트는 off와 같다."""
    fake_llm.outputs = [_step("answer", answer="답")] * 3
    graph.generate_query(BASE)
    graph.generate_query({**BASE, "knowledge_rules": "", "knowledge_tables": ""})
    graph.generate_query({**BASE, "knowledge_rules": "규칙글", "knowledge_tables": "A층"})
    seen = [c[0].content for c in fake_llm.calls]
    assert seen[0] == seen[1]
    assert "## 도메인 규칙\n규칙글\n" in seen[2] and "(카드)\n\nA층" in seen[2]


def test_query_step_makes_tool_call(fake_llm):
    fake_llm.outputs = [_step("query", sql=" SELECT 1 ")]
    msg = graph.generate_query(BASE)["messages"][0]
    assert fake_llm.schemas == [graph.QueryStep]
    assert len(msg.tool_calls) == 1
    call = msg.tool_calls[0]
    assert call["name"] == "run_query" and call["args"]["query"] == "SELECT 1" and call["id"]
    assert msg.id
    assert graph.route_after_generate({"messages": [msg]}) == "check_query"


def test_answer_step_ends(fake_llm):
    fake_llm.outputs = [_step("answer", answer="데이터에 없는 정보입니다.")]
    msg = graph.generate_query(BASE)["messages"][0]
    assert msg.text == "데이터에 없는 정보입니다." and not msg.tool_calls
    assert graph.route_after_generate({"messages": [msg]}) == END


@pytest.mark.parametrize("first", [ValueError("JSON 파싱 실패"), _step("query", sql="  "), _step("answer")])
def test_retries_once_then_succeeds(fake_llm, first):
    fake_llm.outputs = [first, _step("query", sql="SELECT 2")]
    msg = graph.generate_query(BASE)["messages"][0]
    assert len(fake_llm.calls) == 2
    assert msg.tool_calls[0]["args"]["query"] == "SELECT 2"
    assert "generation_error" not in msg.response_metadata


def test_two_failures_record_generation_error(fake_llm):
    fake_llm.outputs = [ValueError("JSON 파싱 실패"), _step("query", sql="")]
    msg = graph.generate_query(BASE)["messages"][0]
    assert len(fake_llm.calls) == 2
    assert not msg.tool_calls
    assert msg.text == graph.GENERATION_FAILED
    error = msg.response_metadata["generation_error"]
    assert "JSON 파싱 실패" in error and "sql이 비어 있음" in error
    assert graph.route_after_generate({"messages": [msg]}) == END


def test_give_up_uses_final_answer(fake_llm):
    fake_llm.outputs = [graph.FinalAnswer(answer="여기까지 확인했습니다.")]
    state = {**BASE, "attempts": graph.settings.max_query_attempts}
    msg = graph.generate_query(state)["messages"][0]
    assert fake_llm.schemas == [graph.FinalAnswer]
    assert "sql" not in graph.FinalAnswer.model_fields
    assert msg.text == "여기까지 확인했습니다." and not msg.tool_calls
    assert "## 주의" in fake_llm.calls[0][0].content


def test_llm_history_turns_tool_messages_into_text():
    call = {"name": "run_query", "args": {"query": "SELECT 1"}, "id": "call_a"}
    messages = [
        HumanMessage("q"),
        AIMessage(content="", tool_calls=[call, {**call, "args": {"query": "SELECT 2"}, "id": "call_b"}], id="m1"),
        ToolMessage("col\n1", tool_call_id="call_a"),
        AIMessage("답"),
    ]
    original = list(messages)
    history = graph._llm_history(messages)
    assert messages == original and messages[1].tool_calls  # state는 그대로
    assert not any(isinstance(m, ToolMessage) for m in history)
    assert not any(isinstance(m, AIMessage) and m.tool_calls for m in history)
    assert history[1].text == "[실행한 SQL]\nSELECT 1\n\nSELECT 2"
    assert isinstance(history[2], HumanMessage) and history[2].text == "[실행 결과]\ncol\n1"
    assert history[0] is messages[0] and history[3] is messages[3]


def test_generate_query_sends_text_history(fake_llm):
    call = {"name": "run_query", "args": {"query": "SELECT 1"}, "id": "call_a"}
    state = {**BASE, "messages": [HumanMessage("q"), AIMessage(content="", tool_calls=[call], id="m1"),
                                  ToolMessage("1", tool_call_id="call_a")]}
    fake_llm.outputs = [_step("answer", answer="1개입니다.")]
    graph.generate_query(state)
    sent = fake_llm.calls[0][1:]
    assert [type(m) for m in sent] == [HumanMessage, AIMessage, HumanMessage]
    assert sent[2].text == "[실행 결과]\n1"


def test_ask_records_generation_error():
    """cli.ask는 generate_query의 generation_error를 AskTrace.generation_errors에 모은다."""
    from portfolio_agent import cli

    class FakeAgent:
        def stream(self, inputs, config, stream_mode):
            yield {"select_tables": {"selected_tables": ["prbd01n001"], "attempts": 0}}
            yield {"generate_query": {"messages": [
                AIMessage(graph.GENERATION_FAILED, response_metadata={"generation_error": "오류 요약"})]}}

    trace = cli.ask(FakeAgent(), "q", "t")
    assert trace.generation_errors == ["오류 요약"]
    assert trace.answer == graph.GENERATION_FAILED and trace.queries == []
