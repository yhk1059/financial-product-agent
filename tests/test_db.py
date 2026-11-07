import psycopg
import pytest

from financial_product_agent import db

pytestmark = pytest.mark.db


def test_select_works():
    cols, rows = db.fetch("SELECT count(*) AS n FROM pref01n001")
    assert cols == ["n"] and rows[0][0] > 0


def test_session_is_read_only():
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        db.fetch("CREATE TEMP TABLE t_guard_check (x int)")


@pytest.mark.parametrize("table,column", [("pg_user", "usename"), ("prbd01n001", "pd_pbcm; DROP")])
def test_lookup_values_rejects_unknown_column(table, column):
    with pytest.raises(ValueError):
        db.lookup_values(table, column, "x", "exact")


def test_lookup_values_partial_ignores_space_and_case():
    exact = db.lookup_values("prbd01n001", "crd_grd", "AAA", "exact")
    assert exact == ["AAA"]
    assert "AAA" in db.lookup_values("prbd01n001", "crd_grd", "a a", "partial")
