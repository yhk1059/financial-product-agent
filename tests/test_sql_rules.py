"""--enforce 규칙(sql_rules) 테스트 (DB·LLM 불필요)."""

import pytest

from portfolio_agent import sql_rules
from portfolio_agent.sql_rules import applied_conditions, ordered_columns, result_note, rewrite, violations

CREDIT = ["AAA", "AA+", "AA0", "AA-", "A+"]
ORDERED = {"crd_grd": ("신용등급", CREDIT), "zrin_fd_ivst_risk_gcd": ("투자위험등급", ["1", "2", "3"])}


# ───────────────────────── 재작성 ─────────────────────────

def test_rewrite_desc_nulls_last():
    out = rewrite("SELECT a FROM t ORDER BY a DESC, b LIMIT 5")
    assert "ORDER BY a DESC NULLS LAST, b" in out


def test_rewrite_keeps_asc_only_sql_as_is():
    sql = "select a from t order by a asc"
    assert rewrite(sql) == sql


def test_rewrite_keeps_unparsable_sql():
    sql = "SELECT a FROM t ORDER BY (a DESC"
    assert rewrite(sql) == sql


# ───────────────────────── 등급·시각 함수 ─────────────────────────

def test_ordered_columns_from_terms():
    class KN:
        items = {"credit_rating": {"label": "신용등급"}, "plain": {"label": "보통"}}
        terms = {"credit_rating": {"order_values": CREDIT, "canonical": [("Bond.crd_grd", None)]},
                 "plain": {"order_values": None, "canonical": [("Bond.pd_nm", None)]}}

    assert ordered_columns(KN) == {"crd_grd": ("신용등급", CREDIT)}


@pytest.mark.parametrize("cond", ["crd_grd >= 'AA-'", "'AA-' <= crd_grd", "b.crd_grd BETWEEN 'AA-' AND 'AAA'",
                                  "zrin_fd_ivst_risk_gcd <= '2'"])
def test_grade_string_comparison_is_error(cond):
    out = violations(f"SELECT * FROM prbd01n001 b WHERE {cond}", ORDERED)
    assert len(out) == 1 and "IN (...)" in out[0]


def test_grade_message_lists_order():
    (msg,) = violations("SELECT * FROM prbd01n001 WHERE crd_grd >= 'AA-'", ORDERED)
    assert "신용등급" in msg and "AAA, AA+, AA0, AA-, A+" in msg


@pytest.mark.parametrize("cond", ["crd_grd IN ('AAA', 'AA+')", "zrin_fd_ivst_risk_gcd <= 2",
                                  "pd_risk_gcd >= '15'", "crd_grd = 'AAA'"])
def test_allowed_comparisons_pass(cond):
    assert violations(f"SELECT * FROM t WHERE {cond}", ORDERED) == []


@pytest.mark.parametrize("fn", ["CURRENT_DATE", "CURRENT_TIMESTAMP", "now()", "LOCALTIMESTAMP", "CURRENT_TIME"])
def test_now_functions_are_error(fn):
    (msg,) = violations(f"SELECT * FROM prbd01n001 WHERE expr_dt <= {fn}", {})
    assert "DATE '2026-08-21'" in msg


# ───────────────────────── 알림 ─────────────────────────

def test_result_note():
    assert result_note(199) == ""
    assert "COUNT" in result_note(200) and "LIMIT 200" in result_note(200)


# ───────────────────────── 적용 조건 ─────────────────────────

CATALOG = {"tables": {
    "prbd01n001": {"columns": [
        {"name": "crd_grd", "comment": "적용신용등급", "null_pct": 18.4},
        {"name": "pd_nm", "comment": "상품명", "null_pct": 2.0},
        {"name": "bond_yld", "comment": "", "null_pct": 0.0},
    ]},
    "prfd01n001": {"columns": [
        {"name": "pd_nm", "comment": "펀드명", "null_pct": 50.0},
        {"name": "rtn_1y", "comment": "1년수익률", "null_pct": 30.0},
    ]},
}}


@pytest.fixture(autouse=True)
def fake_catalog(monkeypatch):
    monkeypatch.setattr(sql_rules, "load_catalog", lambda: CATALOG)


def test_null_disclosed_when_missing_rate_high():
    out = applied_conditions(["SELECT * FROM prbd01n001 WHERE crd_grd IS NOT NULL"])
    assert out == "\n\n[적용 조건]\n- 적용신용등급: 값이 없는 행 제외"


def test_null_not_disclosed_when_missing_rate_low():
    assert applied_conditions(["SELECT * FROM prbd01n001 WHERE pd_nm IS NOT NULL"]) == ""


@pytest.mark.parametrize("cond", ["rtn_1y > 0", "rtn_1y <> 0", "rtn_1y <> '0'", "rtn_1y != 0", "0 <> rtn_1y"])
def test_zero_excluded(cond):
    assert applied_conditions([f"SELECT * FROM prfd01n001 WHERE {cond}"]) == "\n\n[적용 조건]\n- 1년수익률: 0인 행 제외"


def test_zero_uses_column_name_without_comment():
    assert "bond_yld: 0인 행 제외" in applied_conditions(["SELECT * FROM prbd01n001 WHERE bond_yld > 0"])


def test_as_of_date_and_dedup():
    sql = "SELECT * FROM prbd01n001 WHERE mat_dt <= DATE '2026-08-21' + INTERVAL '3 years' AND crd_grd IS NOT NULL"
    out = applied_conditions([sql, sql])
    assert out == "\n\n[적용 조건]\n- 기준일: 2026-08-21 (데이터 스냅샷)\n- 적용신용등급: 값이 없는 행 제외"


def test_no_conditions():
    assert applied_conditions(["SELECT COUNT(*) FROM prbd01n001 WHERE crd_grd IN ('AAA')", "not sql ((("]) == ""


def test_ambiguous_column_skipped():
    """pd_nm은 두 테이블에 다 있으므로, 두 테이블을 함께 쓰면 어느 쪽인지 몰라 건너뛴다."""
    both = "SELECT * FROM prbd01n001 b, prfd01n001 f WHERE f.pd_nm IS NOT NULL"
    assert applied_conditions([both]) == ""
    assert "펀드명: 값이 없는 행 제외" in applied_conditions(["SELECT * FROM prfd01n001 WHERE pd_nm IS NOT NULL"])
