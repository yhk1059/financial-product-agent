import pytest

from portfolio_agent import tools
from portfolio_agent.tools import _position_context, make_run_query, run_query

SQL = 'SELECT COUNT(*) FROM "h" AS "a" JOIN "p" AS "b" ON "a"."item_id" = "b" = "pd_itm_no" LIMIT 200'


def test_position_context_marks_token():
    pos = SQL.index('= "pd_itm_no"') + 1  # PostgreSQL 위치는 1부터 센다
    out = _position_context(SQL, str(pos))
    assert '"b" >>=<< "pd_itm_no"' in out
    assert out.startswith("\n  …")


@pytest.mark.parametrize("position", [None, "", "0", "abc", str(len(SQL) + 1)])
def test_position_context_ignores_bad_position(position):
    assert _position_context(SQL, position) == ""


@pytest.mark.db
def test_run_query_db_error_shows_position():
    out = run_query.invoke({"query": "SELECT 1 FROM pref01n001 a JOIN pref02n001 b ON a.pd_itm_no = b=pd_itm_no"})
    assert out.startswith('Error: syntax error at or near "="')
    assert '"b" >>=<<' in out


ORDERED = {"crd_grd": ("신용등급", ["AAA", "AA+", "AA0", "AA-"])}


@pytest.fixture
def fetched(monkeypatch):
    """db.fetch를 가짜로 바꾼다. 받은 SQL은 calls에 남고, rows만큼 행을 돌려준다."""
    state = {"calls": [], "rows": 1}

    def fake_fetch(sql):
        state["calls"].append(sql)
        return ["n"], [(i,) for i in range(state["rows"])]

    monkeypatch.setattr(tools.db, "fetch", fake_fetch)
    return state


def test_enforce_violation_is_error_without_fetch(fetched):
    out = make_run_query(ORDERED).invoke({"query": "SELECT * FROM prbd01n001 WHERE crd_grd >= 'AA-'"})
    assert out.startswith("Error: ") and "IN (...)" in out
    assert fetched["calls"] == []


def test_enforce_notes_limit(fetched):
    fetched["rows"] = 200
    out = make_run_query(ORDERED).invoke({"query": "SELECT pd_nm FROM prbd01n001"})
    assert out.endswith("개수는 COUNT로 구할 것)")
    assert len(fetched["calls"]) == 1


def test_default_has_no_rules(fetched):
    fetched["rows"] = 200
    out = run_query.invoke({"query": "SELECT * FROM prbd01n001 WHERE crd_grd >= 'AA-'"})
    assert not out.startswith("Error:") and "COUNT" not in out
    assert out.endswith("(총 200행 중 50행 표시)")


# ───────────────────────── enforce: 0 = 미입력·이름 ─────────────────────────

NAMES = {("prbd01n001", "pd_pbcm"): "발행기관", ("domestic_etf_holdings", "component_name"): "구성종목"}
STORED = {"pd_pbcm": ["(주)가나다은행", "가나다캐피탈"], "component_name": ["가나전자", "가나전자우"]}


def fake_lookup(table, column, text, mode):
    """저장된 값: STORED. exact는 같은 값, partial은 text를 포함하는 값."""
    values = STORED[column]
    return [v for v in values if (v == text if mode == "exact" else text in v)]


def test_zero_missing_is_error_without_fetch(fetched):
    zero = frozenset({("pref01n001", "cu_charge_rt")})
    out = make_run_query({}, zero).invoke({"query": "SELECT AVG(cu_charge_rt::numeric) FROM pref01n001"})
    assert out.startswith("Error: [규칙] ") and "NULLIF(cu_charge_rt, '')::numeric > 0" in out
    assert fetched["calls"] == []


def test_unknown_name_with_candidates_is_error(fetched):
    tool = make_run_query({}, names=NAMES, name_lookup=fake_lookup)
    out = tool.invoke({"query": "SELECT COUNT(*) FROM prbd01n001 WHERE pd_pbcm = '가나다'"})
    assert out.startswith("Error: [규칙] 발행기관 이름 '가나다'는 prbd01n001.pd_pbcm에 정확히 일치하는 값이 없습니다")
    assert "'(주)가나다은행', '가나다캐피탈'" in out
    assert fetched["calls"] == []


def test_exact_name_runs(fetched):
    tool = make_run_query({}, names=NAMES, name_lookup=fake_lookup)
    out = tool.invoke({"query": "SELECT COUNT(*) FROM prbd01n001 WHERE pd_pbcm = '(주)가나다은행'"})
    assert out == "n\n0\n(1행)"


def test_unknown_name_without_candidates_runs_with_note(fetched):
    tool = make_run_query({}, names=NAMES, name_lookup=fake_lookup)
    out = tool.invoke({"query": "SELECT COUNT(*) FROM prbd01n001 WHERE pd_pbcm = '없는기관'"})
    assert len(fetched["calls"]) == 1
    assert out.endswith("(알림: 발행기관 이름 '없는기관'는 prbd01n001.pd_pbcm에 정확히 일치하는 값이 없음)")


def test_unknown_holding_runs_with_note(fetched):
    """구성종목은 '편입 없음'이 답일 수 있어 막지 않고 알림만 붙인다."""
    tool = make_run_query({}, names=NAMES, name_lookup=fake_lookup)
    out = tool.invoke({"query": "SELECT COUNT(*) FROM domestic_etf_holdings WHERE component_name = '가나'"})
    assert len(fetched["calls"]) == 1
    assert out.endswith("비슷한 표기: '가나전자', '가나전자우')")


def test_name_check_off_by_default(fetched):
    out = make_run_query({}).invoke({"query": "SELECT COUNT(*) FROM prbd01n001 WHERE pd_pbcm = '가나다'"})
    assert out == "n\n0\n(1행)"


# ───────────────────────── SQLSTATE별 고치는 법 ─────────────────────────

@pytest.mark.parametrize("state, msg, word", [
    ("42803", 'column "x" must appear in the GROUP BY clause', "GROUP BY"),
    ("42883", "operator does not exist: text > integer", "::numeric"),
    ("22007", 'invalid input syntax for type interval: "2026-08-21"', "INTERVAL '3 years'"),
    ("42601", "syntax error at or near", "PostgreSQL"),
])
def test_sqlstate_hint(state, msg, word):
    assert word in tools.sqlstate_hint(state, msg)


def test_sqlstate_hint_unknown_is_empty():
    assert tools.sqlstate_hint("XX000", "boom") == ""


def test_setop_mismatch_hint():
    assert "COUNT(*) FROM (" in tools.sqlstate_hint("42804", "INTERSECT types bigint and text cannot be matched")


def test_repeated_failed_sql_gets_note(fetched):
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    bad = "SELECT 1 FROM nowhere"
    msgs = [HumanMessage("q"), AIMessage("", tool_calls=[{"name": "run_query", "args": {"query": bad}, "id": "c1"}]),
            ToolMessage("Error: 허용되지 않은 테이블입니다", tool_call_id="c1")]
    tool = make_run_query({}, slot_checks=True)
    out = tool.invoke({"query": bad, "state": {"messages": msgs}})
    assert out.startswith("Error:") and out.endswith(tools.REPEAT_NOTE)
    out = tool.invoke({"query": "SELECT 1 FROM prbd01n001", "state": {"messages": msgs}})
    assert tools.REPEAT_NOTE not in out
