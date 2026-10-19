"""retrieval.py 순수 함수 테스트 (DB·LLM 없음)."""

from portfolio_agent.retrieval import (
    LAYER_A_RULES,
    LAYER_B_ITEMS,
    keyword_rank,
    label_forms,
    match_score,
    order_forms,
    recall_at,
    rrf,
)


def test_label_forms_drops_parenthetical():
    assert label_forms("보수·총보수·보수율") == ["보수", "총보수", "보수율"]
    assert label_forms("자산군(주식형·채권형 상장상품)") == ["자산군"]


def test_match_score_ignores_spaces_and_prefers_longest():
    assert match_score("판매가능한 채권", ["판매 가능", "판매"]) == 4
    assert match_score("총보수가 낮은", ["보수", "총보수"]) == 3
    assert match_score("수익률", ["보수"]) == 0


def test_match_score_placeholder():
    assert match_score("한국전력이 발행한 채권", ["○○이 발행한"]) == len("이발행한")
    assert match_score("발행한 채권", ["○○이 발행한"]) == 0


def test_order_forms_adds_zero_stripped_and_drops_single_char():
    assert order_forms(["AA0", "AA-", "A0", "C0"]) == ["AA0", "AA", "AA-", "A0", "C0"]
    assert order_forms(None) == []


def test_ascii_forms_need_letter_boundary():
    assert match_score("AA- 이상 채권", ["AA-", "AA"]) == 3
    assert match_score("AAA 채권", ["AA"]) == 0
    assert match_score("ETF중에", ["ETF"]) == 3


def test_keyword_rank_rules_inherit_from_terms_and_attributes():
    terms = {"fee_rate": ["보수", "총보수"], "etf": ["ETF"]}
    rules = {"R-a": ["fee_rate"], "R-b": ["X.col"], "R-c": ["etf_other"]}
    targets = {"X.col": ["총보수요율"]}
    ranked = keyword_rank("총보수요율 낮은 etf", terms, rules, targets)
    assert ranked[0] == "R-b"  # 속성 이름 5자 일치 × 0.9
    assert set(ranked) == {"fee_rate", "etf", "R-a", "R-b"}


def test_rrf_merges_partial_rankings():
    assert rrf(["a", "b", "c"], ["c"])[0] == "c"
    assert rrf(["a"], [])[0] == "a"


def test_recall_at():
    assert recall_at(["a", "b", "c"], {"a", "c"}, 2) == 0.5
    assert recall_at(["a"], set(), 5) is None


def test_fixed_layers_disjoint():
    assert not LAYER_A_RULES & LAYER_B_ITEMS
