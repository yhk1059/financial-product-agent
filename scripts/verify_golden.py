"""골든 데이터셋 정답 SQL을 실행·검사하고 정답 스냅샷과 검토표를 만든다.

uv run python scripts/verify_golden.py [--version v2]
uv run python scripts/verify_golden.py --questions golden/holdout_v2.yaml --out golden/gold_results_holdout_v2.json

출력:
- golden/gold_results_{version}.json : 문항별 정답 결과 스냅샷, 선택지별 결과, 경고
- golden/review_sheet.md             : 사용자 검토용 요약표 (--questions 없이 실행할 때만)
- --questions/--out                  : 다른 문항 파일(예: 홀드아웃)의 정답 스냅샷을 지정한 경로에 쓴다
"""

import argparse
import decimal
import json
from datetime import date, datetime
from pathlib import Path

import sqlglot
import yaml
from sqlglot import exp

from financial_product_agent import db
from financial_product_agent.config import GOLDEN_DIR, settings
from financial_product_agent.golden_eval import sql_sha
from financial_product_agent.sql_guard import SQLGuardError, validate


def _json(v):
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, date | datetime):
        return v.isoformat()
    return v


def run(sql: str) -> tuple[list[str], list[list]]:
    cols, rows = db.fetch(sql)
    return cols, [[_json(v) for v in r] for r in rows]


def _with_limit(sql: str, n: int) -> str | None:
    tree = sqlglot.parse_one(sql, read="postgres")
    if not isinstance(tree, exp.Select) or not tree.args.get("limit"):
        return None
    return tree.limit(n).sql(dialect="postgres")


def boundary_tie(sql: str, key_columns: list[str]) -> list | None:
    """Top-N 경계 동률 검사: N+1번째 행의 정렬값이 N번째와 같으면 그 행을 반환."""
    tree = sqlglot.parse_one(sql, read="postgres")
    limit = tree.args.get("limit")
    if limit is None:
        return None
    n = int(limit.expression.name)
    cols, rows = run(_with_limit(sql, n + 1))
    if len(rows) <= n:
        return None
    value_idx = [i for i, c in enumerate(cols) if c not in key_columns]
    nth, extra = rows[n - 1], rows[n]
    if value_idx and all(nth[i] == extra[i] for i in value_idx):
        return extra
    return None


def verify(q: dict) -> dict:
    out = {"id": q["id"], "sql_sha": sql_sha(q), "warnings": []}
    if q["answer_type"] == "refusal":
        return out

    sql = q["gold_sql"]
    try:
        validate(sql)
    except SQLGuardError as e:
        out["warnings"].append(f"sql_guard 거부: {e}")

    cols, rows = run(sql)
    out["columns"], out["rows"], out["row_count"] = cols, rows, len(rows)

    alts = [a for v in q.get("key_alternatives", {}).values() for a in v]
    missing = [k for k in [*q.get("key_columns", []), *alts] if k not in cols]
    if missing:
        out["warnings"].append(f"key_columns·key_alternatives가 결과에 없음: {missing}")
    if not rows:
        out["warnings"].append("결과 0건")
    if q["answer_type"] in ("count", "scalar") and len(rows) != 1:
        out["warnings"].append(f"{q['answer_type']} 문항인데 {len(rows)}행")
    if len(rows) > settings.default_limit:
        out["warnings"].append(
            f"결과 {len(rows)}행: 에이전트는 LIMIT {settings.default_limit}로 잘린 결과만 보게 됨"
        )
    if q.get("order_matters"):
        tie = boundary_tie(sql, q.get("key_columns", []))
        if tie:
            out["warnings"].append(f"Top-N 경계 동률 있음 → accept_alternatives 후보: {tie}")
            out["tie_candidate"] = tie

    if "decision_needed" in q:
        out["options"] = []
        for opt in q["decision_needed"]["options"]:
            _, opt_rows = run(opt["sql"])
            out["options"].append({"label": opt["label"], "rows": opt_rows})
    return out


def _summary(r: dict, limit: int = 3) -> str:
    if "rows" not in r:
        return "(거절이 정답)"
    rows = r["rows"]
    if len(rows) == 1 and len(rows[0]) == 1:
        return str(rows[0][0])
    shown = "; ".join(", ".join(str(v) for v in row) for row in rows[:limit])
    more = f" … 외 {len(rows) - limit}건" if len(rows) > limit else ""
    return f"{len(rows)}행: {shown}{more}"


def _cell(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def write_review_sheet(questions: list[dict], results: dict[str, dict], version: str) -> None:
    lines = [
        f"# 골든 데이터셋 {version} 검토표",
        "",
        "`scripts/verify_golden.py`가 생성한 파일입니다. 직접 고치지 말고 `questions_*.yaml`을 고친 뒤 다시 생성하세요.",
        "",
        "| ID | 난이도 | 질문 | 해석 | 정답 요약 | 경고 |",
        "|---|---|---|---|---|---|",
    ]
    for q in questions:
        r = results[q["id"]]
        lines.append(
            "| "
            + " | ".join(
                _cell(x)
                for x in (
                    q["id"],
                    q["difficulty"],
                    q["question"],
                    q["interpretation"],
                    _summary(r),
                    "<br>".join(r["warnings"]) or "-",
                )
            )
            + " |"
        )

    decisions = [q for q in questions if "decision_needed" in q]
    pending = sum("resolved" not in q["decision_needed"] for q in decisions)
    lines += ["", f"## 해석 쟁점 ({len(decisions)}개, 미결정 {pending}개)", ""]
    for q in decisions:
        r = results[q["id"]]
        resolved = q["decision_needed"].get("resolved")
        lines.append(f"### {q['id']}: {q['question']}")
        lines.append(f"- **쟁점:** {q['decision_needed']['question']}")
        lines.append(f"- **결정:** {resolved}" if resolved else "- **결정:** 미결정")
        for opt in r["options"]:
            value = opt["rows"][0][0] if len(opt["rows"]) == 1 and len(opt["rows"][0]) == 1 else opt["rows"]
            lines.append(f"- {opt['label']}: **{value}**")
        lines.append("")

    (GOLDEN_DIR / "review_sheet.md").write_text("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default="v2")
    parser.add_argument("--questions", type=Path, help="문항 YAML 경로 (기본: golden/questions_{version}.yaml)")
    parser.add_argument("--out", type=Path, help="정답 스냅샷 경로 (기본: golden/gold_results_{version}.json)")
    args = parser.parse_args()

    questions_path = args.questions or GOLDEN_DIR / f"questions_{args.version}.yaml"
    questions = yaml.safe_load(questions_path.read_text())
    results = {}
    for q in questions:
        results[q["id"]] = verify(q)
        r = results[q["id"]]
        flag = "WARN" if r["warnings"] else "OK  "
        print(f"{flag} {q['id']}: {_summary(r)}")
        for w in r["warnings"]:
            print(f"       - {w}")

    out = args.out or GOLDEN_DIR / f"gold_results_{args.version}.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1))
    written = [out.name]
    if args.questions is None:  # 검토표는 기본 골든 문항 파일로 실행할 때만 다시 쓴다
        write_review_sheet(questions, results, args.version)
        written.append("review_sheet.md")
    n_warn = sum(bool(r["warnings"]) for r in results.values())
    print(f"\n{len(questions)}문항, 경고 {n_warn}건 → {', '.join(written)}")


if __name__ == "__main__":
    main()
