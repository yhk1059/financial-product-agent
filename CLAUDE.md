# CLAUDE.md

이 저장소에서 Claude Code(claude.ai/code)가 작업할 때 따르는 안내입니다.

## 개요

PostgreSQL `public` 스키마의 금융상품 테이블 5개에 자연어로 질문하면, 테이블을 고르고 SQL을 써서 읽기 전용으로 실행한 뒤 답하는 에이전트입니다. LangGraph와 HyperCLOVA X(`HCX-007`, `langchain-naver`의 `ChatClovaX`)로 만들었습니다. 코드 주석·프롬프트·문서는 한국어로 씁니다. 그대로 유지합니다.

테이블: `pref01n001`(국내 ETF·ETN), `pref02n001`(해외 ETF·ETN), `prfd01n001`(펀드), `prbd01n001`(채권), `domestic_etf_holdings`(국내 ETF 구성종목, `item_id = pd_itm_no`로 `pref01n001`과 연결).

## 명령

`uv`로 관리합니다(Python ≥3.13). `.env`(`.env.example`을 복사)에 `PG*`(읽기 전용 계정), `CLOVA_STUDIO_API_KEY`, `CLOVASTUDIO_MODEL`, `LANGSMITH_*`가 있어야 합니다. `KNOWLEDGE_PGUSER`/`KNOWLEDGE_PGPASSWORD`(`knowledge_writer`, `knowledge` 스키마만 씀)는 `load_ontology.py`, `embed_ontology.py`, 그리고 `eval_retrieval.py`·`render_context.py`가 골든 정답 항목을 읽을 때만 필요합니다. `METADATA_PGUSER`와 `SEMANTIC_DATABASE_URL`은 쓰지 않습니다. `agent_metadata`, `ontology`, `semantic` 스키마는 다른 작업의 것입니다.

```bash
uv sync
uv run portfolio-agent                                   # 대화 모드 (/new = 새 스레드, exit = 종료)
uv run portfolio-agent -q "질문" --show-sql [--ontology off|ab|abc]   # 한 번만. --ontology는 온톨로지 컨텍스트를 넣음 (기본 off)
uv run portfolio-agent -q "질문" --show-sql --enforce   # 기계적 규칙을 코드가 강제 (sql_rules.py, 기본 off)
uv run portfolio-agent -q "질문" --show-sql --slots     # analyze_question + resolve_slots로 이름·등급·범주를 SQL 조각으로 (기본 off, 턴당 LLM 호출 +1)
uv run pytest                                            # 전체 테스트
uv run pytest tests/test_sql_guard.py                    # 순수 단위 테스트, DB·LLM 불필요
uv run pytest -m "not db"                                # PostgreSQL이 필요한 테스트 제외
uv run pytest tests/test_sql_guard.py::test_allows_select
uv run ruff check                                        # 린트 (pyproject.toml `[tool.ruff]`, line-length 120)
uv run python scripts/build_schema_cache.py              # DB 데이터가 바뀌면 data/schema_catalog.json 재생성
uv run python scripts/verify_golden.py                   # 정답 SQL 재실행 → golden/gold_results_v2.json, review_sheet.md
uv run python scripts/verify_golden.py --questions golden/holdout_v2.yaml --out golden/gold_results_holdout_v2.json   # 홀드아웃 정답 (review_sheet 없음)
uv run pytest tests/test_ontology.py                     # 온톨로지 YAML 구조·실재성·누수·커버리지 검사, DB·LLM 불필요
uv run python scripts/load_ontology.py [--dry-run]       # ontology/*.yaml을 knowledge 스키마에 다시 적재 (--dry-run: 행 수만, DB 없음)
uv run python scripts/embed_ontology.py [--dry-run]      # 바뀐 용어·규칙 문서를 knowledge.item_embedding에 임베딩 (--dry-run: 문서 출력만, API·DB 쓰기 없음)
uv run python scripts/eval_retrieval.py                  # C층 검색 채점 (vector/keyword/hybrid recall@k), HCX 대화 호출 없음
uv run python scripts/render_context.py [--label render_v2]      # 골든 문항별 온톨로지 프롬프트 컨텍스트 조립, 프롬프트 증가량 보고, HCX 대화 호출 없음
uv run python scripts/eval_golden.py --label base [--ontology off|ab|abc|oracle] [--enforce] [--slots] [--runs 3] [--only C1-01,C4-06]   # 골든 v2 채점 (oracle은 진단 전용)
uv run python scripts/eval_golden.py --set holdout --label <commit>_<mode>_holdout [...]   # holdout_v2(30문항) 채점. 여기에 맞춰 튜닝하지 않는다
uv run python scripts/eval_golden.py --rescore data/golden_runs/archive/base_r1.jsonl [--write] [--label base_v2]   # DB·LLM 없이 현재 골든 버전으로 다시 채점
uv run python scripts/eval_golden.py --compare base base_enf                                 # 라벨 간 문항별 비교표
```

pytest 마커: `db`(PostgreSQL 필요), `llm`(HyperCLOVA X 호출). 2026-11-07 기준 테스트 1,463개.

## 디렉터리

| 경로 | 내용 |
|---|---|
| `src/portfolio_agent/` | 에이전트 패키지(평면 구조, 모듈 18개). 하위 패키지 분리는 2026-11-06에 보류했다 |
| `scripts/` | 스키마 캐시·온톨로지 적재·임베딩·골든 검증·채점. 패키지만 import하고 스크립트끼리는 import하지 않는다 |
| `tests/` | 모듈별 pytest 파일 하나씩 |
| `golden/` | `design.md`(정본), `questions_v2.yaml`·`gold_results_v2.json`, `holdout_v2.yaml`·`gold_results_holdout_v2.json`, `review_sheet.md`(생성물), `analysis/`(측정 기록) |
| `ontology/` | `model.yaml`·`glossary.yaml`·`rules.yaml`·`review_log.yaml`(정본), `sql/knowledge_schema.sql` |
| `data/` | `schema_catalog.json`(추적). `golden_runs/`·`retrieval_runs/`·`retrieval_cache/`·`context_runs/`·`eval_logs/`는 gitignore |
| `docs/` | `plan_query_slots_design.md`(칸 설계), `온톨로지_따라하기_정리.md`(책 정리), `domain_rules_legacy.md`(옛 규칙, 에이전트는 읽지 않음), `portfolio/`(슬라이드 HTML·PDF 추적, PNG는 gitignore) |

`.env.example`은 변수 이름만 적은 본보기입니다. 값은 `.env`에만 둡니다.

## 아키텍처

그래프(`src/portfolio_agent/graph.py`):

```
START → select_tables → [retrieve_knowledge] → [analyze_question → resolve_slots] → generate_query ─┬─(도구 호출 없음)→ END
                                                                                        ↑          └─(도구 호출)→ run_query → count_attempt ─┘
```

- **select_tables**: 구조화 출력(`TableSelection`, `json_schema`). `ALLOWED_TABLES`의 `Literal`로 제한합니다. 실패하면 전체 테이블로 물러납니다. `domestic_etf_holdings`를 고르면 항상 `pref01n001`을 더합니다.
- **retrieve_knowledge** (`ontology`가 `off`가 아닐 때만. 모드는 `config.ONTOLOGY_MODES`): `ab`는 선택된 테이블의 A·B층을 만들고, `abc`는 마지막 사용자 질문으로 `search.search`를 돌려 C층도 만듭니다. `oracle`(평가 전용, `config.EVAL_ONTOLOGY_MODES`. 대화 CLI에는 없음)은 검색 대신 입력 상태의 `oracle_ids`를 C층으로 씁니다. `eval_golden.py`가 glossary/rules의 `motivated_by`로 문항마다 채워 넣어, 검색이 완벽하면 맞히는지 봅니다. 그 실행은 진단이지 기준선이 아닙니다. `context.build_context(get_knowledge(), tables, c_ids)`를 부르고 `knowledge_rules`/`knowledge_tables`/`knowledge_layers`/`knowledge_oos`를 상태에 둡니다. `knowledge_oos`(`Context.oos_text`)는 C층의 `in_data: false` 용어 중 `scope_tables`가 비었거나 선택된 테이블인 것의 목록이고, `generate_prompt(..., oos=)`가 `## 이 데이터에 없는 정보`로 붙여 용어 이름을 들어 거절하라고 지시합니다(C6). 예외가 나면 로그를 남기고 빈 문자열을 돌려주므로 `off`와 같은 프롬프트가 됩니다. LLM 호출은 늘지 않습니다.
- **analyze_question → resolve_slots** (`--slots`, 기본 off. 설계는 `docs/plan_query_slots_design.md`): 책 7-1(분석 LLM은 값을 넘기고 판단은 코드가 한다)을 따릅니다. `analyze_question`은 턴마다 한 번 `ANALYZE_QUESTION` + `_recent_dialogue`만으로(스키마·규칙 없이) 구조화 출력 `slots.QuestionSlots`(`names` [text, kind 상품|발행기관|구성종목], `grades` [term, value, direction 이상|이하|초과|미만|같음], `values` [범주 단어])를 받아 상태 `slots`에 둡니다. `resolve_slots`는 `slots.resolve(slots, get_knowledge(), load_catalog(), db.DbLookup(), tables, dialogue)`를 부릅니다(순수 함수. DB는 주입된 lookup으로만). `validate_slots`는 대화에 없는 텍스트와 어느 순서 목록에도 없는 등급 값을 버립니다. 이름은 선택된 테이블의 `product_name`/`issuer`/`holding` canonical 컬럼에서 정확 → 정규화(공백·대소문자·법인 표기) → 부분 순으로 찾습니다(정확 일치 또는 정규화 단일 일치 = `confirmed`, 상품 식별자 id와 다른 선택 테이블로 가는 `via` 조건 포함. 부분 일치는 하나뿐이어도 `candidates`. 구성종목은 정확 일치만 확정). 등급은 `term` 단어나 값이 들어 있는 순서 목록으로 순서 용어를 고르고, `order_up`으로 `order_values`를 자른 뒤 카탈로그로 저장된 값에 맞춥니다(띄어쓰기·`(n등급)` 변형, 펀드 코드 컬럼은 위치로). 범주는 `bundle: true` 용어의 canonical 값 전체를 먼저 쓰고, 없으면 카탈로그 범주 값(같음, 그다음 텍스트에 포함)을 씁니다. `not_found`인 이름은 한국어 값이 텍스트 거의 전부를 덮을 때 범주 값으로 다시 시도합니다(`국내주식형` → `주식형`은 되고, `해외 채권형 ETF` → `ETF`는 안 됨). `scan_values`는 LLM 없이 마지막 질문에 그대로 나오는 카탈로그 범주 값(한국어, 3자 이상, 순서 용어 컬럼 아님, 더 긴 일치 값에 덮이지 않음)을 더합니다. `prefer_confirmed_columns`는 같은 질문의 다른 확정 값이 그 컬럼 중 하나를 쓰면 여러 컬럼에 걸린 `candidates` 값을 `confirmed`로 올립니다. `render_resolved`는 `confirmed`/`candidates`/`not_found`만 상태 `resolved_text`로 보여 주고 `generate_prompt(..., resolved=)`가 `## 확인된 값`으로 붙입니다. 실패하면 비워 두므로 슬롯 없는 프롬프트와 같습니다. `--enforce`를 함께 켜면 `make_run_query(..., slot_checks=True)`가 `InjectedState`로 `resolved`를 읽고, `slots.slot_violations`가 확정 등급 컬럼의 조건이 확정 집합과 다르거나(범위 비교 포함) 묶음의 진부분집합을 쓰거나 확정 값의 컬럼을 아예 참조하지 않거나 그 컬럼에 다른 값만 쓰는(UNION 한 분기에 값이 있으면 충분) SQL을 턴당 최대 `slots.MAX_STRIKES`(2)번 막습니다(그 뒤는 알림만). 답변의 `[적용 조건]`에 `이름 해석: …` 줄이 붙습니다(`slots.name_notes`). `AskTrace.slots`/`resolved`가 둘 다 기록하고, `eval_golden.py --slots`는 요약에 슬롯 표를 더합니다(`used` = 확정 컬럼과 값이 마지막 성공 SQL에 나타남).
- **generate_query**: 시스템 프롬프트에는 선택된 테이블의 스키마 카드와 도메인 규칙만 들어갑니다. `knowledge_rules`가 있으면 `{rules}`를 대신하고(`generate_prompt(..., rules=)`. `None`이면 `rules_for` 유지) `knowledge_tables`는 스키마 카드 뒤에 붙습니다. `off`는 온톨로지 이전 프롬프트와 바이트 단위로 같아야 합니다(`tests/test_prompts.py`, `tests/test_graph.py`). LLM은 질문 해석과 SQL 작성만 하고 실행은 코드가 강제합니다. `bind_tools`가 아니라 `json_schema` 구조화 출력 `QueryStep`(`action` query|answer, `sql`, `answer`. 모든 필드 필수, 안 쓰는 것은 빈 문자열)으로 부릅니다. `action=query`면 노드가 직접 `run_query` 도구 호출(`id`는 `call_<uuid>`)이 든 `AIMessage`를 만들어 라우팅·`count_attempt`·`ToolNode`·`cli.ask`의 SQL 수집이 그대로 동작하고, `action=answer`면 일반 `AIMessage`를 돌려 그래프가 끝납니다. 그래서 "조회하겠습니다"만 하고 조회하지 않는 일은 없고, `answer`를 잘못 고르는 것만 남습니다. `_llm_history`는 LLM에 보내는 사본만 고칩니다(상태는 그대로): 도구 호출 `AIMessage`는 `[실행한 SQL]` 텍스트로, `ToolMessage`는 `[실행 결과]` `HumanMessage`로 바꿉니다. CLOVA API가 도구 정의 없는 히스토리의 도구 메시지를 거부하기 때문입니다. 파싱 오류나 고른 action의 `sql`/`answer`가 비면 한 번 재시도하고, 두 번 다 실패하면 `response_metadata["generation_error"]`가 붙은 고정 사과 `AIMessage`를 돌려주고 경고를 남깁니다. `settings.max_query_attempts`(3)회 실행 뒤에는 `give_up` 모드가 `FinalAnswer` 스키마(`answer`만)를 쓰므로 SQL이 더 나올 수 없습니다.
- **run_query** (`tools.py`): `sql_guard.validate` → `db.fetch` → 정리된 텍스트. 오류는 예외가 아니라 `"Error: ..."` 문자열로 LLM에 돌아갑니다. DB 오류에는 SQLSTATE별 수정 힌트(`sqlstate_hint`: GROUP BY, 텍스트 캐스트, 날짜·interval 문법, 집합 연산 타입 불일치, MySQL 문법)와 오류 뒤 같은 SQL을 다시 보냈을 때의 알림(`REPEAT_NOTE`)이 붙습니다. 실행 전 규칙 차단은 `Error: [규칙]`(`sql_rules.RULE_MARK`)로 시작합니다.
- **count_attempt**는 `run_query` 뒤에 돕니다. `attempts` = 이번 턴에 실행됐거나 DB에서 실패한 쿼리 + `FREE_RULE_BLOCKS`(3)를 넘는 규칙 차단. 그래서 코드 차단이 `max_query_attempts`(3) 예산을 먹지 않으면서 무한 반복도 못 합니다. 두 종류 모두 오류 위치를 `>>token<<`으로 표시합니다. 가드 문법 오류는 sqlglot의 문맥을 쓰고(ANSI 밑줄은 LLM이 못 읽음) 토큰이 `AS word` 뒤면(별칭에 공백) 힌트를 더하며, DB 오류는 실행된(가드가 고쳐 쓴) SQL의 `diag.statement_position`에서 문맥을 더합니다. 출력은 `max_result_rows`·`max_result_chars`로 자릅니다.
- **enforce** (`--enforce`, 기본 off. `sql_rules.py`, 순수 함수): 기계적으로 검사할 수 있는 규칙은 프롬프트 글이 아니라 코드가 강제합니다. `generate_query(s, enforce=True)`(노드 이름은 그대로. `cli.ask`가 여기서 답을 모음)가 도구 호출 SQL을 고쳐 씁니다(`sql_rules.rewrite`. 기록된 SQL = 실행된 SQL): `ORDER BY … DESC` → `DESC NULLS LAST`, 결측 10% 이상 컬럼의 `GROUP BY`에 `col IS NOT NULL`, 0=미입력 컬럼을 집계·ORDER BY·범위 비교에 쓰면서 0 제외가 없으면 그 SELECT의 WHERE에 `NULLIF(col, '')::numeric > 0`(텍스트) 또는 `col > 0`(UNION 분기별. 숫자로 시작하는 별칭은 가드처럼 먼저 따옴표로 감쌈). 이번 턴의 성공 SQL에서 찾은 `[적용 조건]` 블록(날짜 리터럴이 있으면 기준일, 결측 10% 이상 컬럼의 NULL 제외, `COUNT(col)`/`COUNT(DISTINCT col)`(결측이 하나라도)이나 AVG/SUM/MIN/MAX(10% 이상)가 조용히 버린 NULL, 제외한 0)을 answer/give_up 답변에 붙입니다. `make_run_query(ordered, zero_missing, names, name_lookup)`는 실행하지 않고 `Error:`를 돌려줍니다: 등급 컬럼의 문자열 범위 비교(`ordered_columns(get_knowledge())`: `order_values`가 있는 용어의 canonical 컬럼, 컬럼 이름으로만 대조), 현재 시각 함수(`settings.as_of_date`를 쓸 것), 0=미입력 컬럼(`zero_missing_columns`: `R-zero-missing` 대상)을 같은 SELECT 범위에서 0 제외(`> 0`, `<> 0`, `NULLIF(…, 0)`, 캐스트 허용) 없이 집계·ORDER BY·범위 비교에 쓴 것, 상품·발행기관 이름(`name_columns`: `product_name`/`issuer` canonical 컬럼)을 `=`/`IN`으로 썼는데 정확히 저장돼 있지 않고 부분 일치 후보가 있는 것(`db.lookup_values`, 후보를 오류에 보여 줌). 후보 없는 모르는 이름과 모르는 구성종목 이름(`holding`)은 실행하고 알림만 붙습니다. `rule_violations`는 P-UNIT(`prbd01n001`에서 `DISTINCT pd_no` 없는 행 단위 COUNT/AVG/SUM. 단 `buyable_quantity` 조건이 있어 매도 행 수를 세는 경우는 제외. `prfd01n001`에서 식별자 아닌 컬럼의 `COUNT(DISTINCT x)`), P-CASH(구성종목 이름을 `component_name NOT IN (cash_like_holding 용어의 현금성 값)` 없이 세거나 나열), 집합 논리(질문에 둘 다/모두/…가 있는데 구성종목 `IN` 목록이 2개 이상이고 `HAVING COUNT(DISTINCT component_name) = n`이 없음), R-ratio-denominator(분자가 결측 10% 이상 컬럼을 보는데 `… / COUNT(*)`)를 더합니다. `violations`는 `R-zero-is-value` 컬럼(해외 보수)에서 0을 제외하는 것도 막습니다. 행이 `default_limit`에 닿으면 알림을 붙입니다. 컬럼 소유자는 SELECT 범위별로 해석하므로(별칭, sqlglot `traverse_scope`) 국내/해외 UNION은 분기별로 판정합니다. `applied_conditions`도 같은 해석을 쓰고 캐스트로 감싼 `> 0`도 잡습니다. off는 바이트 단위로 같아야 합니다.
- 다중 턴 대화는 `thread_id`로 키를 잡은 `InMemorySaver`를 씁니다(`build_chat_agent`). `cli.ask`는 CLI와 `eval_golden.py`가 함께 씁니다. `AskTrace`(답변, 선택 테이블, 실행한 SQL과 도구 출력, `generation_errors`, LLM 호출 수·토큰, 콜백으로 합산한 `input_tokens`와 호출 하나의 최댓값 `max_input_tokens`, `knowledge_layers`, 시간, `llm.retry_wait_seconds()`의 429 대기)를 돌려주고 CLI는 `.answer`를 출력합니다.

보조 모듈:
- `config.py`: 프로젝트 루트의 `.env`를 읽고 `CLOVA_STUDIO_API_KEY` → `CLOVASTUDIO_API_KEY`로 매핑하며, `ALLOWED_TABLES`, `Settings`(타임아웃·한도), 경로 상수(`PROJECT_ROOT`, `DATA_DIR`, `GOLDEN_DIR`, `ONTOLOGY_DIR`, `KNOWLEDGE_DDL_PATH`, `SCHEMA_CACHE_PATH`)를 둡니다. 스크립트와 테스트는 경로를 직접 만들지 않고 이 상수를 씁니다. `scripts/`는 패키지만 import하고 서로는 import하지 않습니다: knowledge 쓰기 계정 접속은 `db.connect_knowledge()`, 골든 `item_motivation` 읽기는 `search.motivation_gold()`입니다. 테이블을 더하려면 `ALLOWED_TABLES`를 고치고, 스키마 캐시를 다시 만들고, `domain_rules.md`에 섹션을 더합니다.
- `sql_guard.py`: sqlglot(postgres 방언)으로 허용된 `public` 테이블에 대한 단일 SELECT/WITH/집합 연산만 통과시킵니다. DML/DDL, `SELECT INTO`, 잠금, `pg_*`/`dblink`/`lo_*`/`set_config`류 함수를 막습니다. 숫자로 시작하는 별칭을 따옴표로 감싸고, LIMIT이 없으면 `LIMIT 200`을 붙이고, `identify=True`로 다시 씁니다. 다층 방어로 `db.connect()`도 읽기 전용 트랜잭션, 15초 `statement_timeout`, `search_path=public`을 강제합니다.
- `schema_catalog.py`: 테이블마다 주석·타입·결측률·고유값 수·범주 값 목록(20개 이하)·범위를 `data/schema_catalog.json`에 프로파일링합니다. `table_overview()`는 테이블 선택에, `schema_card(tables)`는 생성과 검사에 들어갑니다.
- `prompts.py` + `domain_rules.md`: 규칙 파일은 `## ` 제목으로 나눕니다. 키는 제목의 **첫 단어**이고 테이블 이름이나 `공통`과 정확히 같아야 합니다. `공통`과 선택된 테이블의 섹션만 프롬프트에 들어갑니다. `SQL_EXAMPLES`(`examples_for`)도 같은 방식입니다: 일반 SQL 관용구(비율 분모, 같은 테이블 비교 vs 테이블 간 비교, HAVING으로 구성종목 전부 포함, 구성종목 차집합, 채권 종목 단위 집계)를 `공통`과 선택된 테이블에 대해 `## SQL 예시`로 넣습니다. 프롬프트는 한 단어 ASCII 별칭을 요구합니다(한국어·공백 별칭은 문법 오류 반복의 원인이었습니다).
- `llm.py`: `get_llm()`은 `lru_cache`, `temperature=0`, `thinking={"effort": "none"}`. `invoke_with_retry`가 429를 15초/30초/… 대기로 처리합니다.

## HyperCLOVA X (HCX-007) 제약

설계 선택 대부분이 여기서 나옵니다. 표준 LangGraph SQL 에이전트 튜토리얼로 "단순화"하지 마세요:
- 도구 호출과 구조화 출력은 `thinking={"effort": "none"}`일 때만 동작합니다.
- `tool_choice`(강제 도구 호출)는 지원되지 않습니다. 그래서 테이블 선택과 SQL 생성을 도구 호출 대신 구조화 출력으로 하고, 스키마를 도구로 가져오는 대신 프롬프트에 넣습니다. 그냥 `bind_tools`를 쓰면 모델이 "조회해보겠습니다"라고 쓰고 도구 호출 없이 끝나는 일이 있었습니다(골든 `no_query`).
- 한도: 분당 요청 60회, 토큰 6만 개. 프롬프트는 선택된 테이블 범위로 유지합니다. OpenAI 클라이언트의 짧은 백오프로는 부족해서 `invoke_with_retry`가 있습니다.

## 평가

골든 데이터셋은 `golden/`에 있습니다. `design.md`가 해석 정책(P-*), 문항 형식(§4), 채점(§5), 결정 기록(§8)의 정본입니다. `questions_v2.yaml`(현재. v1은 2026-11-07에 삭제했고 그 결정은 design.md §8에 있음)에 `gold_sql`이 있는 30문항(C1–C6)이 있습니다. `verify_golden.py`가 문항별 `sql_sha`를 포함한 `gold_results_v2.json`을 씁니다. `eval_golden.py`의 `VERSION` 상수와 `verify_golden.py`의 `--version` 기본값이 버전을 고릅니다. 해시가 오래되면 `eval_golden.py`가 실행을 거부하므로 `gold_sql`을 고친 뒤에는 `verify_golden.py`를 다시 돌립니다.

채점(`src/portfolio_agent/golden_eval.py`, 순수 함수, `tests/test_golden_eval.py`로 단위 테스트)은 LLM 없는 규칙 기반입니다:
- **실행 정확도:** 성공한 에이전트 SQL을 다시 실행해(자르지 않음, 가드 LIMIT 200) 컬럼 이름이 아니라 값으로 정답과 비교합니다. 타입: `count`/`scalar`는 그 값이 든 행 하나, `list`는 집합 또는 순서 있는 상위 N, `table`은 키 튜플의 다중집합. `label_columns`(정답 SQL이 만든 리터럴, 예: C5-01 `market`)는 일대일 대응으로 맞춥니다. 마지막 성공 쿼리를 먼저 보고 그다음 앞선 쿼리를 봅니다(`exec_match: earlier`). `table`은 여러 쿼리에 나뉜 결과도 `union`으로 인정합니다. `key_alternatives`(예: `pd_abrv_nm` → `pd_nm`, `pd_itm_no`)로 같은 상품을 다른 식별자 컬럼으로도 맞출 수 있습니다(실행·답변 검사 모두).
- **숫자:** 소수점 2자리로 반올림해 같거나 에이전트가 쓴 자릿수로 반올림해 같으면 같은 것으로 봅니다. 정수는 정확히 같아야 합니다. `scalar`는 ×100/÷100도 받습니다(`scale_variant`).
- **답변 텍스트:** `answer_ok`는 정답 키 값(10개 이하)이 답변에 나오는지입니다. `mention_keywords`(`'a|b'` 동의어, `must_mention`과 정렬)와 C6의 `forbidden_patterns`(정규식)도 검사합니다. 언급 키워드는 질문 자체에 나오면 안 되고(스키마 테스트), C6는 일반적인 "찾을 수 없다"가 아니라 문항 고유의 거절 이유를 요구합니다. `ungrounded_numbers`는 참고용입니다. `columns_missing`/`columns_extra`도 참고용입니다: 정답 SQL과 에이전트의 마지막 성공 SQL이 참조한 실제 스키마 컬럼(별칭은 `schema_catalog.json`으로 걸러냄)의 차이로 `wrong_result`를 진단합니다.
- **`correct`:** C1–C5는 실행 일치, `answer_ok`가 false가 아님, 언급 전부를 요구합니다. C6는 거절 키워드와 금지 패턴 없음을 요구합니다. 실패에는 `fail_reason` 하나가 붙습니다.

출력은 `data/golden_runs/`(gitignore)에 갑니다. 현재 기준선만 거기 두고 옛 실행은 `data/golden_runs/archive/`로 옮깁니다(`--compare archive/<label> ...`로 비교). 콘솔 출력(429 재시도, 문항별 표)은 리다이렉션으로 `data/eval_logs/<label>.log`에 저장할 수 있습니다(gitignore. 스크립트가 쓰지 않음). `golden/analysis/runs.md`에 모든 실행의 커밋·모드·점수·상태를 적습니다. 새 실행마다 행을 더합니다. 라벨은 `<commit>_<mode>[_enf][_slots][_x<runs>]`(`_enf` = `--enforce`, `_slots` = `--slots`. 옛 `_plan` 라벨은 삭제된 `--plan`, 옛 `_holdout` 라벨은 삭제된 `holdout_v1`). 프롬프트나 `domain_rules.md`를 바꾸면 평가를 다시 돌립니다(또는 영향받는 id만 `--only`). 채점 규칙을 바꾼 뒤에는 에이전트를 다시 돌리지 말고 `--rescore`를 씁니다.

현재 기준선(2026-11-01, `runs.md`): 골든 `f23859c_abc_enf_slots_x3` 85/90, 홀드아웃 `2c6dcd5_abc_enf_slots_holdout_x3` 61/90. 구성 비교는 `golden/analysis/f23859c_config_x3.md`와 `f4a7ba6_holdout_config_x3.md`에 있습니다.

**홀드아웃:** `golden/holdout_v2.yaml`(30문항, H-01–H-30, 2026-11-01 고정)과 `gold_results_holdout_v2.json`. `--set holdout`으로 돌리고 라벨은 `<commit>_<mode>[_enf][_slots]_holdout`입니다. 분류·테이블·채점은 골든과 같지만 문항마다 같은 정책을 다른 맥락(다른 컬럼·방향·집계·테이블 쌍·이름)에서 묻기 때문에, 골든에 맞춘 코드와 온톨로지가 일반화되는지 잽니다. 홀드아웃 점수에 맞춰 튜닝하지 마세요: 골든에서 고치고 홀드아웃은 한 번만 잽니다. 홀드아웃의 고유명사는 온톨로지나 슬롯 코드에 나오면 안 됩니다(`tests/test_ontology.py`의 `HOLDOUT_PROPER_NOUNS`). `tests/test_golden_schema.py`가 두 세트를 검사합니다. `holdout_v1`(14문항)은 2026-10-31에 삭제했고 `runs.md`의 점수는 기록일 뿐입니다.

## 온톨로지

`ontology/`에는 골든 세트에서 뽑은 데이터 밖 정의가 있습니다(책 『온톨로지 따라하기』 4–5장). `--ontology off|ab|abc`(기본 `off`) 뒤에 연결돼 있습니다(위 `retrieve_knowledge` 참고).
- `model.yaml`: 개념(테이블, 입도, 식별자, 부모/구분자, `missing_means`/`zero_means`가 있는 속성)과 관계(`via`, 카디널리티, `derived`).
- `glossary.yaml`: 용어 → `canonical`(`Concept`, `Concept.column`, 또는 `{ref, values}`), `surface_forms`(같은 말) vs `confusable_with`(다른 것), 데이터에 없는 개념은 `in_data: false`, 모든 표현이 canonical 값 전부를 뜻하면 `bundle: true`(MMF = MMF 값 둘 다. 표현이 값 하나를 가리키는 분류 용어에는 쓰지 않음), `order_up: first|last`(`order`의 어느 끝이 "이상"인지. `order`가 있으면 필수).
- `rules.yaml`: `R-*` 규칙. `type` 조건/시점/예외/계산, `applies_to`, `when`/`then`, `overrides`, `depends_on`, `disclose`, `evidence`.
- `review_log.yaml`: 사람의 결정(merge/drop/derive/demote/keep_low_confidence), 출처 충돌, 일회성 읽기 전용 프로파일링 결과, 열린 질문, 검토 승인.
- 모든 항목에 `motivated_by`(골든 id, 추적용. 렌더링할 때는 뺌)가 있습니다. 정의는 문항별 사실이 아니라 일반 개념이어야 합니다. 문항 id, `examples` 밖의 골든 고유명사, 복사한 정답 SQL 조건은 `tests/test_ontology.py`에서 린트 실패입니다.
- PostgreSQL 사본: YAML이 정본이고 `knowledge` 스키마는 사본입니다. `scripts/load_ontology.py`가 `ontology/sql/knowledge_schema.sql`(멱등 DDL)을 적용하고 트랜잭션 하나로 모든 행을 지우고 다시 넣으므로 테이블을 손으로 고치지 마세요. 모든 참조(`canonical`, `applies_to`, 항목을 가리키는 `confusable_with`, `overrides`/`depends_on`)는 `item`으로의 FK라 잘못된 참조는 적재 전체를 롤백합니다. 항목이 아닌 `confusable_with` 대상은 `term_confusable.target_text`로 갑니다. `attribute_resolved`는 상속된 속성을 펼칩니다. `source_version`은 git 커밋·dirty 여부·파일 해시를 기록합니다. `financial_agent_reader`는 `item_motivation`(골든 id. 검색 채점 전용)을 뺀 전부를 SELECT할 수 있습니다. YAML을 고친 뒤에는 적재기를 다시 돌립니다.
- 프롬프트 층(항목이 프롬프트에 닿는 방식. 상수는 `src/portfolio_agent/retrieval.py`에 있고 아직 YAML에는 없음): **A** = 테이블 선택에 붙음(개념·속성·관계와 속성 전용 규칙 `LAYER_A_RULES`). **B** = 항상 포함(`LAYER_B_ITEMS`: 공통 규칙 여섯 개와 `as_of_date` 용어. 매 질문마다 토큰을 쓰니 작게 유지). **C** = 질문마다 검색으로 고름(나머지 용어·규칙 전부). 항목이 필요한지 알려면 무엇을 봐야 하는지로 정합니다: 테이블 → A, 아무것도 → B, 질문의 단어 → C.
- 임베딩·검색: `embed_ontology.py`가 용어마다(이름 + 같은 말 + 정의) 규칙마다(`when` + `applies_to` 대상 이름. `then` 제외) 문서 하나를 만들어 `portfolio_agent.embedding`으로 CLOVA 임베딩 v2(`bge-m3`, 1024차원, 코사인)를 호출하고 `knowledge.item_embedding`에 저장합니다(`item`으로의 FK 없음. 그래서 온톨로지를 다시 적재해도 벡터는 남고, `content_sha`가 바뀐 문서만 다시 임베딩). 검색(`portfolio_agent.retrieval`, 순수 함수, `tests/test_retrieval.py`)은 C층 항목을 pgvector 코사인, 키워드(이름·같은 말·`order_values`. 규칙은 대상의 점수를 물려받음), RRF 하이브리드로 매깁니다. 기본은 `hybrid`, k=15이고 골든으로만 정했습니다(2026-10-19 골든 recall@15 94.8%). 남은 누락은 대부분 상품 이름(`R-name-exact`, `product_name`)이라 사전이 아니라 값 연결이 필요합니다. k와 방식은 골든으로 튜닝합니다. `portfolio_agent.search`가 DB 쪽 조각(`load_index`, `vector_rank`, 캐시되는 `question_vector`, `search(question)` = 하이브리드 상위 `DEFAULT_K`, 임베딩 실패 시 키워드만, `motivation_gold`)을 들고 있고 에이전트·`eval_retrieval.py`·`render_context.py`가 함께 씁니다. 질문 임베딩은 `data/retrieval_cache/`(질문 sha256 키)에 캐시하고 결과는 `data/retrieval_runs/`에 갑니다(둘 다 gitignore).
- 렌더링(`portfolio_agent.context`, 책 7-2/7-3. 읽기 계정으로 `knowledge` 스키마를 읽는 `load_knowledge`(와 에이전트가 쓰는 `lru_cache` 래퍼 `get_knowledge`)를 빼면 순수 함수. `tests/test_context.py`): `build_context(kn, tables, c_ids)`는 `rules_text`(`{rules}`용: B층 공통 규칙, 그다음 C층 용어·규칙)와 `tables_text`(A층. 스키마 카드 뒤에 붙음: 입도·식별자·하위 유형이 있는 개념, 단위·NULL·0의 의미·메모가 있는 속성 줄, 테이블에 닿는 관계, 대상이 테이블에 있는 `LAYER_A_RULES`)를 돌려줍니다. C층 단계 순서: 선택된 테이블과 무관한 항목 제거(`is_related`. 용어와 한 테이블의 그 용어 버전을 함께 대상으로 하는 규칙, 예: `R-sale-fund` = `on_sale` + `FundClass.sale_yn`은 그 테이블에만 해당. `in_data: false` 용어는 항상 해당), 그다음 `depends_on` 대상을 한 단계 더함. `overrides` 대상은 끌어오지 않고 규칙 줄이 덮어쓰는 기본값을 인용합니다. 용어는 canonical 참조를 선택된 테이블로 한정해 실제 `table.column='value'`로 보여 줍니다. 뺀 것: `motivated_by`, `evidence`, `generalized_from`, `confusable_with`(보류). `render_context.py`는 테이블 선택 대신 골든 `tables`(구성종목이 있으면 `select_tables`처럼 `pref01n001` 추가)를 쓰고 토큰이 아니라 글자 수를 잽니다(로컬 HCX 토크나이저 없음). 2026-10-19: `generate_query` 프롬프트 중앙값 5,050 → 8,646자(1.68배, 최대 2.10배), C층 15 → 중앙값 10항목, recall 94.8% → 93.4%. 빠진 "정답" 항목은 일반화된 테이블 간 판매 규칙(C1-01/C1-06의 `R-sale-domestic-listed`/`-overseas-listed`)뿐이고 진짜 누락은 아닙니다. 일부 규칙 글이 아직 YAML 필드 이름(`zero_means`, `in_data false`, `glossary …`)을 쓰므로 나중에 `rules.yaml`에서 고칩니다. 출력은 `data/context_runs/`(gitignore)에 갑니다.
- 정의가 충돌할 때의 출처 우선순위: `golden/design.md` 정책·결정 > 실제 데이터 > `schema_catalog.json` > `questions_v2.yaml` 메모 > `docs/domain_rules_legacy.md`(옛 규칙. 에이전트는 읽지 않음). 정책이나 정답이 바뀌면 해당 온톨로지 항목과 `review_log.yaml`을 고칩니다.
- 2026-10-21에 SQL 생성을 도구 호출에서 구조화 출력(`QueryStep`)으로 바꿨습니다. 그 전 골든 실행(`base`, `base_v2`, `onto_ab`, `onto_abc`, `smoke`)은 생성 방식이 달라 기준선이 아니며 파일은 `data/golden_runs/archive/`에 있습니다. 현재 기준선은 `runs.md`에 있습니다. 평가 요약의 비용 표에는 `생성 실패(문항 수)` 행(`generation_errors`가 있는 문항)이 있습니다.
