"""골든 채점 함수 단위 테스트 (DB·LLM 불필요)."""

import json

import pytest
import yaml

from portfolio_agent.config import PROJECT_ROOT
from portfolio_agent.golden_eval import (
    check_answer,
    check_forbidden,
    check_mentions,
    exec_match,
    grade,
    to_json,
    ungrounded_numbers,
    values_equal,
)

GOLDEN = PROJECT_ROOT / "golden"
QUESTIONS = {q["id"]: q for q in yaml.safe_load((GOLDEN / "questions_v1.yaml").read_text())}
GOLD = json.loads((GOLDEN / "gold_results_v1.json").read_text())


def query(cols, rows, ok=True, output=""):
    return {"sql": "SELECT 1", "ok": ok, "output": output, "rerun_columns": cols, "rerun_rows": rows}


def record(answer, *queries, tables=None):
    return {"answer": answer, "queries": list(queries), "selected_tables": tables or []}


# ───────────── 숫자 정규화 ─────────────
@pytest.mark.parametrize(
    "agent, gold",
    [
        (78, 78),
        ("0.51", 0.5103),  # 에이전트가 ROUND(...,2)
        ("0.5103", 0.5103),
        (0.51034, 0.5103),
        ("55.9", 55.94),  # 소수 1자리로 반올림
        ("2026-09-13", "2026-09-13"),
        (" KODEX 200 ", "KODEX 200"),
        (None, None),
    ],
)
def test_values_equal(agent, gold):
    assert values_equal(agent, gold)


@pytest.mark.parametrize(
    "agent, gold",
    [(77, 78), (78.4, 78), ("0.52", 0.5103), ("1", 0.5103), (None, 0), ("KODEX200", "KODEX 200")],
)
def test_values_not_equal(agent, gold):
    assert not values_equal(agent, gold)


def test_to_json_keeps_decimal_scale():
    import decimal

    assert to_json(decimal.Decimal("0.5100")) == "0.5100"


# ───────────── 실행 정확도 ─────────────
def test_count_ignores_alias():
    q, g = QUESTIONS["C1-01"], GOLD["C1-01"]
    assert exec_match(q, g, [query(["n"], [[78]])])[0] == "last"
    assert exec_match(q, g, [query(["n"], [[43]])])[0] == "none"
    assert exec_match(q, g, [query(["n"], [[78], [43]])])[0] == "none"  # 여러 행이면 오답


def test_uses_last_successful_query():
    q, g = QUESTIONS["C1-01"], GOLD["C1-01"]
    queries = [query(["n"], [[78]]), query(["n"], [[43]]), {"sql": "bad", "ok": False, "output": "Error: x"}]
    assert exec_match(q, g, queries)[0] == "none"
    assert exec_match(q, g, [])[0] == "no_query"


def test_scalar_scale_variant():
    q, g = QUESTIONS["C2-06"], GOLD["C2-06"]
    status, info = exec_match(q, g, [query(["ratio"], [["0.559"]])])
    assert status == "last" and info["scale_variant"]


def test_unordered_list_is_set():
    q, g = QUESTIONS["C1-02"], GOLD["C1-02"]
    rows = [[r[1], r[0]] for r in reversed(g["rows"])]  # 순서·컬럼 위치가 달라도 된다
    rows.append(rows[0])  # LOT별 중복 행은 집합으로 본다
    assert exec_match(q, g, [query(["name", "code"], rows)])[0] == "last"
    assert exec_match(q, g, [query(["name", "code"], rows[1:-1])])[0] == "none"


def test_ordered_list_checks_top_n():
    q, g = QUESTIONS["C3-01"], GOLD["C3-01"]
    rows = g["rows"] + [["다른 ETF", 1.0]]  # N보다 많이 조회해도 앞 N개가 맞으면 정답
    assert exec_match(q, g, [query(["nm", "r"], rows)])[0] == "last"
    swapped = [g["rows"][1], g["rows"][0], *g["rows"][2:]]
    assert exec_match(q, g, [query(["nm", "r"], swapped)])[0] == "none"


def test_ordered_list_accepts_alternatives():
    q = {**QUESTIONS["C3-01"], "accept_alternatives": [{"rank": 5, "key": "동률 ETF"}]}
    g = GOLD["C3-01"]
    rows = [*g["rows"][:4], ["동률 ETF", 311.5]]
    assert exec_match(q, g, [query(["nm", "r"], rows)])[0] == "last"


def test_table_multiset_any_order():
    q, g = QUESTIONS["C2-03"], GOLD["C2-03"]
    rows = [[r[1], r[0]] for r in reversed(g["rows"])]
    assert exec_match(q, g, [query(["cnt", "type"], rows)])[0] == "last"
    assert exec_match(q, g, [query(["cnt", "type"], rows[:2])])[0] == "none"


def test_table_label_columns_match_by_bijection():
    q, g = QUESTIONS["C5-01"], GOLD["C5-01"]
    rows = [["해외 ETF", "0.5103", 5972], ["국내 ETF", "0.4024", 67]]
    assert exec_match(q, g, [query(["구분", "평균", "n"], rows)])[0] == "last"
    swapped = [["해외 ETF", "0.4024"], ["국내 ETF", "0.5103"]]  # 라벨만 바꾼 대응도 일대일이면 허용
    assert exec_match(q, g, [query(["구분", "평균"], swapped)])[0] == "last"
    assert exec_match(q, g, [query(["구분", "평균"], [["a", "0.4024"], ["b", "0.9"]])])[0] == "none"


def test_table_union_across_queries():
    q, g = QUESTIONS["C5-01"], GOLD["C5-01"]
    queries = [
        query(["c"], [["x"], ["y"]]),  # 탐색용 조회는 합집합에서 빠진다
        query(["avg"], [["0.4024"]]),
        query(["avg", "n"], [["0.5103", 5972]]),
    ]
    assert exec_match(q, g, queries)[0] == "union"
    assert exec_match(q, g, queries[:2])[0] == "none"


def test_union_only_for_table():
    q, g = QUESTIONS["C1-02"], GOLD["C1-02"]
    half = len(g["rows"]) // 2
    queries = [query(["pd_no"], [[r[0]] for r in g["rows"][:half]]), query(["pd_no"], [[r[0]] for r in g["rows"][half:]])]
    assert exec_match(q, g, queries)[0] == "none"


# ───────────── 답변 텍스트 ─────────────
def test_check_answer_number_variants():
    q, g = QUESTIONS["C2-03"], GOLD["C2-03"]
    ok, _ = check_answer(q, g, "회사채 12,622 종목, 특수채 6100종목, 국공채 1,775종목입니다.")
    assert ok
    ok, missing = check_answer(q, g, "회사채 12,622종목, 특수채 6,100종목입니다.")
    assert not ok and missing == ["국공채", 1775]


def test_check_answer_skips_labels_and_long_lists():
    q, g = QUESTIONS["C5-01"], GOLD["C5-01"]
    assert check_answer(q, g, "국내 0.40%, 해외 0.51%")[0]  # 'domestic' 라벨은 찾지 않는다
    assert check_answer(QUESTIONS["C1-02"], GOLD["C1-02"], "")[0] is None  # 20개 목록


def test_small_values_not_rounded_to_integer():
    q, g = QUESTIONS["C2-04"], GOLD["C2-04"]
    assert not check_answer(q, g, "평균 보수는 1% 미만입니다")[0]


def test_mentions_and_forbidden():
    q = QUESTIONS["C1-01"]
    assert check_mentions(q, "당사 판매 물량 기준으로 판매 건수는 78건입니다") == []
    assert check_mentions(q, "78개입니다") == q["must_mention"]
    assert check_forbidden(QUESTIONS["C6-02"], "비중은 약 25.3 %입니다")
    assert not check_forbidden(QUESTIONS["C6-02"], "편입 비중 데이터는 없습니다")


def test_ungrounded_numbers():
    out = "pd_abrv_nm | du_er_1y\nHANARO Fn K-반도체 | 386.5\n(1행)"
    qs = [{"ok": True, "output": out}, {"ok": True, "output": "n\n5180073904940\n(1행)"}]
    answer = "2026년 8월 21일 기준 1위는 386.5%이고 순자산 5.18조원, 2위는 999.9%입니다."
    assert ungrounded_numbers("상위 5개 알려줘", answer, qs) == [999.9]


# ───────────── 문항 판정 ─────────────
def test_grade_correct_and_reasons():
    q, g = QUESTIONS["C1-01"], GOLD["C1-01"]
    good = record("판매 가능 물량 기준 판매 건수는 78건입니다.", query(["n"], [[78]]))
    assert grade(q, g, good)["correct"]

    no_mention = grade(q, g, record("78입니다.", query(["n"], [[78]])))
    assert no_mention["fail_reason"] == "missing_mention"

    wrong_text = grade(q, g, record("판매 가능 물량 기준 판매 건수는 80건입니다.", query(["n"], [[78]])))
    assert wrong_text["fail_reason"] == "answer_mismatch"

    wrong = grade(q, g, record("판매 건 43건", query(["n"], [[43]]), tables=["prbd01n001"]))
    assert wrong["fail_reason"] == "wrong_result"
    assert grade(q, g, record("판매 건 43건", query(["n"], [[43]])))["fail_reason"] == "table_miss"
    assert grade(q, g, {**good, "error": "boom"})["fail_reason"] == "error"


def test_grade_refusal():
    q = QUESTIONS["C6-01"]
    assert grade(q, None, record("자회사 관계 정보는 데이터에 없습니다."))["correct"]
    guess = grade(q, None, record("자회사 정보는 데이터에 없지만 에코프로비엠을 담은 ETF는 3개입니다."))
    assert guess["fail_reason"] == "refusal_fail"


# ───────────── 컬럼 진단 ─────────────
def test_sql_columns_drops_aliases_and_ctes():
    from portfolio_agent.golden_eval import sql_columns

    assert sql_columns(QUESTIONS["C1-01"]["gold_sql"]) == {"curr_cd", "trade_price", "crd_grd"}
    assert "n" not in sql_columns(QUESTIONS["C4-06"]["gold_sql"])  # CTE 컬럼 별칭
    assert sql_columns("SELECT (") is None


def test_column_diff_in_grade():
    q, g = QUESTIONS["C1-01"], GOLD["C1-01"]
    agent_sql = "SELECT COUNT(*) FROM prbd01n001 WHERE pd_exg_mkt IN ('장내','장외') AND curr_cd='KRW' AND crd_grd >= 'AA-'"
    r = grade(q, g, record("13,779건", {**query(["count"], [[13779]]), "sql": agent_sql}))
    assert r["columns_missing"] == ["trade_price"] and r["columns_extra"] == ["pd_exg_mkt"]
    refusal = grade(QUESTIONS["C6-01"], None, record("데이터에 없습니다"))
    assert refusal["columns_missing"] is None
