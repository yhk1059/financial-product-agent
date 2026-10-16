import psycopg
import pytest

from portfolio_agent import db

pytestmark = pytest.mark.db


def test_select_works():
    cols, rows = db.fetch("SELECT count(*) AS n FROM pref01n001")
    assert cols == ["n"] and rows[0][0] > 0


def test_session_is_read_only():
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        db.fetch("CREATE TEMP TABLE t_guard_check (x int)")
