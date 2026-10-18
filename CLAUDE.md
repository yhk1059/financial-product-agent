# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Natural-language Q&A agent over five Korean financial-product tables in PostgreSQL (`public` schema). It routes a question to tables, writes SQL, runs it read-only, and answers. Built with LangGraph and HyperCLOVA X (`HCX-007` via `langchain-naver`'s `ChatClovaX`). Code comments, prompts, and docs are in Korean. Keep it that way.

Tables: `pref01n001` (domestic ETF/ETN), `pref02n001` (overseas ETF/ETN), `prfd01n001` (funds), `prbd01n001` (bonds), `domestic_etf_holdings` (domestic ETF constituents, joins to `pref01n001` via `item_id = pd_itm_no`).

## Commands

Managed with `uv` (Python ≥3.13). `.env` must provide `PG*` (read-only account), `CLOVA_STUDIO_API_KEY`, `CLOVASTUDIO_MODEL`, and `LANGSMITH_*`. `KNOWLEDGE_PGUSER`/`KNOWLEDGE_PGPASSWORD` (`knowledge_writer`, writes only the `knowledge` schema) are needed only for `load_ontology.py`. Don't use `METADATA_PGUSER` or `SEMANTIC_DATABASE_URL`; the `agent_metadata`, `ontology`, and `semantic` schemas belong to other work.

```bash
uv sync
uv run portfolio-agent                                   # interactive chat (/new = new thread, exit = quit)
uv run portfolio-agent -q "질문" --show-sql [--check]    # one-shot; --check enables the check_query node
uv run pytest                                            # all tests
uv run pytest tests/test_sql_guard.py                    # pure unit tests, no DB/LLM
uv run pytest -m "not db"                                # skip tests that need PostgreSQL
uv run pytest tests/test_sql_guard.py::test_allows_select
uv run python scripts/build_schema_cache.py              # regenerate data/schema_catalog.json after DB data changes
uv run python scripts/verify_golden.py                   # rerun gold SQL → golden/gold_results_v2.json, review_sheet.md
uv run python scripts/verify_golden.py --questions golden/holdout_v1.yaml --out golden/gold_results_holdout_v1.json   # holdout gold (no review_sheet)
uv run pytest tests/test_ontology.py                     # ontology YAML structure/existence/leak/coverage checks, no DB/LLM
uv run python scripts/load_ontology.py [--dry-run]       # reload ontology/*.yaml into the knowledge schema (--dry-run: row counts only, no DB)
uv run python scripts/eval_golden.py --label base [--check] [--runs 3] [--only C1-01,C4-06]   # score agent on golden v2
uv run python scripts/eval_golden.py --rescore data/golden_runs/base_r1.jsonl [--write] [--label base_v2]   # rescore without DB/LLM against the current golden version
uv run python scripts/eval_golden.py --compare base check                                    # per-question table across labels
```

Pytest markers: `db` (needs PostgreSQL), `llm` (calls HyperCLOVA X).

## Architecture

Graph (`src/portfolio_agent/graph.py`):

```
START → select_tables → generate_query ─┬─(no tool call)→ END
                              ↑          └─(tool call)→ [check_query | count_attempt] → run_query ─┘
```

- **select_tables**: structured output (`TableSelection`, `json_schema`), restricted to a `Literal` of `ALLOWED_TABLES`. On failure it falls back to all tables. It always adds `pref01n001` when `domestic_etf_holdings` is selected.
- **generate_query**: the system prompt holds the schema cards and domain rules for the selected tables only. The LLM is bound to the `run_query` tool. After `settings.max_query_attempts` (3) runs, `give_up` mode turns any further tool call into a plain answer. Tools stay bound even then, because the CLOVA API rejects tool messages in history when no tools are defined.
- **check_query** (optional, off by default because it costs extra LLM calls): it re-asks for the SQL via structured output and replaces the tool call's args. It returns an `AIMessage` with the **same id**, so `MessagesState` replaces the original message.
- **run_query** (`tools.py`): `sql_guard.validate` → `db.fetch` → formatted text. Errors go back to the LLM as `"Error: ..."` strings, not exceptions. Output is truncated to `max_result_rows` and `max_result_chars`.
- Multi-turn chat uses `InMemorySaver` keyed by `thread_id` (`build_chat_agent`). `cli.ask` is shared by the CLI and `eval_golden.py`. It returns an `AskTrace` (answer, selected tables, executed SQL with tool output, LLM calls/tokens via a callback, time, 429 wait from `llm.retry_wait_seconds()`); the CLI prints `.answer`.

Supporting modules:
- `config.py`: loads `.env` from the project root, maps `CLOVA_STUDIO_API_KEY` → `CLOVASTUDIO_API_KEY`, and defines `ALLOWED_TABLES` and `Settings` (timeouts, limits). Adding a table means updating `ALLOWED_TABLES`, then regenerating the schema cache, then adding a `domain_rules.md` section.
- `sql_guard.py`: sqlglot (postgres dialect) allows only a single SELECT/WITH/set-op on allowed `public` tables. It blocks DML/DDL, `SELECT INTO`, locks, and `pg_*`/`dblink`/`lo_*`/`set_config`-style functions. It quotes digit-leading aliases, adds `LIMIT 200` when there is none, and re-emits SQL with `identify=True`. Defense in depth: `db.connect()` also forces a read-only transaction, a 15s `statement_timeout`, and `search_path=public`.
- `schema_catalog.py`: profiles each table (comments, types, null %, distinct counts, category value lists ≤20, ranges) into `data/schema_catalog.json`. `table_overview()` feeds table selection, and `schema_card(tables)` feeds generation and checking.
- `prompts.py` + `domain_rules.md`: the rules file is split on `## ` headings. The key is the heading's **first word**, which must be exactly a table name or `공통` (common). Only `공통` plus the selected tables' sections go into prompts.
- `llm.py`: `get_llm()` is `lru_cache`d, `temperature=0`, `thinking={"effort": "none"}`. `invoke_with_retry` handles 429s with 15s/30s/… waits.

## HyperCLOVA X (HCX-007) constraints

These drive most design choices. Don't "simplify" them back to the standard LangGraph SQL-agent tutorial:
- Tool calling and structured output only work with `thinking={"effort": "none"}`.
- `tool_choice` (forced tool calls) is unsupported. That is why table selection and query checking use structured output instead of forced tool calls, and why schemas are injected into prompts rather than fetched via tools.
- Rate limit: 60 requests/min and 60k tokens/min. Keep prompts scoped to the selected tables. The OpenAI client's short backoff isn't enough, which is why `invoke_with_retry` exists.

## Evaluation

The golden dataset lives in `golden/`. `design.md` is the source of truth for interpretation policies (P-*), item format (§4), scoring (§5), and the decision log (§8). `questions_v2.yaml` (current; v1 kept for history) holds 30 items (C1–C6) with `gold_sql`. `verify_golden.py` writes `gold_results_v2.json`, including a per-item `sql_sha`. The `VERSION` constant in `eval_golden.py` and `--version` default in `verify_golden.py` select the version. `eval_golden.py` refuses to run if any hash is stale, so rerun `verify_golden.py` after editing `gold_sql`.

Scoring (`src/portfolio_agent/golden_eval.py`, pure functions, unit-tested in `tests/test_golden_eval.py`) is rule-based, with no LLM:
- **Execution accuracy:** successful agent SQL is re-run (untruncated, guard LIMIT 200) and compared to gold by value, not by column name. Types: `count`/`scalar` mean one row containing the value, `list` is a set or ordered top-N, `table` is a multiset of key tuples. `label_columns` (literals invented by gold SQL, e.g. C5-01 `market`) match by one-to-one correspondence. The last successful query is tried first, then earlier ones (`exec_match: earlier`). For `table`, results split across several queries count as `union`. `key_alternatives` (e.g. `pd_abrv_nm` → `pd_nm`, `pd_itm_no`) let the same product match via another identifier column, in both execution and answer checks.
- **Numbers:** equal if they match when rounded to 2 dp, or when rounded to the agent's own decimal places. Integers must match exactly. `scalar` also accepts ×100/÷100 (`scale_variant`).
- **Answer text:** `answer_ok` means gold key values (≤10) appear in the answer. `mention_keywords` (`'a|b'` synonyms, aligned with `must_mention`) and C6 `forbidden_patterns` (regex) are also checked. Mention keywords must not appear in the question itself (schema test), and C6 requires an item-specific refusal reason, not just a generic "can't find". `ungrounded_numbers` is reference-only. So are `columns_missing`/`columns_extra`: real schema columns (aliases dropped via `schema_catalog.json`) referenced by gold SQL vs the agent's last successful SQL, used to diagnose `wrong_result`.
- **`correct`:** C1–C5 require exec match, `answer_ok` not false, and all mentions. C6 requires refusal keywords and no forbidden hits. Failures get one `fail_reason`.

Outputs go to `data/golden_runs/` (gitignored). When changing prompts or `domain_rules.md`, rerun the eval (or `--only` the affected ids). After changing scoring rules, use `--rescore` instead of rerunning the agent.

**Holdout:** `golden/holdout_v1.yaml` has 14 items (H-01…H-14) in the same format plus `tests_knowledge`. They ask the same knowledge as golden in different contexts. They were frozen before the ontology was written, so don't edit them; bump to `holdout_v2` instead.

## Ontology

`ontology/` holds data-external definitions extracted from the golden set (book 『온톨로지 따라하기』 ch. 4–5). It is not wired into the agent yet (no prompt rendering).
- `model.yaml`: concepts (table, grain, identity, parent/discriminator, attributes with `missing_means`/`zero_means`) and relations (`via`, cardinality, `derived`).
- `glossary.yaml`: term → `canonical` (`Concept`, `Concept.column`, or `{ref, values}`), `surface_forms` (synonyms) vs `confusable_with` (different things), `in_data: false` for concepts the data lacks.
- `rules.yaml`: `R-*` rules with `type` 조건/시점/예외/계산, `applies_to`, `when`/`then`, `overrides`, `depends_on`, `disclose`, `evidence`.
- `review_log.yaml`: human decisions (merge/drop/derive/demote/keep_low_confidence), source conflicts, one-off read-only profiling results, open questions, review approvals.
- Every item has `motivated_by` (golden ids, trace only; drop it when rendering). Definitions must be general concepts, not per-question facts; question ids, golden proper nouns outside `examples`, holdout proper nouns, and copied gold SQL predicates are lint failures in `tests/test_ontology.py`. Its `HOLDOUT_COVERAGE` maps each holdout `tests_knowledge` line to the items that resolve it.
- PostgreSQL copy: YAML is the source of truth; the `knowledge` schema is a copy. `scripts/load_ontology.py` applies `ontology/sql/knowledge_schema.sql` (idempotent DDL) and, in one transaction, deletes and reinserts every row, so never edit the tables by hand. All references (`canonical`, `applies_to`, `confusable_with` when it names an item, `overrides`/`depends_on`) are FKs to `item`, so a bad reference rolls back the whole load. `confusable_with` targets that aren't items go to `term_confusable.target_text`. `attribute_resolved` expands inherited attributes. `source_version` records the git commit, dirty flag, and file hashes. `financial_agent_reader` can SELECT everything except `item_motivation` (golden ids; for retrieval scoring only). Rerun the loader after editing the YAML.
- Source priority when definitions conflict: `golden/design.md` policies/decisions > actual data > `schema_catalog.json` > `questions_v2.yaml` notes > `domain_rules_legacy.md`. When policies or gold change, update the matching ontology items and `review_log.yaml`.
