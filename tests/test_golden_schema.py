"""골든·홀드아웃 데이터셋 문항 형식·커버리지 검사 (DB 불필요)."""

import re
from collections import Counter

import pytest
import yaml

from portfolio_agent.config import ALLOWED_TABLES, PROJECT_ROOT

GOLDEN = PROJECT_ROOT / "golden"
SETS = {
    "golden": yaml.safe_load((GOLDEN / "questions_v2.yaml").read_text()),
    "holdout": yaml.safe_load((GOLDEN / "holdout_v2.yaml").read_text()),
}
QUESTIONS = [q for qs in SETS.values() for q in qs]  # 문항 id는 묶음끼리 겹치지 않는다(C*-nn, H-nn)
POLICIES = set(re.findall(r"\*\*(P-[A-Z]+)\*\*", (GOLDEN / "design.md").read_text()))

REQUIRED = {"id", "category", "difficulty", "tables", "question", "interpretation", "policy_refs", "answer_type"}
TARGET_PER_CATEGORY = {"C1": 7, "C2": 7, "C3": 5, "C4": 6, "C5": 2, "C6": 3}
ANSWER_TYPES = {"count", "scalar", "list", "table", "refusal"}


@pytest.mark.parametrize("q", QUESTIONS, ids=lambda q: q.get("id", "?"))
def test_question_fields(q):
    assert REQUIRED <= q.keys(), REQUIRED - q.keys()
    assert q["id"].startswith((q["category"] + "-", "H-"))  # 홀드아웃은 H-nn
    assert q["difficulty"] in {"easy", "medium", "hard"}
    assert q["answer_type"] in ANSWER_TYPES
    assert set(q["tables"]) <= set(ALLOWED_TABLES)
    assert set(q["policy_refs"]) <= POLICIES, set(q["policy_refs"]) - POLICIES
    if q["answer_type"] == "refusal":
        assert q["category"] == "C6" and "gold_sql" not in q
    else:
        assert q.get("gold_sql") and q.get("key_columns")
    for opt in q.get("decision_needed", {}).get("options", []):
        assert opt["label"] and opt["sql"]


@pytest.mark.parametrize("q", QUESTIONS, ids=lambda q: q.get("id", "?"))
def test_scoring_fields(q):
    """채점 필드 (design.md 4절·5.6)."""
    assert len(q.get("mention_keywords", [])) == len(q.get("must_mention", []))
    assert all(kw and "||" not in kw for kw in q.get("mention_keywords", []))
    if q["category"] == "C6":
        assert q.get("forbidden_patterns")
    for p in q.get("forbidden_patterns", []):
        re.compile(p)
    assert set(q.get("label_columns", [])) <= set(q.get("key_columns", []))
    for alt in q.get("accept_alternatives", []):
        assert q.get("order_matters") and alt["rank"] >= 1 and "key" in alt
    assert set(q.get("key_alternatives", {})) <= set(q.get("key_columns", []))


@pytest.mark.parametrize("q", [q for q in QUESTIONS if q.get("mention_keywords")], ids=lambda q: q["id"])
def test_mention_keywords_not_in_question(q):
    """S1: 질문에 이미 있는 표현은 답변이 질문을 따라 말하기만 해도 통과하므로 키워드로 쓰지 않는다."""
    norm = lambda s: re.sub(r"\s+", "", s).lower()
    echoed = [alt for kw in q["mention_keywords"] for alt in kw.split("|") if norm(alt) in norm(q["question"])]
    assert not echoed, echoed


def test_ids_unique():
    ids = [q["id"] for q in QUESTIONS]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("qset", list(SETS))
def test_category_distribution(qset):
    assert Counter(q["category"] for q in SETS[qset]) == TARGET_PER_CATEGORY


@pytest.mark.parametrize("qset", list(SETS))
def test_table_coverage(qset):
    counts = Counter(t for q in SETS[qset] for t in q["tables"])
    assert all(counts[t] >= 4 for t in ALLOWED_TABLES), counts


def test_holdout_questions_differ_from_golden():
    """홀드아웃은 골든 질문의 표현만 바꾼 문항이 아니어야 한다: 질문 문장과 정답 SQL이 골든과 같으면 안 된다."""
    golden_q = {q["question"] for q in SETS["golden"]}
    golden_sql = {re.sub(r"\s+", " ", q["gold_sql"]).strip() for q in SETS["golden"] if q.get("gold_sql")}
    for q in SETS["holdout"]:
        assert q["question"] not in golden_q, q["id"]
        if q.get("gold_sql"):
            assert re.sub(r"\s+", " ", q["gold_sql"]).strip() not in golden_sql, q["id"]
