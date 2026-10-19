"""온톨로지 C층(질문으로 고르는 용어·규칙) 검색: 키워드 순위, RRF 결합, 재현율 채점 (순수 함수)."""

import re

# B층: 질문과 상관없이 항상 프롬프트에 넣는 공통 규칙 (2026-10-19 결정)
LAYER_B_RULES = frozenset({
    "R-scope-default", "R-null-exclude", "R-units", "R-as-of-date", "R-outlier-raw", "R-oos-refusal",
})
# A층: 적용 대상이 속성뿐이라 선택된 테이블의 스키마 카드에 붙이는 규칙 (2026-10-19 결정)
LAYER_A_RULES = frozenset({"R-zero-missing", "R-zero-is-value", "R-text-numeric"})
FIXED_ITEMS = LAYER_A_RULES | LAYER_B_RULES

RRF_K = 60  # 표준값. 골든으로 조정하지 않았다
RULE_FROM_TERM = 0.9  # 규칙이 적용 대상 용어의 키워드 점수를 물려받는 비율

_SPACES = re.compile(r"\s+")
_PAREN = re.compile(r"\(.*?\)")
PLACEHOLDER = "○○"


def _norm(s: str) -> str:
    return _SPACES.sub("", s).lower()


def label_forms(label: str) -> list[str]:
    """'보수·총보수·보수율' → 세 표현. 괄호 속 설명은 버린다."""
    return [p for p in (_PAREN.sub("", label).split("·")) if p.strip()]


def order_forms(values: list[str] | None) -> list[str]:
    """등급 순서 값을 키워드 표현으로 쓴다. 'AA0'처럼 끝의 0은 빼고 부르는 경우가 많아 'AA'도 넣는다.
    한 글자 값('A', 'C')은 다른 말에 섞여 걸리기 쉬워 뺀다."""
    forms = []
    for v in values or []:
        forms.append(v)
        if v.endswith("0"):
            forms.append(v[:-1])
    return [f for f in dict.fromkeys(forms) if len(_norm(f)) >= 2]


def form_pattern(form: str) -> re.Pattern:
    """표현을 공백 무시 패턴으로 만든다. '○○'는 아무 말 1~20자.
    영문으로 시작·끝나는 표현은 앞뒤에 영문자가 붙으면 맞지 않는다 ('AA'가 'AAA' 속에서 걸리지 않게)."""
    norm = _norm(form)
    parts = [re.escape(_norm(p)) for p in form.split(PLACEHOLDER)]
    body = ".{1,20}?".join(parts)
    if norm[:1].isascii() and norm[:1].isalpha():
        body = "(?<![a-z])" + body
    if norm[-1:].isascii() and norm[-1:].isalpha():
        body += "(?![a-z])"
    return re.compile(body)


def match_score(question: str, forms: list[str]) -> int:
    """질문에 들어 있는 가장 긴 표현의 길이(자리표시자 제외). 없으면 0."""
    q = _norm(question)
    best = 0
    for f in forms:
        if _norm(f.replace(PLACEHOLDER, "")) and form_pattern(f).search(q):
            best = max(best, len(_norm(f.replace(PLACEHOLDER, ""))))
    return best


def keyword_rank(question: str, term_forms: dict[str, list[str]],
                 rule_targets: dict[str, list[str]], target_forms: dict[str, list[str]]) -> list[str]:
    """키워드로 걸린 용어·규칙만 점수순으로 돌려준다.

    용어: 이름·같은 말 중 질문에 든 가장 긴 표현의 길이.
    규칙: 적용 대상(용어는 그 용어 점수, 속성·개념은 이름 일치) 중 최고점 × RULE_FROM_TERM.
    """
    scores: dict[str, float] = {}
    for tid, forms in term_forms.items():
        if s := match_score(question, forms):
            scores[tid] = s
    for rid, targets in rule_targets.items():
        best = 0.0
        for t in targets:
            s = scores.get(t) if t in term_forms else match_score(question, target_forms.get(t, []))
            best = max(best, s or 0)
        if best:
            scores[rid] = best * RULE_FROM_TERM
    return sorted(scores, key=lambda i: (-scores[i], i))


def rrf(*rankings: list[str], k: int = RRF_K) -> list[str]:
    """Reciprocal Rank Fusion: 각 순위의 1/(k+rank)를 더한다. 한쪽에만 있어도 된다."""
    score: dict[str, float] = {}
    for ranking in rankings:
        for r, item in enumerate(ranking, 1):
            score[item] = score.get(item, 0.0) + 1 / (k + r)
    return sorted(score, key=lambda i: (-score[i], i))


def recall_at(ranking: list[str], gold: set[str], k: int) -> float | None:
    """상위 k개가 정답 중 몇 %를 담는가. 정답이 없으면 None."""
    if not gold:
        return None
    return len(gold & set(ranking[:k])) / len(gold)
