# portfolio-agent

금융상품 테이블(PostgreSQL public 스키마 5개)에 대해 자연어로 질문하면, SQL로 조회·집계해서 답하는 에이전트입니다.
LangGraph + HyperCLOVA X(`HCX-007`, `langchain-naver`의 `ChatClovaX`)로 만들었습니다.

## 대상 테이블
| 테이블 | 내용 |
|---|---|
| `pref01n001` | 국내 ETF·ETN |
| `pref02n001` | 해외 ETF·ETN |
| `prfd01n001` | 펀드 |
| `prbd01n001` | 채권 |
| `domestic_etf_holdings` | 국내 ETF 구성종목 |

## 실행
```bash
uv sync
uv run portfolio-agent                      # 대화 모드 (/new: 새 대화, exit: 종료)
uv run portfolio-agent -q "삼성전자를 편입한 국내 ETF는 몇 개야?" --show-sql
```
`.env`에 `PG*`(읽기 전용 계정), `CLOVA_STUDIO_API_KEY`, `CLOVASTUDIO_MODEL`, `LANGSMITH_*`가 있어야 합니다.

## 구조
```
START → select_tables → generate_query ─┬─(도구 호출 없음)→ END
                              ↑          └─(도구 호출)→ [check_query] → run_query ─┘
```
- `select_tables`: 질문에 필요한 테이블을 구조화 출력으로 고릅니다.
- `generate_query`: 선택된 테이블의 스키마 카드와 도메인 규칙을 보고 `run_query` 도구로 SQL을 실행합니다. 결과가 나오면 답변을 씁니다. 쿼리는 최대 3회까지 실행합니다.
- `check_query`(선택, `--check`): 실행 전에 SQL을 검토합니다. LLM 호출이 늘어 기본값은 꺼져 있습니다.
- `run_query`: `sql_guard`가 단일 SELECT 문과 허용 테이블만 통과시킵니다. 실행은 읽기 전용 세션에서 하고 타임아웃은 15초입니다.

| 파일 | 역할 |
|---|---|
| `src/portfolio_agent/graph.py` | LangGraph 그래프 |
| `src/portfolio_agent/prompts.py` | 노드별 프롬프트 |
| `src/portfolio_agent/domain_rules.md` | 테이블별 도메인 규칙 (판매중 필터, 결측 해석, 신용등급 순서 등) |
| `src/portfolio_agent/schema_catalog.py` | 컬럼 설명·결측률·값 분포를 담은 스키마 카드 (`data/schema_catalog.json` 캐시) |
| `src/portfolio_agent/sql_guard.py` | SQL 안전 검사 |

## HyperCLOVA X 제약 (HCX-007)
- 도구 호출과 구조화 출력은 `thinking={"effort": "none"}`일 때만 동작합니다.
- `tool_choice`(강제 호출)는 지원되지 않습니다. 그래서 튜토리얼의 강제 호출 단계를 구조화 출력과 결정적 노드로 바꿨습니다.
- 한도는 분당 요청 60회, 토큰 6만 개입니다. 프롬프트는 선택된 테이블 것만 넣고, 429가 나면 15초 이상 기다렸다가 재시도합니다.

## 개발
```bash
uv run pytest                                # sql_guard·DB 테스트
uv run python scripts/build_schema_cache.py  # DB 데이터가 바뀌면 스키마 카드 재생성
uv run python scripts/run_eval.py            # 평가 질문셋 정답률 측정
```
