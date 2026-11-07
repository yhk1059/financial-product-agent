"""온톨로지 검색용 문서 조립과 CLOVA 임베딩 v2(bge-m3) 호출."""

import hashlib
import time
from functools import lru_cache

import openai
from langchain_naver import ClovaXEmbeddings

from . import config  # noqa: F401  (.env 로드, CLOVASTUDIO_API_KEY 매핑)

EMBED_MODEL = "bge-m3"  # CLOVA Studio 임베딩 v2
EMBED_DIM = 1024
# 문서 조립 방식. 용어: 이름 + 같은 말 + 정의 / 규칙: when + 적용 대상 이름 (then 제외, 항목당 1개)
VARIANT = "base"

_RETRY_WAITS = (15, 30, 60)


def term_doc(label: str, surface_forms: list[str], definition: str) -> str:
    return f"{label}. 같은 말: {', '.join(surface_forms)}. {definition}"


def rule_doc(when_text: str, target_labels: list[str]) -> str:
    return f"{when_text} 대상: {', '.join(target_labels)}."


def content_sha(content: str, model: str = EMBED_MODEL, variant: str = VARIANT) -> str:
    return hashlib.sha256(f"{model}\n{variant}\n{content}".encode()).hexdigest()


@lru_cache
def _client() -> ClovaXEmbeddings:
    return ClovaXEmbeddings(model=EMBED_MODEL, dimensions=None)


def embed(text: str) -> list[float]:
    """텍스트 하나를 임베딩한다. 429는 15s/30s/60s 기다린 뒤 다시 시도한다."""
    for wait in (*_RETRY_WAITS, None):
        try:
            vec = _client().embed_query(text)
            break
        except openai.RateLimitError:
            if wait is None:
                raise
            time.sleep(wait)
    if len(vec) != EMBED_DIM:
        raise ValueError(f"임베딩 차원 {len(vec)} != {EMBED_DIM}")
    return vec


def to_pgvector(vec: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"
