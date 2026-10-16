"""환경 변수(.env) 로드와 에이전트 설정값."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

# langchain-naver는 CLOVASTUDIO_API_KEY를 읽는다.
if "CLOVASTUDIO_API_KEY" not in os.environ and "CLOVA_STUDIO_API_KEY" in os.environ:
    os.environ["CLOVASTUDIO_API_KEY"] = os.environ["CLOVA_STUDIO_API_KEY"]

# 에이전트가 접근할 수 있는 테이블 (public 스키마)
ALLOWED_TABLES: tuple[str, ...] = (
    "pref01n001",
    "pref02n001",
    "prfd01n001",
    "prbd01n001",
    "domestic_etf_holdings",
)

SCHEMA_CACHE_PATH = PROJECT_ROOT / "data" / "schema_catalog.json"


@dataclass(frozen=True)
class Settings:
    model: str = os.getenv("CLOVASTUDIO_MODEL", "HCX-007")
    statement_timeout_ms: int = 15_000
    default_limit: int = 200  # LIMIT 없는 쿼리에 강제로 붙이는 값
    max_result_rows: int = 50  # LLM에 돌려주는 최대 행 수
    max_result_chars: int = 8_000
    max_query_attempts: int = 3


settings = Settings()
