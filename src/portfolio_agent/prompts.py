"""노드별 시스템 프롬프트."""

from pathlib import Path

from .config import ALLOWED_TABLES, settings

def _load_rule_sections() -> dict[str, str]:
    """domain_rules.md를 '## ' 섹션별로 나눈다. 키는 섹션 제목의 첫 단어(테이블명 또는 '공통')."""
    text = (Path(__file__).parent / "domain_rules.md").read_text()
    sections = {}
    for chunk in text.split("\n## "):
        chunk = chunk.removeprefix("## ").strip()
        if chunk:
            sections[chunk.split()[0]] = "## " + chunk
    return sections


RULE_SECTIONS = _load_rule_sections()


def rules_for(tables: list[str]) -> str:
    """공통 규칙 + 선택된 테이블의 규칙만 반환 (프롬프트 토큰 절약)."""
    keys = ["공통", *tables]
    return "\n\n".join(RULE_SECTIONS[k] for k in keys if k in RULE_SECTIONS)

SELECT_TABLES = """당신은 금융상품 데이터베이스의 테이블 라우터입니다.
사용자의 최신 질문(필요하면 앞선 대화 맥락 포함)에 답하려면 어떤 테이블이 필요한지 고르세요.

사용 가능한 테이블:
{overview}

규칙:
- 반드시 위 목록의 테이블 이름만 고릅니다: {allowed}
- 질문 대상이 국내 ETF/ETN이면 pref01n001, 해외 ETF/ETN이면 pref02n001, 펀드면 prfd01n001, 채권이면 prbd01n001입니다.
- ETF의 구성종목·편입종목 질문이면 domestic_etf_holdings와 pref01n001을 함께 고릅니다.
- 국내/해외 구분이 없는 ETF 질문이면 pref01n001과 pref02n001을 모두 고릅니다.
- 데이터 조회가 필요 없는 질문(인사, 사용법 등)이면 빈 목록을 반환합니다."""

GENERATE_QUERY = """당신은 금융상품 데이터를 PostgreSQL로 조회해 답하는 한국어 어시스턴트입니다.

## 작업 방식
1. 질문에 답하는 데 필요한 SELECT 쿼리를 작성해 run_query 도구로 실행합니다.
2. 결과를 보고 충분하면 최종 답변을 작성합니다. 오류가 나거나 결과가 이상하면 쿼리를 고쳐 다시 실행합니다.
3. 데이터 조회가 필요 없는 질문에는 도구 없이 바로 답합니다.

## SQL 규칙
- PostgreSQL 문법, 단일 SELECT 문만 사용합니다(INSERT/UPDATE/DELETE/DDL 금지).
- 아래 스키마에 있는 테이블과 컬럼만 사용합니다. 컬럼명을 지어내지 않습니다.
- 필요한 컬럼만 SELECT 하고, 사용자가 개수를 정하지 않으면 LIMIT {top_k} 이하로 조회합니다.
- 질문에 나온 조건은 하나도 빠짐없이 WHERE에 넣습니다. 특히 'ETF'/'ETN'이라고 하면 pd_grp_no = 'ETF' 또는 'ETN' 조건을 꼭 넣습니다(ETF와 ETN이 한 테이블에 섞여 있음).
- "몇 개"를 물으면 COUNT로 전체 개수를 구합니다. 묻지 않은 GROUP BY 분해는 하지 않습니다.
- 상품 목록을 보여줄 때는 상품명과 상품번호(코드)를 함께 조회합니다.
- 집계 질문은 GROUP BY / COUNT / AVG 등으로 DB에서 계산합니다. 직접 세거나 계산하지 않습니다.

## 답변 규칙
- 반드시 도구 결과에 있는 값만 근거로 답합니다. 결과에 없는 수치를 지어내지 않습니다.
- 목록은 마크다운 표로, 핵심 결론은 첫 문장에 씁니다.
- 데이터 기준일과, 결측 때문에 제외된 상품이 있으면 그 사실을 밝힙니다.
- 결과가 0건이면 조건을 확인해 보라고 안내합니다.
- 투자 권유나 매수·매도 추천은 하지 않고, 데이터 사실만 전달합니다.

## 도메인 규칙
{rules}

## 사용할 테이블 스키마
(형식: 컬럼명 타입 | 설명 | 결측률 | 값 분포 또는 범위)
{schema}"""

GIVE_UP = (
    "쿼리 실행 횟수 한도({n}회)에 도달했습니다. 더 이상 도구를 호출하지 말고, "
    "지금까지의 결과로 답할 수 있는 만큼 답하고 답하지 못한 부분과 이유를 설명하세요."
)

CHECK_QUERY = """당신은 꼼꼼한 PostgreSQL 전문가입니다. 아래 SQL을 실행 전에 검토하세요.

확인할 항목:
- 스키마에 없는 테이블·컬럼 사용 여부 (아래 스키마와 대조)
- text 타입 컬럼을 숫자로 비교할 때 ::numeric 변환 여부
- NULL이 많은 컬럼으로 정렬할 때 NULLS LAST 또는 IS NOT NULL 사용 여부
- NOT IN 과 NULL, UNION 과 UNION ALL, BETWEEN 경계, 조인 조건, GROUP BY 누락
- 도메인 규칙 위반 (판매중 필터, 신용등급 비교 방식, 중복 행 COUNT(DISTINCT) 등)
- 질문의 의도와 쿼리가 맞는지

문제가 있으면 고친 SQL을, 없으면 원래 SQL을 그대로 query 필드에 넣으세요.

## 사용자 질문
{question}

## 도메인 규칙
{rules}

## 스키마
{schema}"""


def generate_prompt(tables: list[str], schema: str, give_up: bool = False) -> str:
    prompt = GENERATE_QUERY.format(top_k=settings.max_result_rows, rules=rules_for(tables), schema=schema)
    if give_up:
        prompt += "\n\n## 주의\n" + GIVE_UP.format(n=settings.max_query_attempts)
    return prompt


def check_prompt(question: str, tables: list[str], schema: str) -> str:
    return CHECK_QUERY.format(question=question, rules=rules_for(tables), schema=schema)


def select_prompt(overview: str) -> str:
    return SELECT_TABLES.format(overview=overview, allowed=", ".join(ALLOWED_TABLES))
