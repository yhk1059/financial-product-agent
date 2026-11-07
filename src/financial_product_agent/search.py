"""온톨로지 C층 검색: 질문 하나로 용어·규칙 id를 고른다 (읽기 계정, knowledge 스키마).

순위 계산은 retrieval.py의 순수 함수를 쓰고, 여기서는 DB 읽기·질문 임베딩·캐시만 맡는다.
scripts/eval_retrieval.py(채점)와 에이전트(graph.retrieve_knowledge)가 같은 순위를 쓰게 한 곳에 둔다.
"""

import hashlib
import json
import logging
from functools import lru_cache

from . import db
from .config import DATA_DIR
from .embedding import EMBED_MODEL, VARIANT, embed, to_pgvector
from .retrieval import DEFAULT_K, FIXED_ITEMS, keyword_rank, label_forms, order_forms, rrf

log = logging.getLogger(__name__)

CACHE_PATH = DATA_DIR / "retrieval_cache" / f"questions_{EMBED_MODEL}.json"


@lru_cache
def load_index():
    """키워드 순위에 쓸 용어 표현, 규칙 적용 대상, 대상 이름, 임베딩된 항목을 읽는다 (읽기 계정)."""
    _, rows = db.fetch(
        "SELECT i.id, i.label, coalesce(array_agg(f.form) FILTER (WHERE f.form IS NOT NULL), '{}'),"
        " t.order_values"
        " FROM knowledge.term t JOIN knowledge.item i ON i.id = t.id"
        " LEFT JOIN knowledge.term_surface_form f ON f.term_id = t.id GROUP BY i.id, i.label, t.order_values")
    term_forms = {tid: label_forms(label) + list(forms) + order_forms(order)
                  for tid, label, forms, order in rows}
    _, rows = db.fetch("SELECT rule_id, array_agg(target_id ORDER BY target_id)"
                       " FROM knowledge.rule_applies_to GROUP BY rule_id")
    rule_targets = dict(rows)
    _, rows = db.fetch("SELECT id, label FROM knowledge.item WHERE kind IN ('attribute', 'concept')")
    target_forms = {i: label_forms(label) for i, label in rows if label}
    _, rows = db.fetch("SELECT item_id FROM knowledge.item_embedding WHERE model = %s AND variant = %s",
                       (EMBED_MODEL, VARIANT))
    embedded = {r[0] for r in rows}
    return term_forms, rule_targets, target_forms, embedded


def vector_rank(vec: list[float]) -> list[str]:
    v = to_pgvector(vec)
    _, rows = db.fetch("SELECT item_id FROM knowledge.item_embedding WHERE model = %s AND variant = %s"
                       " ORDER BY vec <=> %s::vector, item_id", (EMBED_MODEL, VARIANT, v))
    return [r[0] for r in rows]


def motivation_gold(candidates: set[str]) -> dict[str, set[str]]:
    """문항별 정답 항목(knowledge.item_motivation ∩ candidates). 읽기 계정에는 막혀 있어 knowledge 계정으로 읽는다.
    scripts/eval_retrieval.py·render_context.py 채점용."""
    with db.connect_knowledge() as conn, conn.cursor() as cur:
        cur.execute("SET TRANSACTION READ ONLY")
        cur.execute("SELECT question_id, item_id FROM knowledge.item_motivation")
        gold: dict[str, set[str]] = {}
        for qid, item in cur.fetchall():
            if item in candidates:
                gold.setdefault(qid, set()).add(item)
    return gold


def _key(question: str) -> str:
    return hashlib.sha256(question.encode()).hexdigest()


def _read_cache() -> dict[str, list[float]]:
    return json.loads(CACHE_PATH.read_text()) if CACHE_PATH.exists() else {}


def is_cached(question: str) -> bool:
    return _key(question) in _read_cache()


def question_vector(question: str) -> list[float]:
    """질문 임베딩. 캐시(질문 sha256 키)에 없으면 임베딩 API를 부르고 캐시에 더한다."""
    cache = _read_cache()
    key = _key(question)
    if key not in cache:
        cache[key] = embed(question)
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(cache))
    return cache[key]


def search(question: str) -> list[str]:
    """C층 후보(임베딩된 항목 − A·B층 고정 항목)를 hybrid(RRF)로 매겨 상위 DEFAULT_K개를 돌려준다.

    임베딩이나 벡터 검색이 실패하면 키워드 순위만 쓴다.
    """
    term_forms, rule_targets, target_forms, embedded = load_index()
    candidates = embedded - FIXED_ITEMS
    kw = [i for i in keyword_rank(question, term_forms, rule_targets, target_forms) if i in candidates]
    try:
        vec = [i for i in vector_rank(question_vector(question)) if i in candidates]
    except Exception as e:
        log.warning("질문 임베딩·벡터 검색 실패, 키워드 순위만 씁니다: %s", e)
        vec = []
    return rrf(vec, kw)[:DEFAULT_K]
