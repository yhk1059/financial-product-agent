"""골든 문항마다 온톨로지 컨텍스트를 조립해 파일로 뽑고 프롬프트 증가량을 잰다 (에이전트·HCX 대화 호출 없음).

uv run python scripts/render_context.py [--label render_v1]

- 테이블 선택 대신 골든 정답 테이블(tables)을 쓴다. 테이블 선택이 늘 맞는다고 가정하므로 실제보다 작게 나올 수 있다.
  구성종목 테이블이 있으면 select_tables처럼 pref01n001을 더한다.
- C층은 하이브리드 상위 DEFAULT_K개 (eval_retrieval.py와 같은 순위), 질문 임베딩은 캐시를 쓴다.
- 프롬프트 길이는 글자 수로 잰다(HCX 토크나이저가 로컬에 없다). 온톨로지를 넣은 프롬프트는
  prompts.GENERATE_QUERY 틀에 {rules}=조립 규칙, {schema}=스키마 카드 + A층 블록을 넣어 만든다.
- 결과: data/context_runs/{label}/{문항id}.md (조립된 글), data/context_runs/{label}.json (수치)
"""

import argparse
import json
import statistics
from datetime import datetime

import yaml
from eval_retrieval import QUESTIONS_PATHS, load_gold, load_index, question_vectors, vector_rank

from portfolio_agent.config import PROJECT_ROOT, settings
from portfolio_agent.context import build_context, load_knowledge
from portfolio_agent.prompts import GENERATE_QUERY, NO_RULES, generate_prompt, rules_for
from portfolio_agent.retrieval import DEFAULT_K, FIXED_ITEMS, keyword_rank, rrf
from portfolio_agent.schema_catalog import schema_card

RUNS_DIR = PROJECT_ROOT / "data" / "context_runs"


def _stats(xs: list[int]) -> str:
    return f"중앙값 {statistics.median(xs):,.0f} / 최대 {max(xs):,}"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--label", default="render_v1")
    args = ap.parse_args()

    questions = yaml.safe_load(QUESTIONS_PATHS["golden"].read_text(encoding="utf-8"))
    term_forms, rule_targets, target_forms, embedded = load_index()
    candidates = embedded - FIXED_ITEMS
    gold = load_gold(candidates)
    vectors = question_vectors(questions)
    kn = load_knowledge()

    out_dir = RUNS_DIR / args.label
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for q in questions:
        tables = list(q["tables"])
        if "domestic_etf_holdings" in tables and "pref01n001" not in tables:  # graph.select_tables와 같은 보정
            tables.append("pref01n001")
        if rules_for(tables) != NO_RULES:
            raise SystemExit("domain_rules.md에 규칙이 있어 현재 프롬프트와 비교할 수 없다")
        vec = [i for i in vector_rank(vectors[q["id"]]) if i in candidates]
        kw = [i for i in keyword_rank(q["question"], term_forms, rule_targets, target_forms) if i in candidates]
        top = rrf(vec, kw)[:DEFAULT_K]
        ctx = build_context(kn, tables, top)

        card = schema_card(tables)
        before = generate_prompt(tables, card)
        schema = card + ("\n\n" + ctx.tables_text if ctx.tables_text else "")
        after = GENERATE_QUERY.format(top_k=settings.max_result_rows, rules=ctx.rules_text, schema=schema)

        g = gold.get(q["id"], set())
        c_all = ctx.layers["C"] + ctx.layers["expanded"]
        rows.append({
            "id": q["id"], "tables": tables,
            "chars": {"before": len(before), "after": len(after), "rules": len(ctx.rules_text),
                      "tables": len(ctx.tables_text)},
            "layers": ctx.layers,
            "expanded_gold": [i for i in ctx.layers["expanded"] if i in g],
            "recall": {"top": len(g & set(top)) / len(g) if g else None,
                       "filtered": len(g & set(ctx.layers["C"])) / len(g) if g else None,
                       "expanded": len(g & set(c_all)) / len(g) if g else None},
            "dropped_gold": [i for i in ctx.layers["dropped"] if i in g],
        })
        (out_dir / f"{q['id']}.md").write_text(
            f"# {q['id']} {q['question']}\n\n테이블: {', '.join(tables) or '(없음)'}\n"
            f"C층: {', '.join(ctx.layers['C'])}\n걸러짐: {', '.join(ctx.layers['dropped']) or '-'}\n"
            f"확장: {', '.join(ctx.layers['expanded']) or '-'}\n\n"
            f"## 도메인 규칙 ({{rules}})\n\n{ctx.rules_text}\n\n"
            f"## 스키마 카드 뒤에 붙는 A층\n\n{ctx.tables_text or '(없음)'}\n", encoding="utf-8")

    before = [r["chars"]["before"] for r in rows]
    after = [r["chars"]["after"] for r in rows]
    ratio = [a / b for a, b in zip(after, before)]
    print(f"\n골든 {len(rows)}문항 (테이블 = 골든 정답 테이블,"
          f" C층 = hybrid@{DEFAULT_K} → 테이블로 거름 → depends_on 1칸)\n")
    print(f"generate_query 프롬프트 글자 수  현재: {_stats(before)}")
    print(f"                              온톨로지: {_stats(after)}")
    print(f"증가 배율: 중앙값 {statistics.median(ratio):.2f}배 / 최대 {max(ratio):.2f}배")
    print(f"늘어난 몫 (중앙값): {{rules}} {statistics.median(r['chars']['rules'] for r in rows):,.0f}자,"
          f" A층 블록 {statistics.median(r['chars']['tables'] for r in rows):,.0f}자")

    n = {k: [len(r["layers"][k]) for r in rows] for k in ("A", "B", "C", "dropped", "expanded")}
    print("항목 수 (중앙값/최대): " + ", ".join(f"{k} {statistics.median(v):.0f}/{max(v)}" for k, v in n.items()))

    scored = [r for r in rows if r["recall"]["top"] is not None]
    for key, name in (("top", f"검색 상위 {DEFAULT_K}"), ("filtered", "테이블로 거른 뒤"), ("expanded", "확장 후")):
        rec = sum(r["recall"][key] for r in scored) / len(scored)
        full = sum(r["recall"][key] == 1 for r in scored)
        print(f"C층 재현율 {name}: {rec:.1%} (전부 맞힌 문항 {full}/{len(scored)})")
    dropped_gold = [(r["id"], i) for r in rows for i in r["dropped_gold"]]
    print(f"걸러진 항목 {sum(n['dropped'])}개, 그중 정답 {len(dropped_gold)}개"
          + (": " + ", ".join(f"{q}:{i}" for q, i in dropped_gold) if dropped_gold else ""))
    exp = [(r["id"], i) for r in rows for i in r["layers"]["expanded"]]
    exp_gold = [(r["id"], i) for r in rows for i in r["expanded_gold"]]
    print(f"확장으로 붙은 항목 {len(exp)}개, 그중 정답 {len(exp_gold)}개: "
          + (", ".join(f"{q}:{i}" for q, i in exp) or "-"))

    out = RUNS_DIR / f"{args.label}.json"
    out.write_text(json.dumps({"label": args.label, "created_at": datetime.now().isoformat(timespec="seconds"),
                               "k": DEFAULT_K, "rows": rows}, ensure_ascii=False, indent=1))
    print(f"\n→ {out.relative_to(PROJECT_ROOT)}, {out_dir.relative_to(PROJECT_ROOT)}/")


if __name__ == "__main__":
    main()
