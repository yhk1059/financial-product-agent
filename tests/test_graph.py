"""그래프 구성과 온톨로지 연결 테스트 (DB·LLM 불필요)."""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph import END

from portfolio_agent import graph
from portfolio_agent.context import Context


def test_off_has_no_retrieve_node():
    assert "retrieve_knowledge" not in graph.build_graph().nodes
    assert "retrieve_knowledge" not in graph.build_graph(ontology="off").nodes


@pytest.mark.parametrize("mode", ["ab", "abc", "oracle"])
def test_on_has_retrieve_node(mode):
    assert "retrieve_knowledge" in graph.build_graph(ontology=mode).nodes


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


def test_oracle_uses_given_ids_without_search(calls):
    out = graph.retrieve_knowledge({**STATE, "oracle_ids": ["R-topn-nulls", "etf"]}, "oracle")
    assert calls["search"] == []
    assert calls["build"] == [(["prbd01n001"], ["R-topn-nulls", "etf"])]
    assert out["knowledge_layers"]["C"] == ["R-topn-nulls", "etf"]


def test_oracle_without_ids_is_ab(calls):
    graph.retrieve_knowledge(STATE, "oracle")
    assert calls["build"] == [(["prbd01n001"], [])]


def test_chat_cli_has_no_oracle():
    from portfolio_agent.config import ONTOLOGY_MODES
    assert "oracle" not in ONTOLOGY_MODES


def test_ask_passes_oracle_ids():
    from portfolio_agent import cli

    seen = []

    class FakeAgent:
        def stream(self, inputs, config, stream_mode):
            seen.append(inputs)
            yield {"generate_query": {"messages": [AIMessage("답")]}}

    cli.ask(FakeAgent(), "q", "t")
    cli.ask(FakeAgent(), "q", "t", oracle_ids=["etf"])
    assert "oracle_ids" not in seen[0]
    assert seen[1]["oracle_ids"] == ["etf"]


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
    assert graph.route_after_generate({"messages": [msg]}) == "count_attempt"


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


# ───────────────────────── enforce ─────────────────────────

ENF_CATALOG = {"tables": {"prbd01n001": {"columns": [
    {"name": "crd_grd", "comment": "적용신용등급", "null_pct": 18.4},
    {"name": "pd_nm", "comment": "상품명", "null_pct": 0.0},
]}}}


def _turn_state(attempts=1):
    """지난 턴의 성공 SQL, 이번 턴의 실패 SQL과 성공 SQL이 든 대화."""
    def call(cid, sql):
        return AIMessage(content="", tool_calls=[{"name": "run_query", "args": {"query": sql}, "id": cid}], id=cid)

    return {**BASE, "attempts": attempts, "messages": [
        HumanMessage("지난 질문"),
        call("c0", "SELECT pd_nm FROM prbd01n001 WHERE pd_nm IS NOT NULL AND expr_dt > DATE '2026-08-21'"),
        ToolMessage("1행", tool_call_id="c0"),
        AIMessage("지난 답"),
        HumanMessage("q"),
        call("c1", "SELECT COUNT(*) FROM prbd01n001 WHERE crd_grd >= 'AA-'"),
        ToolMessage("Error: 등급", tool_call_id="c1"),
        call("c2", "SELECT COUNT(*) FROM prbd01n001 WHERE crd_grd IS NOT NULL AND crd_grd IN ('AAA')"),
        ToolMessage("count\n3\n(1행)", tool_call_id="c2"),
    ]}


def test_turn_sqls_only_this_turn_successes():
    assert graph._turn_sqls(_turn_state()["messages"]) == [
        "SELECT COUNT(*) FROM prbd01n001 WHERE crd_grd IS NOT NULL AND crd_grd IN ('AAA')"]


def test_enforce_rewrites_tool_call_sql(fake_llm):
    fake_llm.outputs = [_step("query", sql="SELECT pd_nm FROM prbd01n001 ORDER BY crd_grd DESC")] * 2
    on = graph.generate_query(BASE, enforce=True)["messages"][0].tool_calls[0]["args"]["query"]
    off = graph.generate_query(BASE)["messages"][0].tool_calls[0]["args"]["query"]
    assert on.endswith("ORDER BY crd_grd DESC NULLS LAST")
    assert off == "SELECT pd_nm FROM prbd01n001 ORDER BY crd_grd DESC"


@pytest.mark.parametrize("give_up", [False, True])
def test_enforce_appends_applied_conditions(fake_llm, monkeypatch, give_up):
    from portfolio_agent import sql_rules
    monkeypatch.setattr(sql_rules, "load_catalog", lambda: ENF_CATALOG)
    state = _turn_state(graph.settings.max_query_attempts if give_up else 1)
    out = graph.FinalAnswer(answer="3개입니다.") if give_up else _step("answer", answer="3개입니다.")
    fake_llm.outputs = [out, out]
    on = graph.generate_query(state, enforce=True)["messages"][0].text
    off = graph.generate_query(state)["messages"][0].text
    assert on == "3개입니다.\n\n[적용 조건]\n- 적용신용등급: 값이 없는 행 제외"  # 지난 턴의 기준일은 넣지 않는다
    assert off == "3개입니다."


def test_enforce_graph_builds_without_knowledge(monkeypatch):
    def boom():
        raise RuntimeError("DB 없음")

    monkeypatch.setattr(graph, "get_knowledge", boom)
    nodes = graph.build_graph(enforce=True).nodes
    assert "generate_query" in nodes and "run_query" in nodes


# ───────────────────────── slots ─────────────────────────

def test_slot_nodes_only_with_slots():
    assert not {"analyze_question", "resolve_slots"} & set(graph.build_graph().nodes)
    assert {"analyze_question", "resolve_slots"} <= set(graph.build_graph(slots=True).nodes)


def test_analyze_question_returns_slots(fake_llm):
    from portfolio_agent.slots import QuestionSlots

    fake_llm.outputs = [QuestionSlots(names=[], grades=[], values=["MMF"])]
    out = graph.analyze_question(BASE)
    assert fake_llm.schemas == [QuestionSlots]
    assert out == {"slots": {"names": [], "grades": [], "values": ["MMF"]}}


def test_analyze_question_failure_is_empty(fake_llm):
    fake_llm.outputs = [ValueError("parse")]
    assert graph.analyze_question(BASE) == {"slots": {}}


def test_resolve_slots_empty_or_failure(monkeypatch):
    assert graph.resolve_slots({**BASE, "slots": {}}) == {"resolved": [], "resolved_text": ""}

    def boom():
        raise RuntimeError("no db")

    monkeypatch.setattr(graph, "get_knowledge", boom)
    out = graph.resolve_slots({**BASE, "slots": {"names": [], "grades": [], "values": ["x"]}})
    assert out == {"resolved": [], "resolved_text": ""}


def test_resolve_slots_renders(monkeypatch):
    seen = {}

    def fake_resolve(slots, kn, catalog, lookup, tables, dialogue):
        seen.update(tables=tables, dialogue=dialogue)
        return [{"slot": "value", "status": "confirmed", "text": "MMF", "fragment": "t.c IN ('a', 'b')"},
                {"slot": "value", "status": "skipped", "text": "x", "fragment": ""}]

    monkeypatch.setattr(graph, "get_knowledge", lambda: object())
    monkeypatch.setattr(graph, "load_catalog", lambda: {})
    monkeypatch.setattr(graph, "resolve", fake_resolve)
    out = graph.resolve_slots({**BASE, "slots": {"values": ["MMF"]}}, lookup=object())
    assert out["resolved_text"] == "- 범주 'MMF' → t.c IN ('a', 'b')"
    assert seen == {"tables": ["prbd01n001"], "dialogue": "사용자: q"}


def test_generate_query_uses_resolved(fake_llm):
    """확인된 값이 있으면 프롬프트 끝에 '## 확인된 값'이 붙고, 비면 칸 없는 프롬프트와 같다."""
    fake_llm.outputs = [_step("answer", answer="답")] * 3
    graph.generate_query(BASE)
    graph.generate_query({**BASE, "resolved_text": ""})
    graph.generate_query({**BASE, "resolved_text": "- 범주 'MMF' → X"})
    seen = [c[0].content for c in fake_llm.calls]
    assert seen[0] == seen[1] and "## 확인된 값" not in seen[0]
    assert seen[2].startswith(seen[0]) and seen[2].endswith("이 조각을 그대로 씁니다.\n- 범주 'MMF' → X")


def test_enforce_appends_name_interpretation(fake_llm, monkeypatch):
    from portfolio_agent import sql_rules

    monkeypatch.setattr(sql_rules, "load_catalog", lambda: ENF_CATALOG)
    fake_llm.outputs = [_step("answer", answer="답")]
    resolved = [{"slot": "name", "kind": "발행기관", "text": "가나", "status": "candidates", "values": ["AAA"],
                 "note": ""}]
    out = graph.generate_query({**_turn_state(), "resolved": resolved}, enforce=True)["messages"][0].text
    assert out.endswith("- 이름 해석: '가나' → 'AAA'")


class _ScriptedLLM:
    """실제 그래프용: 스키마별로 정해 둔 출력을 차례로 돌려준다."""

    def __init__(self, outputs):
        self.outputs, self.schemas = outputs, []

    def with_structured_output(self, schema, method=None):
        self.schemas.append(schema.__name__)
        return self


def test_slot_graph_end_to_end(monkeypatch):
    """--enforce --slots 그래프: 칸은 턴마다 한 번만 뽑고, run_query가 상태의 resolved로 집합 불일치를 막는다(G4).
    두 번 막은 뒤 세 번째는 실행하고 알림만 붙인다."""
    from portfolio_agent import tools
    from portfolio_agent.slots import QuestionSlots

    resolved = [{"slot": "grade", "kind": "", "text": "신용등급 'AA-' 이상", "status": "confirmed",
                 "table": "prbd01n001", "column": "crd_grd", "values": ["AAA", "AA+"], "note": "",
                 "fragment": "prbd01n001.crd_grd IN ('AAA', 'AA+')"}]
    bad = "SELECT COUNT(*) FROM prbd01n001 WHERE crd_grd = 'AAA'"
    outputs = [graph.TableSelection(tables=["prbd01n001"]), QuestionSlots(names=[], grades=[], values=[]),
               _step(sql=bad), _step(sql=bad), _step(sql=bad), graph.FinalAnswer(answer="끝")]
    llm = _ScriptedLLM(outputs)
    monkeypatch.setattr(graph, "get_llm", lambda: llm)
    monkeypatch.setattr(graph, "invoke_with_retry", lambda runnable, messages: outputs.pop(0))
    monkeypatch.setattr(graph, "schema_card", lambda tables: "(카드)")
    monkeypatch.setattr(graph, "table_overview", lambda: "(개요)")
    def resolve_slots(state):  # 노드 이름은 함수 이름에서 온다
        return {"resolved": resolved, "resolved_text": "- x"}

    monkeypatch.setattr(graph, "resolve_slots", resolve_slots)
    monkeypatch.setattr(graph, "_enforce_tool", lambda slot_checks=False: tools.make_run_query(
        {}, slot_checks=slot_checks))
    monkeypatch.setattr(tools.db, "fetch", lambda sql: (["n"], [(1,)]))
    agent = graph.build_graph(enforce=True, slots=True)
    final = agent.invoke({"messages": [HumanMessage("q")]})
    results = [m.text for m in final["messages"] if isinstance(m, ToolMessage)]
    assert [r.startswith("Error:") for r in results] == [True, True, False]
    assert "crd_grd IN ('AAA', 'AA+')" in results[0] and results[2].endswith("이 조각을 그대로 쓰세요.)")
    assert llm.schemas.count("QuestionSlots") == 1
    assert final["messages"][-1].text == "끝"
