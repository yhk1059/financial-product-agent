"""--enforce 규칙(sql_rules) 테스트 (DB·LLM 불필요)."""

import pytest

from financial_product_agent import sql_rules
from financial_product_agent.sql_rules import (applied_conditions, name_literals, ordered_columns, result_note, rewrite,
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
        {"name": "pd_no", "comment": "채권종목번호", "null_pct": 0.0},
        {"name": "srfc_irt", "comment": "표면이자율", "null_pct": 2.0},
        {"name": "buyable_quantity", "comment": "매수가능수량", "null_pct": 0.0},
        {"name": "std_pd_mcls_nm", "comment": "상품중분류", "null_pct": 0.0},
        {"name": "after_tax_yield", "comment": "세후수익률", "null_pct": 0.0},
    ]},
    "prfd01n001": {"columns": [
        {"name": "pd_nm", "comment": "펀드명", "null_pct": 50.0},
        {"name": "rtn_1y", "comment": "1년수익률", "null_pct": 30.0},
        {"name": "rptt_ksd_itm_no", "comment": "대표예탁원종목번호", "null_pct": 1.2},
        {"name": "fd_yr1_ern_r", "comment": "펀드 1년수익률", "null_pct": 20.0},
        {"name": "han_clas_nm", "comment": "클래스명", "null_pct": 0.0},
        {"name": "sale_yn", "comment": "판매여부", "null_pct": 0.0},
    ]},
    "domestic_etf_holdings": {"columns": [
        {"name": "item_id", "comment": "ETF 코드", "null_pct": 0.0},
        {"name": "component_name", "comment": "구성종목명", "null_pct": 0.0},
    ]},
    "pref01n001": {"columns": [
        {"name": "cu_charge_rt", "type": "text", "comment": "총보수요율", "null_pct": 90.0},
        {"name": "du_last_aum", "type": "numeric", "comment": "최종AUM", "null_pct": 1.0},
        {"name": "pd_abrv_nm", "type": "text", "comment": "상품약어명", "null_pct": 0.0},
        {"name": "pd_nm", "type": "text", "comment": "상품명", "null_pct": 0.0},
        {"name": "pd_grp_no", "type": "text", "comment": "", "null_pct": 0.0},
        {"name": "pd_itm_no", "type": "text", "comment": "상품번호", "null_pct": 0.0},
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
    assert out == "\n\n[적용 조건]\n- 총보수요율: 0인 행 제외\n- 총보수요율: 값이 없는 행 제외"  # AVG가 조용히 뺀 결측


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
    assert msg.startswith("pref01n001.cu_charge_rt(총보수요율)의 0은 미입력") and "NULLIF(cu_charge_rt, '')::numeric > 0" in msg


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


# ───────────────────────── 집계가 조용히 뺀 결측 ─────────────────────────

def test_count_column_discloses_any_missing():
    """COUNT(컬럼)·COUNT(DISTINCT 컬럼)은 결측률이 낮아도 밝힌다(개수에서 그 행이 빠진다)."""
    out = applied_conditions(["SELECT COUNT(DISTINCT rptt_ksd_itm_no) FROM prfd01n001 WHERE sale_yn = '판매중'"])
    assert "대표예탁원종목번호: 값이 없는 행 제외" in out
    assert applied_conditions(["SELECT COUNT(*) FROM prfd01n001"]) == ""


def test_avg_discloses_only_high_missing():
    out = applied_conditions(["SELECT AVG(fd_yr1_ern_r) FROM prfd01n001"])
    assert "펀드 1년수익률: 값이 없는 행 제외" in out
    assert applied_conditions(["SELECT AVG(srfc_irt) FROM prbd01n001"]) == ""  # 결측률 10% 미만


# ───────────────────────── 단위·현금성·집합 논리 ─────────────────────────

from financial_product_agent.sql_rules import rule_violations

UNITS = {"prbd01n001": ("pd_no", "buyable_quantity")}
CLASS_IDS = {"prfd01n001": frozenset({"itm_no", "rptt_ksd_itm_no"})}
CASH = ["원화예금", "설정현금액"]


@pytest.mark.parametrize("sql", [
    "SELECT AVG(srfc_irt) FROM prbd01n001 WHERE std_pd_mcls_nm = '국공채'",
    "SELECT COUNT(*) FROM prbd01n001 WHERE crd_grd = 'AAA'",
    "SELECT crd_grd, COUNT(*) FROM prbd01n001 GROUP BY crd_grd",
])
def test_bond_row_unit_is_error(sql):
    out = rule_violations(sql, UNITS, CLASS_IDS, CASH)
    assert len(out) == 1 and "COUNT(DISTINCT pd_no)" in out[0]


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FROM prbd01n001 WHERE crd_grd = 'AAA' AND buyable_quantity > 0",  # 판매 건수는 행 단위
    "SELECT COUNT(DISTINCT pd_no) FROM prbd01n001",
    "SELECT AVG(x) FROM (SELECT DISTINCT pd_no, srfc_irt AS x FROM prbd01n001) t",
    "SELECT pd_no, pd_nm FROM prbd01n001 ORDER BY after_tax_yield DESC LIMIT 3",
])
def test_bond_item_unit_passes(sql):
    assert rule_violations(sql, UNITS, CLASS_IDS, CASH) == []


def test_fund_distinct_other_column_is_error():
    out = rule_violations("SELECT COUNT(DISTINCT han_clas_nm) FROM prfd01n001", UNITS, CLASS_IDS, CASH)
    assert len(out) == 1 and "COUNT(*)" in out[0] and "han_clas_nm" in out[0]
    assert rule_violations("SELECT COUNT(DISTINCT rptt_ksd_itm_no) FROM prfd01n001", UNITS, CLASS_IDS, CASH) == []
    assert rule_violations("SELECT COUNT(*) FROM prfd01n001", UNITS, CLASS_IDS, CASH) == []


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(DISTINCT component_name) FROM domestic_etf_holdings WHERE item_id = 'X'",
    "SELECT DISTINCT h.component_name FROM domestic_etf_holdings h JOIN pref01n001 p ON p.pd_itm_no = h.item_id",
])
def test_holding_names_without_cash_exclusion_is_error(sql):
    out = rule_violations(sql, UNITS, CLASS_IDS, CASH)
    assert len(out) == 1 and "NOT IN ('원화예금', '설정현금액')" in out[0]


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(DISTINCT component_name) FROM domestic_etf_holdings WHERE item_id = 'X' "
    "AND component_name NOT IN ('원화예금', '설정현금액')",
    "SELECT COUNT(DISTINCT item_id) FROM domestic_etf_holdings WHERE component_name = '가나전자'",  # 종목으로 거름
    "SELECT p.pd_nm FROM pref01n001 p JOIN domestic_etf_holdings h ON p.pd_itm_no = h.item_id WHERE h.component_name = '가나'",
])
def test_cash_check_passes(sql):
    assert rule_violations(sql, UNITS, CLASS_IDS, CASH) == []


BOTH = "SELECT COUNT(DISTINCT item_id) FROM domestic_etf_holdings WHERE component_name IN ('가', '나')"


def test_all_of_question_needs_having():
    out = rule_violations(BOTH, UNITS, CLASS_IDS, CASH, question="가랑 나를 둘 다 담은 ETF는 몇 개야?")
    assert len(out) == 1 and "HAVING COUNT(DISTINCT component_name) = 2" in out[0]
    assert rule_violations(BOTH, UNITS, CLASS_IDS, CASH, question="가나 나를 담은 ETF") == []  # '둘 다'가 없으면 통과
    fixed = ("SELECT COUNT(*) FROM (SELECT item_id FROM domestic_etf_holdings WHERE component_name IN ('가', '나') "
             "GROUP BY item_id HAVING COUNT(DISTINCT component_name) = 2) t")
    assert rule_violations(fixed, UNITS, CLASS_IDS, CASH, question="둘 다 담은") == []


# ───────────────────────── GROUP BY 결측 그룹·비율 분모·날짜 기준일 ─────────────────────────

def test_rewrite_adds_not_null_for_sparse_group_column():
    out = rewrite("SELECT rtn_1y, COUNT(*) FROM prfd01n001 WHERE sale_yn = '판매중' GROUP BY rtn_1y")
    assert "NOT rtn_1y IS NULL" in out or "rtn_1y IS NOT NULL" in out
    assert rewrite("SELECT rtn_1y, COUNT(*) FROM prfd01n001 WHERE rtn_1y IS NOT NULL GROUP BY rtn_1y").count("NULL") == 1
    assert rewrite("SELECT rtn_1y, COUNT(*) FROM prfd01n001 WHERE rtn_1y IN ('a') GROUP BY rtn_1y").count("NULL") == 0
    sql = "SELECT sale_yn, COUNT(*) FROM prfd01n001 GROUP BY sale_yn"  # 결측 없는 컬럼은 그대로
    assert rewrite(sql) == sql


def test_ratio_count_star_denominator_is_error():
    sql = ("SELECT ROUND(100.0 * SUM(CASE WHEN cu_charge_rt::numeric > 1 THEN 1 ELSE 0 END) / COUNT(*), 1) "
           "FROM pref01n001")
    out = rule_violations(sql)
    assert len(out) == 1 and "COUNT(cu_charge_rt)" in out[0]
    fixed = "SELECT 100.0 * COUNT(*) FILTER (WHERE cu_charge_rt::numeric > 1) / COUNT(cu_charge_rt) FROM pref01n001"
    assert rule_violations(fixed) == []
    assert rule_violations("SELECT COUNT(*) FILTER (WHERE pd_grp_no = 'ETF') / COUNT(*) FROM pref01n001") == []  # 결측 없음


def test_other_date_literal_discloses_as_of():
    out = applied_conditions(["SELECT pd_nm FROM prbd01n001 WHERE mat_dt <= DATE '2029-08-21'"])
    assert f"기준일: {sql_rules.settings.as_of_date}" in out


# ───────────────────────── 0이 실제 값인 컬럼·NULLIF('')·숫자 별칭 ─────────────────────────

ZERO_VALUE = frozenset({("pref02n001", "cu_charge_rt")})


@pytest.mark.parametrize("cond", ["cu_charge_rt <> 0", "cu_charge_rt > 0", "NULLIF(cu_charge_rt, 0) IS NOT NULL"])
def test_excluding_real_zero_is_error(cond):
    out = violations(f"SELECT AVG(cu_charge_rt) FROM pref02n001 WHERE {cond}", {}, zero_value=ZERO_VALUE)
    assert len(out) == 1 and "0은 실제 값" in out[0]


def test_real_zero_included_passes():
    assert violations("SELECT AVG(cu_charge_rt) FROM pref02n001 WHERE pd_grp_no = 'ETF'", {}, zero_value=ZERO_VALUE) == []
    # 국내(0=미입력)만 0을 빼는 UNION은 해외 쪽을 건드리지 않았으므로 통과
    sql = ("SELECT AVG(NULLIF(cu_charge_rt, '')::numeric) FROM pref01n001 WHERE NULLIF(cu_charge_rt, '')::numeric > 0 "
           "UNION ALL SELECT AVG(cu_charge_rt) FROM pref02n001")
    assert violations(sql, {}, ZERO, ZERO_VALUE) == []


def test_empty_nullif_counts_as_zero_exclusion():
    sql = "SELECT AVG(NULLIF(cu_charge_rt, '')::numeric) FROM pref01n001 WHERE NULLIF(cu_charge_rt, '')::numeric > 0"
    assert violations(sql, {}, ZERO) == []
    assert "총보수요율: 0인 행 제외" in applied_conditions([sql])


def test_zero_message_suggests_nullif_for_text():
    (msg,) = violations("SELECT AVG(cu_charge_rt::numeric) FROM pref01n001", {}, ZERO)
    assert "NULLIF(cu_charge_rt, '')::numeric > 0" in msg


def test_rewrite_handles_digit_leading_alias():
    out = rewrite("SELECT a AS 3년수익률 FROM t ORDER BY a DESC LIMIT 1")
    assert "NULLS LAST" in out and '"3년수익률"' in out


def test_rewrite_adds_zero_exclusion_per_select():
    """0=미입력 컬럼을 집계에 쓰면 그 SELECT에만 0 제외 조건을 더한다(해외 SELECT는 그대로)."""
    out = rewrite(UNION.format(dom="WHERE pd_grp_no = 'ETF'"), ZERO)
    assert out.count("CAST(NULLIF(cu_charge_rt, '') AS DECIMAL) > 0") == 1  # sqlglot은 ::numeric을 CAST로 적는다
    assert out.index("NULLIF") < out.index("UNION")
    already = UNION.format(dom="WHERE cu_charge_rt::numeric > 0")
    assert rewrite(already, ZERO) == already
    listing = "SELECT pd_nm, cu_charge_rt FROM pref01n001"  # 계산에 안 쓰면 그대로
    assert rewrite(listing, ZERO) == listing
    assert "du_last_aum > 0" in rewrite("SELECT AVG(du_last_aum) FROM pref01n001", ZERO)  # numeric 컬럼
