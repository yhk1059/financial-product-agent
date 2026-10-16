"""평가 질문셋(tests/eval_questions.yaml)으로 에이전트 정답률을 측정한다.

uv run python scripts/run_eval.py [--check] [--only id1,id2]
결과는 data/eval_results.json 에 저장된다.
"""

import argparse
import decimal
import json
import re
import time
import uuid
from pathlib import Path

import yaml

from portfolio_agent import db
from portfolio_agent.cli import ask
from portfolio_agent.config import PROJECT_ROOT
from portfolio_agent.graph import build_chat_agent


def _variants(v) -> list[str]:
    """답변에서 찾을 문자열 후보 (숫자는 천 단위 콤마·반올림 표기 포함)."""
    if isinstance(v, int):
        return [str(v), f"{v:,}"]
    if isinstance(v, float | decimal.Decimal):
        f = float(v)
        return [f"{f:g}", f"{f:.2f}", f"{f:.1f}", str(round(f))]
    return [str(v)]


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s)


def judge(answer: str, expected: list) -> tuple[bool, list]:
    text = _norm(answer)
    missing = [v for v in expected if not any(_norm(c) in text for c in _variants(v))]
    return not missing, missing


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--only")
    args = parser.parse_args()

    questions = yaml.safe_load((PROJECT_ROOT / "tests" / "eval_questions.yaml").read_text())
    if args.only:
        ids = set(args.only.split(","))
        questions = [q for q in questions if q["id"] in ids]

    agent = build_chat_agent(check=args.check)
    results = []
    for q in questions:
        cols, rows = db.fetch(q["gold_sql"])
        idx = cols.index(q["expect_column"]) if "expect_column" in q else 0
        expected = [r[idx] for r in rows]
        start = time.time()
        try:
            answer = ask(agent, q["question"], str(uuid.uuid4()))
        except Exception as e:  # 평가 전체가 멈추지 않게 기록만 한다
            answer = f"ERROR: {type(e).__name__}: {e}"
        ok, missing = judge(answer, expected)
        results.append(
            {
                "id": q["id"],
                "pass": ok,
                "seconds": round(time.time() - start, 1),
                "expected": [str(v) for v in expected],
                "missing": [str(v) for v in missing],
                "answer": answer,
            }
        )
        print(f"{'PASS' if ok else 'FAIL'} {q['id']} ({results[-1]['seconds']}s) missing={missing}")

    passed = sum(r["pass"] for r in results)
    print(f"\n정답률: {passed}/{len(results)} ({100 * passed / len(results):.0f}%)")
    out = Path(PROJECT_ROOT / "data" / "eval_results.json")
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1))
    print(f"saved: {out}")


if __name__ == "__main__":
    main()
