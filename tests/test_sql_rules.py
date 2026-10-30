"""--enforce 규칙(sql_rules) 테스트 (DB·LLM 불필요)."""

import pytest

from portfolio_agent import sql_rules
from portfolio_agent.sql_rules import (applied_conditions, name_literals, ordered_columns, result_note, rewrite,
                                       violations)

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
    "pref01n001": {"columns": [
        {"name": "cu_charge_rt", "type": "text", "comment": "총보수요율", "null_pct": 90.0},
        {"name": "du_last_aum", "type": "numeric", "comment": "최종AUM", "null_pct": 1.0},
        {"name": "pd_abrv_nm", "type": "text", "comment": "상품약어명", "null_pct": 0.0},
        {"name": "pd_nm", "type": "text", "comment": "상품명", "null_pct": 0.0},
        {"name": "pd_grp_no", "type": "text", "comment": "", "null_pct": 0.0},
    ]},
    "pref02n001": {"columns": [
        {"name": "cu_charge_rt", "type": "numeric", "comment": "연간보수율", "null_pct": 5.0},
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
    """pd_nm은 두 테이블에 다 있으므로, 별칭 없이 두 테이블을 함께 쓰면 어느 쪽인지 몰라 건너뛴다."""
    both = "SELECT * FROM prbd01n001 b, prfd01n001 f WHERE pd_nm IS NOT NULL"
    assert applied_conditions([both]) == ""
    assert "펀드명: 값이 없는 행 제외" in applied_conditions(["SELECT * FROM prfd01n001 WHERE pd_nm IS NOT NULL"])


def test_alias_resolves_owner():
    both = "SELECT * FROM prbd01n001 b, prfd01n001 f WHERE f.pd_nm IS NOT NULL"
    assert applied_conditions([both]) == "\n\n[적용 조건]\n- 펀드명: 값이 없는 행 제외"


@pytest.mark.parametrize("cond", ["cu_charge_rt::numeric > 0", "CAST(cu_charge_rt AS NUMERIC) > 0",
                                  "NULLIF(cu_charge_rt::numeric, 0) IS NOT NULL", "cu_charge_rt <> '0'"])
def test_cast_zero_excluded_disclosed(cond):
    out = applied_conditions([f"SELECT AVG(cu_charge_rt::numeric) FROM pref01n001 WHERE {cond}"])
    assert "- 총보수요율: 0인 행 제외" in out


def test_union_discloses_per_select():
    """국내·해외를 UNION하면 국내 SELECT의 0 제외만 국내 컬럼 이름으로 밝힌다."""
    out = applied_conditions([UNION.format(dom="WHERE cu_charge_rt::numeric > 0")])
    assert out == "\n\n[적용 조건]\n- 총보수요율: 0인 행 제외"


# ───────────────────────── 0 = 미입력 ─────────────────────────

ZERO = frozenset({("pref01n001", "cu_charge_rt"), ("pref01n001", "du_last_aum")})
UNION = ("SELECT '국내', AVG(cu_charge_rt::numeric) FROM pref01n001 {dom} "
         "UNION ALL SELECT '해외', AVG(cu_charge_rt) FROM pref02n001")


def test_zero_missing_columns_from_rule():
    class KN:
        rules = {"R-zero-missing": {"targets": ["Dom.cu_charge_rt", "Dom"]}}
        attributes = {"Dom.cu_charge_rt": {"concept_id": "Dom", "column_name": "cu_charge_rt"}}
        concepts = {"Dom": {"table_name": "pref01n001"}}

    assert sql_rules.zero_missing_columns(KN) == {("pref01n001", "cu_charge_rt")}


def test_union_only_domestic_select_violates():
    (msg,) = violations(UNION.format(dom="WHERE pd_grp_no = 'ETF'"), {}, ZERO)
    assert msg.startswith("pref01n001.cu_charge_rt(총보수요율)의 0은 미입력") and "cu_charge_rt::numeric > 0" in msg


@pytest.mark.parametrize("cond", ["cu_charge_rt::numeric > 0", "cu_charge_rt <> '0'", "cu_charge_rt::numeric >= 0.01"])
def test_zero_exclusion_passes(cond):
    assert violations(UNION.format(dom=f"WHERE {cond}"), {}, ZERO) == []


def test_nullif_passes():
    assert violations("SELECT AVG(NULLIF(cu_charge_rt::numeric, 0)) FROM pref01n001", {}, ZERO) == []


def test_overseas_alone_passes():
    assert violations("SELECT AVG(cu_charge_rt) FROM pref02n001 ORDER BY 1", {}, ZERO) == []


@pytest.mark.parametrize("sql", [
    "SELECT pd_nm FROM pref01n001 WHERE cu_charge_rt != '' ORDER BY cu_charge_rt LIMIT 3",
    "SELECT pd_nm FROM pref01n001 p WHERE p.cu_charge_rt::numeric < 0.1",
    "SELECT MAX(du_last_aum) FROM pref01n001",
])
def test_zero_missing_computed_is_error(sql):
    assert len(violations(sql, {}, ZERO)) == 1


@pytest.mark.parametrize("sql", [
    "SELECT pd_nm, cu_charge_rt FROM pref01n001 WHERE pd_grp_no = 'ETF'",   # 보여 주기만
    "SELECT COUNT(*) FROM pref01n001 WHERE cu_charge_rt = '0'",             # 0인 것을 세기
    "SELECT AVG(fee) FROM (SELECT cu_charge_rt::numeric AS fee FROM pref01n001 WHERE cu_charge_rt::numeric > 0) t",
])
def test_zero_missing_other_uses_pass(sql):
    assert violations(sql, {}, ZERO) == []


def test_zero_check_off_by_default():
    assert violations("SELECT MAX(du_last_aum) FROM pref01n001", {}) == []


def test_zero_message_for_numeric_column():
    (msg,) = violations("SELECT MAX(du_last_aum) FROM pref01n001", {}, ZERO)
    assert "du_last_aum > 0" in msg and "::numeric" not in msg


# ───────────────────────── 이름 ─────────────────────────

NAMES = {("prbd01n001", "pd_pbcm"): "발행기관", ("pref01n001", "pd_abrv_nm"): "상품",
         ("domestic_etf_holdings", "component_name"): "구성종목"}


def test_name_columns_from_terms():
    class KN:
        terms = {"issuer": {"canonical": [("Issuer", None), ("Bond.pd_pbcm", None)]}}
        attributes = {"Bond.pd_pbcm": {"concept_id": "Bond", "column_name": "pd_pbcm"}}
        concepts = {"Bond": {"table_name": "prbd01n001"}}

    assert sql_rules.name_columns(KN) == {("prbd01n001", "pd_pbcm"): "발행기관"}


def test_name_literals_eq_and_in():
    sql = ("SELECT * FROM prbd01n001 b WHERE b.pd_pbcm = '가나은행' AND 'x' = 'x' "
           "UNION SELECT * FROM pref01n001 WHERE pd_abrv_nm IN ('ABC 100', 'ABC 200')")
    assert name_literals(sql, NAMES) == [("발행기관", "prbd01n001", "pd_pbcm", "가나은행"),
                                         ("상품", "pref01n001", "pd_abrv_nm", "ABC 100"),
                                         ("상품", "pref01n001", "pd_abrv_nm", "ABC 200")]


def test_name_literals_ignore_like_and_other_columns():
    sql = "SELECT * FROM prbd01n001 WHERE pd_pbcm LIKE '%은행%' AND crd_grd = 'AAA'"
    assert name_literals(sql, NAMES) == []
