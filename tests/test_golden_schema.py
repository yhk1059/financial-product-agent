"""골든 데이터셋 문항 형식·커버리지 검사 (DB 불필요)."""

import re
from collections import Counter

import pytest
import yaml

from portfolio_agent.config import ALLOWED_TABLES, PROJECT_ROOT

GOLDEN = PROJECT_ROOT / "golden"
QUESTIONS = yaml.safe_load((GOLDEN / "questions_v0.yaml").read_text())
POLICIES = set(re.findall(r"\*\*(P-[A-Z]+)\*\*", (GOLDEN / "design.md").read_text()))

REQUIRED = {"id", "category", "difficulty", "tables", "question", "interpretation", "policy_refs", "answer_type"}
TARGET_PER_CATEGORY = {"C1": 7, "C2": 7, "C3": 5, "C4": 6, "C5": 2, "C6": 3}
ANSWER_TYPES = {"count", "scalar", "list", "table", "refusal"}


@pytest.mark.parametrize("q", QUESTIONS, ids=lambda q: q.get("id", "?"))
def test_question_fields(q):
    assert REQUIRED <= q.keys(), REQUIRED - q.keys()
    assert q["id"].startswith(q["category"] + "-")
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


def test_ids_unique():
    ids = [q["id"] for q in QUESTIONS]
    assert len(ids) == len(set(ids))


def test_category_distribution():
    assert Counter(q["category"] for q in QUESTIONS) == TARGET_PER_CATEGORY


def test_table_coverage():
    counts = Counter(t for q in QUESTIONS for t in q["tables"])
    assert all(counts[t] >= 4 for t in ALLOWED_TABLES), counts
