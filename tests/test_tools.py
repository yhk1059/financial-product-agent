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
