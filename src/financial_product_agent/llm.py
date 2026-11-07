"""HyperCLOVA X(ChatClovaX) 모델 생성.

HCX-007은 추론(thinking) 모드에서 tools / response_format을 지원하지 않으므로
도구 호출·구조화 출력이 필요한 노드는 thinking effort를 'none'으로 둔다.
tool_choice(강제 호출)는 지원되지 않는다.
"""

import logging
import time
from functools import lru_cache

import openai

from langchain_naver import ChatClovaX

from .config import settings

log = logging.getLogger(__name__)

_retry_wait_seconds = 0.0  # 프로세스 시작 이후 429 재시도로 기다린 누적 시간 (평가 비용 기록용)


@lru_cache
def get_llm(max_tokens: int = 2048) -> ChatClovaX:
    return ChatClovaX(
        model=settings.model,
        temperature=0,
        max_tokens=max_tokens,
        thinking={"effort": "none"},
        # 429(분당 요청 60회·토큰 6만 한도)는 invoke_with_retry가 길게 기다려 처리한다.
        max_retries=1,
    )


def retry_wait_seconds() -> float:
    return _retry_wait_seconds


def invoke_with_retry(runnable, messages, attempts: int = 5):
    """분당 토큰 한도(429)에 걸리면 한도가 풀릴 때까지 기다렸다가 다시 호출한다.

    openai 클라이언트의 기본 백오프(최대 수 초)로는 60초 단위 토큰 한도가 풀리지 않아 따로 둔다.
    """
    global _retry_wait_seconds
    for i in range(attempts):
        try:
            return runnable.invoke(messages)
        except openai.RateLimitError:
            if i == attempts - 1:
                raise
            wait = 15 * (i + 1)
            log.warning("CLOVA Studio rate limit; retrying in %ss", wait)
            time.sleep(wait)
            _retry_wait_seconds += wait
