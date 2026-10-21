import pytest

from portfolio_agent.tools import _position_context, run_query

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
