"""온톨로지 C층 검색을 골든 문항으로 채점한다 (에이전트·HCX 대화 호출 없음).

uv run python scripts/eval_retrieval.py [--label 이름] [--ks 5,10,15,20] [--detail-k 15]

- 정답: knowledge.item_motivation(골든 id → 항목). C층 용어·규칙만 남긴다. A·B층 항목은 항상 들어가므로 뺀다.
- 방식: vector(pgvector 코사인), keyword(이름·같은 말 일치, 규칙은 적용 대상에서 물려받음), hybrid(RRF).
  순위에서도 A·B층 항목을 빼고 상위 k를 자른다 (렌더링 때 어차피 따로 들어가므로).
- 질문 임베딩은 data/retrieval_cache/에 캐시한다(portfolio_agent.search, 에이전트와 같은 캐시).
  결과는 data/retrieval_runs/{label}.json.
- 기본 방식·k는 retrieval.DEFAULT_METHOD/DEFAULT_K. 이 값은 골든으로만 정한다.
"""

import argparse
import json
import time
from datetime import datetime

import yaml

from portfolio_agent.config import DATA_DIR, GOLDEN_DIR, PROJECT_ROOT
from portfolio_agent.embedding import EMBED_MODEL, VARIANT
from portfolio_agent.retrieval import DEFAULT_K, DEFAULT_METHOD, FIXED_ITEMS, keyword_rank, recall_at, rrf
from portfolio_agent.search import is_cached, load_index, motivation_gold, question_vector, vector_rank

QUESTIONS_PATHS = {
    "golden": GOLDEN_DIR / "questions_v2.yaml",
}
RUNS_DIR = DATA_DIR / "retrieval_runs"
METHODS = ("vector", "keyword", "hybrid")


def question_vectors(questions: list[dict]) -> dict[str, list[float]]:
    out, new = {}, 0
    for q in questions:
        if not is_cached(q["question"]):
            if new:
                time.sleep(1.1)
            new += 1
        out[q["id"]] = question_vector(q["question"])
    print(f"질문 임베딩: 새로 {new}, 캐시 {len(questions) - new}")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--set", choices=tuple(QUESTIONS_PATHS), default="golden", dest="qset")
    ap.add_argument("--label", help="결과 파일 이름 (기본: --set 값)")
    ap.add_argument("--ks", default="5,10,15,20")
    ap.add_argument("--detail-k", type=int, default=DEFAULT_K, help="문항별 놓친 항목을 보여 줄 k")
    args = ap.parse_args()
    ks = sorted({int(k) for k in args.ks.split(",")} | {DEFAULT_K})
    label = args.label or args.qset

    questions = yaml.safe_load(QUESTIONS_PATHS[args.qset].read_text(encoding="utf-8"))
    term_forms, rule_targets, target_forms, embedded = load_index()
    candidates = embedded - FIXED_ITEMS
    gold = motivation_gold(candidates)
    vectors = question_vectors(questions)

    results = []
    for q in questions:
        vec = [i for i in vector_rank(vectors[q["id"]]) if i in candidates]
        kw = [i for i in keyword_rank(q["question"], term_forms, rule_targets, target_forms) if i in candidates]
        rankings = {"vector": vec, "keyword": kw, "hybrid": rrf(vec, kw)}
        g = gold.get(q["id"], set())
        results.append({
            "id": q["id"], "question": q["question"], "gold": sorted(g),
            "rankings": rankings,
            "recall": {m: {k: recall_at(r, g, k) for k in ks} for m, r in rankings.items()},
            "gold_rank": {m: {i: (r.index(i) + 1 if i in r else None) for i in sorted(g)}
                          for m, r in rankings.items()},
        })

    scored = [r for r in results if r["gold"]]
    print(f"\n{args.qset} {len(results)}문항 중 C층 정답이 있는 {len(scored)}문항, 후보 {len(candidates)}개"
          f" (고정 제외 {len(FIXED_ITEMS & embedded)}개), 정답 합계 {sum(len(r['gold']) for r in scored)}개\n")
    head = "방식      " + "".join(f"  재현율@{k:<3}" for k in ks) + "".join(f"  전부@{k:<3}" for k in ks)
    print(head)
    summary = {}
    for m in METHODS:
        rec = {k: sum(r["recall"][m][k] for r in scored) / len(scored) for k in ks}
        full = {k: sum(r["recall"][m][k] == 1 for r in scored) for k in ks}
        summary[m] = {"recall": rec, "full_hit": full}
        print(f"{m:<10}" + "".join(f"  {rec[k]:>9.1%}" for k in ks) + "".join(f"  {full[k]:>6}" for k in ks))
    d = summary[DEFAULT_METHOD]
    print(f"\n기본값 {DEFAULT_METHOD}@{DEFAULT_K}: 재현율 {d['recall'][DEFAULT_K]:.1%},"
          f" 전부 맞힌 문항 {d['full_hit'][DEFAULT_K]}/{len(scored)}")
    kw_len = sorted(len(r["rankings"]["keyword"]) for r in results)
    print(f"\nkeyword가 고른 항목 수: 중앙값 {kw_len[len(kw_len) // 2]}, 최대 {kw_len[-1]}, 0개인 문항 {kw_len.count(0)}")

    dk = args.detail_k
    print(f"\n문항별 놓친 정답 (@{dk}, 숫자 = 순위, - = 순위에 없음)")
    for r in scored:
        misses = {m: [f"{i}({r['gold_rank'][m][i] or '-'})" for i in r["gold"]
                      if (r["gold_rank"][m][i] or 10**9) > dk] for m in METHODS}
        if any(misses.values()):
            print(f"{r['id']} 정답 {len(r['gold'])}개")
            for m in METHODS:
                if misses[m]:
                    print(f"   {m:<8} {', '.join(misses[m])}")

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    out = RUNS_DIR / f"{label}.json"
    out.write_text(json.dumps({
        "label": label, "set": args.qset, "created_at": datetime.now().isoformat(timespec="seconds"),
        "model": EMBED_MODEL, "variant": VARIANT, "ks": ks, "summary": summary, "results": results,
    }, ensure_ascii=False, indent=1))
    print(f"\n→ {out.relative_to(PROJECT_ROOT)}")


if __name__ == "__main__":
    main()
