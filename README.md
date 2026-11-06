# portfolio-agent

금융상품 테이블(PostgreSQL `public` 스키마 5개)에 자연어로 질문하면 SQL로 조회·집계해서 답하는 에이전트입니다.
LangGraph + HyperCLOVA X(`HCX-007`, `langchain-naver`의 `ChatClovaX`)로 만들었습니다.
설계·운영 세부(노드별 동작, 평가 절차, 온톨로지 규칙)는 [CLAUDE.md](CLAUDE.md)에 있습니다.

## 대상 테이블
| 테이블 | 내용 |
|---|---|
| `pref01n001` | 국내 ETF·ETN |
| `pref02n001` | 해외 ETF·ETN |
| `prfd01n001` | 펀드 |
| `prbd01n001` | 채권 |
| `domestic_etf_holdings` | 국내 ETF 구성종목 (`item_id = pd_itm_no`로 `pref01n001`과 연결) |

## 실행
```bash
uv sync
cp .env.example .env                        # 값을 채운다
uv run portfolio-agent                      # 대화 모드 (/new: 새 대화, exit: 종료)
uv run portfolio-agent -q "삼성전자를 편입한 국내 ETF는 몇 개야?" --show-sql
uv run portfolio-agent -q "질문" --show-sql --ontology abc --enforce --slots   # 골든 기준선 구성
```
- `--ontology off|ab|abc` (기본 `off`): `ontology/`의 개념·용어·규칙을 프롬프트에 넣습니다. `abc`는 질문으로 C층을 검색해 더합니다.
- `--enforce` (기본 off): 기계적으로 검사할 수 있는 규칙(`sql_rules.py`)을 코드가 강제합니다. SQL 재작성, 실행 전 차단, 답변의 `[적용 조건]`.
- `--slots` (기본 off): 질문의 이름·등급·범주 값을 구조화 출력으로 뽑아 코드가 SQL 조각으로 바꿉니다.

## 구조
```
START → select_tables → [retrieve_knowledge] → [analyze_question → resolve_slots] → generate_query ─┬─(answer)→ END
                                                                                        ↑          └─(query)→ run_query → count_attempt ─┘
```
- `select_tables`: 질문에 필요한 테이블을 구조화 출력으로 고릅니다.
- `retrieve_knowledge` (`--ontology`): 선택된 테이블의 A·B층과 질문으로 검색한 C층 지식을 프롬프트 글로 조립합니다.
- `analyze_question → resolve_slots` (`--slots`): 이름·등급·범주 값을 뽑아 DB와 카탈로그로 확인한 값을 프롬프트에 붙입니다.
- `generate_query`: 선택된 테이블의 스키마 카드와 도메인 규칙을 보고 SQL을 쓰거나(`query`) 답합니다(`answer`). 구조화 출력이라 "조회하겠습니다"만 하고 끝나는 일이 없습니다. 쿼리는 최대 3회입니다.
- `run_query`: `sql_guard`가 단일 SELECT와 허용 테이블만 통과시키고, 읽기 전용 세션에서 15초 타임아웃으로 실행합니다. 오류는 수정 힌트와 함께 LLM에 돌려줍니다.

## 디렉터리
| 경로 | 내용 |
|---|---|
| `src/portfolio_agent/` | 에이전트 패키지. `graph.py`(그래프), `prompts.py` + `domain_rules.md`(프롬프트), `sql_guard.py`·`sql_rules.py`(SQL 검사·강제), `slots.py`(칸 해석), `context.py`·`retrieval.py`·`search.py`(온톨로지 조립·검색), `golden_eval.py`(채점), `config.py`(설정·경로 상수) |
| `scripts/` | 스키마 캐시, 온톨로지 적재·임베딩, 골든 검증·채점 스크립트 |
| `tests/` | pytest. 마커 `db`(PostgreSQL 필요), `llm`(HyperCLOVA X 호출) |
| `golden/` | 골든 30문항 `questions_v2.yaml`, 홀드아웃 30문항 `holdout_v2.yaml`, 설계 `design.md`, 정답 스냅샷, `analysis/`(측정 기록) |
| `ontology/` | 데이터 밖 정의 YAML(정본). PostgreSQL `knowledge` 스키마는 사본 |
| `data/` | `schema_catalog.json`(추적)과 측정 산출물(gitignore) |
| `docs/` | 설계 문서, 학습 노트, 이전 도메인 규칙 보관본, 포트폴리오 슬라이드 |

## HyperCLOVA X 제약 (HCX-007)
- 도구 호출과 구조화 출력은 `thinking={"effort": "none"}`일 때만 동작합니다.
- `tool_choice`(강제 호출)는 지원되지 않습니다. 그래서 테이블 선택과 SQL 생성을 구조화 출력으로 하고, 스키마는 도구가 아니라 프롬프트에 넣습니다.
- 한도는 분당 요청 60회, 토큰 6만 개입니다. 프롬프트는 선택된 테이블 것만 넣고, 429가 나면 15초 이상 기다렸다가 재시도합니다.

## 개발
```bash
uv run pytest                                        # 전체 (DB 필요한 테스트 포함)
uv run pytest -m "not db"                            # DB 없이
uv run ruff check
uv run python scripts/build_schema_cache.py          # DB 데이터가 바뀌면 스키마 카드 재생성
uv run python scripts/load_ontology.py               # ontology/*.yaml → knowledge 스키마
uv run python scripts/embed_ontology.py              # 바뀐 용어·규칙 문서 임베딩
uv run python scripts/verify_golden.py               # 골든 정답 스냅샷 재생성
uv run python scripts/eval_golden.py --label <commit>_abc_enf_slots --ontology abc --enforce --slots   # 골든 채점
```
