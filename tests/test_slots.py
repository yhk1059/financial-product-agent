"""--slots 칸 처리(slots.py) 테스트 (DB·LLM 불필요: 가짜 Knowledge·카탈로그·lookup)."""

import inspect
import re

import pytest

from portfolio_agent import prompts, slots
from portfolio_agent.context import Knowledge
from portfolio_agent.slots import (grade_index, grade_range, name_notes, normalize_name, render_resolved, resolve,
                                   slot_violations, validate_slots)

CREDIT = ["AAA", "AA+", "AA0", "AA-", "A+", "A0", "A-"]
RISK = ["매우 높은 위험", "높은 위험", "다소 높은 위험", "보통 위험", "낮은 위험", "매우 낮은 위험"]


def _kn() -> Knowledge:
    items = {t: {"label": label} for t, label in [
        ("credit_rating", "신용등급"), ("risk_grade", "투자위험등급"), ("product_name", "상품명"),
        ("issuer", "발행기관"), ("holding", "구성종목·편입"), ("mmf", "MMF"), ("bond_class", "채권 분류")]}
    concepts = {
        "Dom": {"table_name": "pref01n001", "identity": ["pd_itm_no"]},
        "DomEtf": {"table_name": "pref01n001", "identity": ["pd_itm_no"]},
        "Fund": {"table_name": "prfd01n001", "identity": ["itm_no"]},
        "Bond": {"table_name": "prbd01n001", "identity": ["pd_no"]},
        "Lot": {"table_name": "prbd01n001", "identity": ["pd_no", "pd_exg_mkt"]},
        "Holding": {"table_name": "domestic_etf_holdings", "identity": ["holding_id"]},
    }
    attrs = {f"{c}.{col}": {"concept_id": c, "column_name": col} for c, col in [
        ("Dom", "pd_abrv_nm"), ("Dom", "pd_risk_nm"), ("Fund", "itm_nm"), ("Fund", "zrin_fd_ivst_risk_grd_nm"),
        ("Fund", "zrin_fd_ivst_risk_gcd"), ("Fund", "zrin_btyp_nm"), ("Bond", "crd_grd"), ("Bond", "pd_pbcm"),
        ("Bond", "pd_nm"), ("Bond", "std_pd_mcls_nm"), ("Holding", "component_name")]}
    relations = {"etf_holds": {"from_id": "DomEtf", "to_id": "Holding", "via": ["pd_itm_no", "item_id"]},
                 "lot": {"from_id": "Lot", "to_id": "Bond", "via": ["pd_no"]}}

    def term(canonical, forms=(), order=None, bundle=False, order_up=None):
        return {"canonical": canonical, "forms": list(forms), "order_values": order, "bundle": bundle,
                "order_up": order_up}

    terms = {
        "credit_rating": term([("Bond.crd_grd", None)], ["신용등급", "등급", "크레딧"], CREDIT, order_up="first"),
        "risk_grade": term([("Dom.pd_risk_nm", None), ("Fund.zrin_fd_ivst_risk_grd_nm", None),
                            ("Fund.zrin_fd_ivst_risk_gcd", None)], ["위험등급", "위험도"], RISK, order_up="first"),
        "product_name": term([("Dom.pd_abrv_nm", None), ("Fund.itm_nm", None), ("Bond.pd_nm", None)], ["상품명"]),
        "issuer": term([("Issuer", None), ("Bond.pd_pbcm", None)], ["발행기관"]),
        "holding": term([("Holding", None), ("Holding.component_name", None)], ["구성종목"]),
        "mmf": term([("Fund.zrin_btyp_nm", ["MMF", "외화 MMF"])], ["MMF", "머니마켓펀드"], bundle=True),
        "bond_class": term([("Bond.std_pd_mcls_nm", ["회사채", "국공채"])], ["채권 분류", "국공채"]),
    }
    return Knowledge(items, concepts, attrs, relations, terms, {})


KN = _kn()
CATALOG = {"tables": {
    "prbd01n001": {"columns": [
        {"name": "crd_grd", "null_pct": 18.0, "values": {v: 1 for v in CREDIT}},
        {"name": "std_pd_mcls_nm", "null_pct": 0.0, "values": {"회사채": 1, "국공채": 1}},
        {"name": "std_pd_scls_nm", "null_pct": 0.0, "values": {"가나채": 1, "다라채": 1, "일반사채": 1}},
        {"name": "pd_exg_mkt", "null_pct": 0.0, "values": {"01": 1, "02": 1}},
    ]},
    "prfd01n001": {"columns": [
        {"name": "zrin_fd_ivst_risk_grd_nm", "null_pct": 60.0,
         "values": {"높은 위험": 1, "높은위험": 1, "보통 위험": 1, "보통위험": 1, "낮은 위험": 1, "매우 높은 위험": 1}},
        {"name": "zrin_fd_ivst_risk_gcd", "null_pct": 60.0, "values": {str(i): 1 for i in range(1, 7)}},
        {"name": "zrin_btyp_nm", "null_pct": 50.0, "values": {"MMF": 1, "외화 MMF": 1, "주식형": 1}},
        {"name": "or_attr_desc", "null_pct": 0.0, "values": {"MMF": 1, "채권형": 1}},
    ]},
    "pref01n001": {"columns": [
        {"name": "pd_risk_nm", "null_pct": 0.0, "values": {"매우높은위험(1등급)": 1, "보통위험(4등급)": 1,
                                                          "낮은위험(5등급)": 1, "해당없음": 1}},
    ]},
}}


class FakeLookup:
    """저장된 이름: STORED[column]. 식별값: IDS[value]."""

    STORED = {"pd_pbcm": ["(주)가나다은행", "가나다캐피탈", "라마바증권"],
              "pd_abrv_nm": ["가나 200", "가나 200TR", "다라 50", "다라 50"],
              "component_name": ["가나전자", "가나전자우"], "itm_nm": [], "pd_nm": []}
    IDS = {"가나 200": ["KR0000000001"], "다라 50": ["KR0000000002", "KR0000000003"]}

    def values(self, table, column, text, mode):
        stored = list(dict.fromkeys(self.STORED[column]))
        if mode == "exact":
            return [v for v in stored if v == text]
        if mode == "normalized":
            return [v for v in stored if normalize_name(v) == normalize_name(text)]
        return sorted((v for v in stored if text.replace(" ", "").lower() in v.replace(" ", "").lower()), key=len)

    def ids(self, table, column, value, id_column):
        return self.IDS.get(value, [])


def run(slot_dict, tables, dialogue=""):
    full = {"names": [], "grades": [], "values": [], **slot_dict}
    return resolve(full, KN, CATALOG, FakeLookup(), tables, dialogue)


def grade(value, direction, term="신용등급", tables=("prbd01n001",)):
    return run({"grades": [{"term": term, "value": value, "direction": direction}]}, list(tables))


# ───────────────────────── 등급 ─────────────────────────

@pytest.mark.parametrize("direction,want", [
    ("이상", ["AAA", "AA+", "AA0", "AA-"]), ("이하", ["AA-", "A+", "A0", "A-"]), ("초과", ["AAA", "AA+", "AA0"]),
    ("미만", ["A+", "A0", "A-"]), ("같음", ["AA-"])])
def test_grade_directions(direction, want):
    (r,) = grade("AA-", direction)
    assert r["status"] == "confirmed" and r["values"] == want
    assert r["fragment"] == slots._in("prbd01n001", "crd_grd", want)


def test_grade_fragment_text():
    (r,) = grade("AA-", "이상")
    assert render_resolved([r]) == ("- 신용등급 'AA-' 이상 → prbd01n001.crd_grd IN ('AAA', 'AA+', 'AA0', 'AA-') "
                                    "(등급이 비어 있는 행은 빠짐)")


@pytest.mark.parametrize("value,i", [("AA", 2), ("A", 5), ("aa-", 3), ("3등급", 2), ("A0", 5)])
def test_grade_index_forms(value, i):
    assert grade_index(CREDIT, value) == i


def test_grade_index_risk_forms():
    assert grade_index(RISK, "보통위험") == 3
    assert grade_index(RISK, "보통위험(4등급)") == 3
    assert grade_index(RISK, "4등급") == 3
    assert grade_index(RISK, "7등급") is None


def test_order_up_last_reverses():
    kn = _kn()
    kn.terms["credit_rating"]["order_values"] = CREDIT[::-1]
    kn.terms["credit_rating"]["order_up"] = "last"
    assert slots.grade_terms(kn)["credit_rating"] == CREDIT


def test_fund_risk_names_and_codes():
    """펀드 등급명은 띄어쓰기 변형을 모두 모으고, 코드 컬럼은 n번째 등급 번호로 낸다."""
    out = grade("보통 위험", "이하", term="위험등급", tables=("prfd01n001",))
    by_col = {r["column"]: r["values"] for r in out}
    assert by_col["zrin_fd_ivst_risk_grd_nm"] == ["보통 위험", "보통위험", "낮은 위험"]
    assert by_col["zrin_fd_ivst_risk_gcd"] == ["4", "5", "6"]


def test_domestic_risk_grade_with_number_suffix():
    (r,) = grade("높은 위험", "이상", term="위험등급", tables=("pref01n001",))
    assert r["values"] == ["매우높은위험(1등급)"]


def test_ambiguous_term_uses_value():
    """'등급'은 신용등급의 표현이지만 값이 위험등급 순서에만 있으면 위험등급으로 정한다."""
    out = grade("보통 위험", "같음", term="등급", tables=("prfd01n001",))
    assert {r["column"] for r in out} == {"zrin_fd_ivst_risk_grd_nm", "zrin_fd_ivst_risk_gcd"}
    (r,) = grade("AA0", "같음", term="")
    assert r["values"] == ["AA0"]


def test_grade_without_table_column_skipped():
    (r,) = grade("AA-", "이상", tables=("pref01n001",))
    assert r["status"] == "skipped" and render_resolved([r]) == ""


def test_grade_range_helper():
    assert grade_range(["a", "b", "c"], 0, "미만") == ["b", "c"]
    assert grade_range(["a", "b", "c"], 2, "초과") == ["a", "b"]


# ───────────────────────── 이름 ─────────────────────────

def name(text, kind, tables):
    return run({"names": [{"text": text, "kind": kind}]}, list(tables))


def test_exact_product_with_identity_and_relation():
    (r,) = name("가나 200", "상품", ["pref01n001", "domestic_etf_holdings"])
    assert r["status"] == "confirmed"
    assert r["fragment"] == ("pref01n001.pd_abrv_nm = '가나 200' (pd_itm_no = 'KR0000000001'), "
                             "구성종목은 domestic_etf_holdings.item_id = 'KR0000000001'")


def test_exact_product_many_ids():
    (r,) = name("다라 50", "상품", ["pref01n001"])
    assert r["fragment"] == "pref01n001.pd_abrv_nm = '다라 50' (pd_itm_no IN ('KR0000000002', 'KR0000000003'))"


def test_too_many_ids_are_left_out():
    FakeLookup.IDS["가나 200TR"] = [f"KR{i:010d}" for i in range(slots.MAX_IDS + 1)]
    try:
        (r,) = name("가나 200TR", "상품", ["pref01n001"])
    finally:
        del FakeLookup.IDS["가나 200TR"]
    assert r["fragment"] == "pref01n001.pd_abrv_nm = '가나 200TR'"


def test_issuer_has_no_row_ids():
    FakeLookup.IDS["(주)가나다은행"] = ["B1"]
    try:
        (r,) = name("(주)가나다은행", "발행기관", ["prbd01n001"])
    finally:
        del FakeLookup.IDS["(주)가나다은행"]
    assert r["fragment"] == "prbd01n001.pd_pbcm = '(주)가나다은행'"


def test_normalized_match_confirms_with_note():
    (r,) = name("가나다 은행", "발행기관", ["prbd01n001"])
    assert r["status"] == "confirmed" and r["values"] == ["(주)가나다은행"]
    assert r["note"] == "이름 해석: '가나다 은행' → '(주)가나다은행'"
    assert "답변에 밝힐 것" in render_resolved([r])


def test_partial_single_candidate_is_not_confirmed():
    (r,) = name("라마바", "발행기관", ["prbd01n001"])
    assert r["status"] == "candidates" and r["values"] == ["라마바증권"]
    assert "저장된 이름 후보: '라마바증권'" in render_resolved([r])


def test_not_found():
    (r,) = name("없는이름", "발행기관", ["prbd01n001"])
    assert r["status"] == "not_found"
    assert render_resolved([r]) == "- 발행기관 '없는이름' → prbd01n001.pd_pbcm의 저장된 이름에서 찾지 못함(표기가 다를 수 있음)"


def test_name_column_not_selected_skipped():
    (r,) = name("가나다", "발행기관", ["pref01n001"])
    assert r["status"] == "skipped" and render_resolved([r]) == ""


def test_holding_partial_is_only_for_spelling():
    (r,) = name("가나", "구성종목", ["domestic_etf_holdings"])
    assert r["status"] == "candidates"
    assert "편입 판정에 쓰지 않는다" in render_resolved([r])


def test_holding_exact_confirms_without_ids():
    (r,) = name("가나전자", "구성종목", ["domestic_etf_holdings"])
    assert r["fragment"] == "domestic_etf_holdings.component_name = '가나전자'"


def test_name_notes():
    rs = name("가나다 은행", "발행기관", ["prbd01n001"]) + name("라마바", "발행기관", ["prbd01n001"])
    sqls = ["SELECT 1 FROM prbd01n001 WHERE pd_pbcm IN ('(주)가나다은행', '라마바증권')"]
    assert name_notes(rs, sqls) == ["이름 해석: '가나다 은행' → '(주)가나다은행'", "이름 해석: '라마바' → '라마바증권'"]
    assert name_notes(rs, ["SELECT 1"]) == []


# ───────────────────────── 범주 값 ─────────────────────────

def value(text, tables):
    return run({"values": [text]}, list(tables))


def test_bundle_term_gives_all_values():
    """MMF는 카탈로그 값이기도 하지만 묶음 용어가 먼저다."""
    (r,) = value("mmf", ["prfd01n001"])
    assert r["status"] == "confirmed" and r["fragment"] == "prfd01n001.zrin_btyp_nm IN ('MMF', '외화 MMF')"


def test_non_bundle_term_goes_to_catalog():
    """묶음이 아닌 분류 용어의 표현은 canonical 전체가 아니라 카탈로그 값 하나다."""
    (r,) = value("국공채", ["prbd01n001"])
    assert r["fragment"] == "prbd01n001.std_pd_mcls_nm = '국공채'"


def test_catalog_value_contained_in_text():
    (r,) = value("가나채권", ["prbd01n001"])
    assert r["fragment"] == "prbd01n001.std_pd_scls_nm = '가나채'"


def test_catalog_digits_not_matched_by_containment():
    (r,) = value("2001년물", ["prbd01n001"])
    assert r["status"] == "skipped"


def test_same_value_in_many_columns_is_candidates():
    kn = _kn()
    kn.terms["mmf"]["bundle"] = False
    out = resolve({"names": [], "grades": [], "values": ["MMF"]}, kn, CATALOG, FakeLookup(), ["prfd01n001"])
    (r,) = out
    assert r["status"] == "candidates" and "zrin_btyp_nm = 'MMF'" in r["fragment"] and "or_attr_desc" in r["fragment"]


# ───────────────────────── 검증 ─────────────────────────

def test_validate_drops_invented_text():
    s = {"names": [{"text": "가나다", "kind": "발행기관"}, {"text": "지어낸", "kind": "상품"}],
         "grades": [{"term": "신용등급", "value": "AA-", "direction": "이상"},
                    {"term": "신용등급", "value": "ZZ", "direction": "이상"}],
         "values": ["MMF", "없는값"]}
    kept, dropped = validate_slots(s, "사용자: 가나다가 발행한 신용등급 AA- 이상, ZZ, mmf", KN)
    assert [n["text"] for n in kept["names"]] == ["가나다"]
    assert [g["value"] for g in kept["grades"]] == ["AA-"]
    assert kept["values"] == ["MMF"]
    assert [d["status"] for d in dropped] == ["dropped"] * 3
    assert render_resolved(dropped) == ""


def test_resolve_with_dialogue_records_dropped():
    out = run({"names": [{"text": "지어낸", "kind": "상품"}]}, ["pref01n001"], dialogue="사용자: 질문")
    assert [r["status"] for r in out] == ["dropped"]


def test_render_empty():
    assert render_resolved([]) == ""


# ───────────────────────── enforce: 확인된 값과 다른 SQL ─────────────────────────

AA = grade("AA-", "이상")
MMF = value("MMF", ["prfd01n001"])


@pytest.mark.parametrize("cond", ["crd_grd IN ('AA+', 'AA0', 'AA-', 'A+')", "crd_grd = 'AA-'", "crd_grd >= 'AA-'"])
def test_grade_set_mismatch_is_error(cond):
    (msg,) = slot_violations(f"SELECT COUNT(*) FROM prbd01n001 WHERE {cond}", AA)
    assert "crd_grd IN ('AAA', 'AA+', 'AA0', 'AA-')" in msg


def test_grade_same_set_passes():
    assert slot_violations("SELECT 1 FROM prbd01n001 WHERE crd_grd IN ('AA-', 'AAA', 'AA0', 'AA+')", AA) == []


def test_bundle_proper_subset_is_error():
    (msg,) = slot_violations("SELECT 1 FROM prfd01n001 WHERE zrin_btyp_nm = 'MMF'", MMF)
    assert "IN ('MMF', '외화 MMF')" in msg
    assert slot_violations("SELECT 1 FROM prfd01n001 WHERE zrin_btyp_nm IN ('MMF', '외화 MMF')", MMF) == []


def test_other_table_not_checked():
    assert slot_violations("SELECT 1 FROM pref01n001 WHERE crd_grd = 'A'", AA) == []


def test_empty_resolved_no_checks():
    assert slot_violations("SELECT 1 FROM prbd01n001 WHERE crd_grd = 'A'", []) == []


# ───────────────────────── 누출 ─────────────────────────

def test_no_proper_nouns_in_slot_code():
    """slots.py와 칸 추출 프롬프트에 골든·홀드아웃 고유명사와 문항 id가 없어야 한다."""
    from test_ontology import GOLDEN_PROPER_NOUNS, HOLDOUT_PROPER_NOUNS, QUESTION_ID, proper_noun_leaks

    texts = [inspect.getsource(slots), prompts.ANALYZE_QUESTION, prompts.RESOLVED_SECTION]
    for text in texts:
        assert not proper_noun_leaks({"text": text}, GOLDEN_PROPER_NOUNS + HOLDOUT_PROPER_NOUNS)
        assert not QUESTION_ID.search(text), re.findall(QUESTION_ID, text)


# ───────────────────────── 질문에서 코드가 찾는 범주 값 ─────────────────────────

from portfolio_agent.slots import scan_values


def _kn_values():
    """용어 없음(순서 용어도 없음)."""
    class K:
        terms = {}
    return K()


CAT = {"tables": {"t": {"columns": [
    {"name": "big", "values": {"해외주식형": 1, "주식형": 2, "채권형": 3}},
    {"name": "sale", "values": {"판매중": 1, "판매완료": 2}},
    {"name": "two", "values": {"국내": 1}},
    {"name": "eng", "values": {"ETF": 1}},
]}}}


def test_scan_values_finds_long_korean_values_only():
    out = scan_values("판매중인 국내 ETF 중 채권형", _kn_values(), CAT, ["t"])
    assert sorted((r["column"], r["values"][0], r["status"]) for r in out) == [("big", "채권형", "confirmed"),
                                                                                ("sale", "판매중", "confirmed")]


def test_scan_values_skips_value_covered_by_longer_value():
    out = scan_values("해외주식형 펀드", _kn_values(), CAT, ["t"])
    assert [r["values"] for r in out] == [["해외주식형"]]
    out = scan_values("국내주식형이랑 해외주식형", _kn_values(), CAT, ["t"])  # 앞의 주식형은 덮이지 않는다
    assert sorted(v for r in out for v in r["values"]) == ["주식형", "해외주식형"]


def test_scan_values_skips_ordered_columns():
    class K:
        terms = {"g": {"order_values": ["판매중", "판매완료"], "canonical": [("T.sale", None)]}}
    assert scan_values("판매중인 것", K(), CAT, ["t"]) == []


def test_value_slot_requires_its_column():
    resolved = [{"slot": "value", "status": "confirmed", "text": "국고채", "table": "prbd01n001",
                 "column": "std_pd_scls_nm", "values": ["국고채"], "fragment": "prbd01n001.std_pd_scls_nm = '국고채'"}]
    out = slot_violations("SELECT AVG(x) FROM prbd01n001 WHERE std_pd_mcls_nm = '국공채'", resolved)
    assert len(out) == 1 and "std_pd_scls_nm 컬럼을 쓰지 않았습니다" in out[0]
    assert slot_violations("SELECT std_pd_scls_nm, AVG(x) FROM prbd01n001 GROUP BY 1", resolved) == []
    out = slot_violations("SELECT AVG(x) FROM prbd01n001 WHERE std_pd_scls_nm = '국민주택'", resolved)
    assert len(out) == 1 and "다른 값" in out[0]


def test_candidates_prefer_column_of_confirmed_value():
    from portfolio_agent.slots import prefer_confirmed_columns
    cand = {"slot": "value", "status": "candidates", "text": "주식형", "table": "t", "column": "", "values": ["주식형", "주식형"],
            "fragment": "a = '주식형', b = '주식형'", "note": "", "kind": "", "options": [["a", "주식형"], ["b", "주식형"]]}
    conf = {"slot": "value", "status": "confirmed", "text": "해외주식형", "table": "t", "column": "b", "values": ["해외주식형"],
            "fragment": "t.b = '해외주식형'", "note": "", "kind": ""}
    out = prefer_confirmed_columns([cand, conf])
    assert out[0]["status"] == "confirmed" and out[0]["column"] == "b" and out[0]["fragment"] == "t.b = '주식형'"
    assert prefer_confirmed_columns([cand])[0]["status"] == "candidates"  # 확정 값이 없으면 그대로


def test_value_in_one_union_branch_is_enough():
    resolved = [{"slot": "value", "status": "confirmed", "text": "해외주식형", "table": "prfd01n001", "column": "zrin_btyp_nm",
                 "values": ["해외주식형"], "fragment": "prfd01n001.zrin_btyp_nm = '해외주식형'"}]
    sql = ("SELECT '국내' AS g, AVG(r) FROM prfd01n001 WHERE zrin_btyp_nm = '주식형' "
           "UNION ALL SELECT '해외', AVG(r) FROM prfd01n001 WHERE zrin_btyp_nm = '해외주식형'")
    assert slot_violations(sql, resolved) == []
    out = slot_violations("SELECT AVG(r) FROM prfd01n001 WHERE zrin_btyp_nm = '주식형'", resolved)
    assert len(out) == 1 and "다른 값" in out[0]
