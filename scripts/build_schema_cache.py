"""public 허용 테이블의 스키마 카드 캐시(data/schema_catalog.json)를 다시 만든다."""

from financial_product_agent.config import ALLOWED_TABLES, SCHEMA_CACHE_PATH
from financial_product_agent.schema_catalog import build_cache, schema_card

if __name__ == "__main__":
    build_cache()
    print(f"saved: {SCHEMA_CACHE_PATH}")
    for t in ALLOWED_TABLES:
        print(f"{t}: {len(schema_card([t])):,} chars")
