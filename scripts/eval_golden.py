"""골든 데이터셋으로 에이전트를 실행·채점한다 (golden/design.md 5절).

uv run python scripts/eval_golden.py --label base [--check] [--ontology off|ab|abc] [--runs 3] [--only C1-01,C4-06]
uv run python scripts/eval_golden.py --rescore data/golden_runs/base_r1.jsonl [--write] [--label base_v2]
    # DB·LLM 없이 현재 골든 버전으로 재채점. --label을 주면 그 이름의 새 파일(_r1.jsonl, _summary.md)로 저장
uv run python scripts/eval_golden.py --compare base check                                 # 구성별 문항 비교표

출력: data/golden_runs/{label}_r{i}.jsonl (문항별 실행 기록·채점), {label}_summary.md (요약)
"""

import argparse
import json
import statistics
import subprocess
import uuid
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path

import psycopg
import yaml

from portfolio_agent import db
from portfolio_agent.config import ONTOLOGY_MODES, PROJECT_ROOT, settings
from portfolio_agent.golden_eval import grade, sql_sha, to_json
from portfolio_agent.sql_guard import SQLGuardError, validate

VERSION = "v2"
GOLDEN_DIR = PROJECT_ROOT / "golden"
RUNS_DIR = PROJECT_ROOT / "data" / "golden_runs"
CATEGORIES = ["C1", "C2", "C3", "C4", "C5", "C6"]


def load_golden() -> tuple[list[dict], dict]:
    questions = yaml.safe_load((GOLDEN_DIR / f"questions_{VERSION}.yaml").read_text())
    gold = json.loads((GOLDEN_DIR / f"gold_results_{VERSION}.json").read_text())
    stale = [q["id"] for q in questions if q["id"] not in gold or gold[q["id"]].get("sql_sha") != sql_sha(q)]
    if stale:
        raise SystemExit(
            f"정답 스냅샷이 질문 파일과 맞지 않습니다: {', '.join(stale)}\n"
            "먼저 `uv run python scripts/verify_golden.py`를 실행하세요."
        )
    return questions, gold


def git_rev() -> str:
    def git(*args):
        return subprocess.run(["git", *args], cwd=PROJECT_ROOT, capture_output=True, text=True).stdout.strip()

    rev = git("rev-parse", "--short", "HEAD") or "unknown"
    return rev + ("-dirty" if git("status", "--porcelain", "--untracked-files=no") else "")


def rerun(queries: list[dict]) -> None:
    """성공한 쿼리를 다시 실행해 잘리지 않은 결과(최대 LIMIT 200)를 붙인다 (5.4-1)."""
    for x in queries:
        if not x["ok"]:
            continue
        try:
            cols, rows = db.fetch(validate(x["sql"]))
        except (SQLGuardError, psycopg.Error) as e:
            x["rerun_error"] = str(e)
            continue
        x["rerun_columns"] = cols
        x["rerun_rows"] = [[to_json(v) for v in r] for r in rows]


def last_rows(queries: list[dict]) -> tuple[list | None, list | None]:
    done = [x for x in queries if x.get("ok") and "rerun_rows" in x]
    return (done[-1]["rerun_columns"], done[-1]["rerun_rows"]) if done else (None, None)


def run_one(agent, q: dict, gold: dict, meta: dict) -> dict:
    from portfolio_agent.cli import ask  # 재채점·비교만 할 때는 LLM 모듈을 불러오지 않는다

    record = {"id": q["id"], "category": q["category"], "difficulty": q["difficulty"], **meta}
    try:
        trace = asdict(ask(agent, q["question"], str(uuid.uuid4())))
    except Exception as e:  # 평가 전체가 멈추지 않게 기록만 한다
        trace = {"answer": "", "queries": [], "error": f"{type(e).__name__}: {e}"}
    rerun(trace["queries"])
    record.update(trace)
    record["agent_columns"], record["agent_rows"] = last_rows(record["queries"])
    record.update(grade(q, gold.get(q["id"]), record))
    return record


def print_row(r: dict) -> None:
    mark = "O" if r["correct"] else "X"
    detail = "" if r["correct"] else f"  {r['fail_reason']}"
    if r.get("exec_match") == "union":
        detail += "  (union)"
    print(f"{mark} {r['id']} [{r.get('exec_match') or 'refusal'}] {r.get('seconds', 0):5.1f}s{detail}")


def run(args) -> None:
    from portfolio_agent.graph import build_chat_agent

    questions, gold = load_golden()
    if args.only:
        ids = set(args.only.split(","))
        questions = [q for q in questions if q["id"] in ids]
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    agent = build_chat_agent(check=args.check, ontology=args.ontology)
    meta = {
        "label": args.label,
        "check": args.check,
        "ontology": args.ontology,
        "model": settings.model,
        "git_rev": git_rev(),
        "golden_version": VERSION,
    }
    rows = []
    for i in range(1, args.runs + 1):
        path = RUNS_DIR / f"{args.label}_r{i}.jsonl"
        print(f"── run {i}/{args.runs} → {path.name}")
        with path.open("w") as f:
            for q in questions:
                r = run_one(agent, q, gold, {**meta, "run": i})
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
                f.flush()
                print_row(r)
                rows.append(r)
    write_summary(args.label, rows, questions)


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def rescore(args) -> None:
    questions, gold = load_golden()
    qmap = {q["id"]: q for q in questions}
    path = Path(args.rescore)
    rows = read_jsonl(path)
    if args.label:  # 원본은 그대로 두고 새 이름으로 저장 (예: v1 기준선을 v2 기준으로 재채점)
        path = path.with_name(path.name.replace(f"{rows[0]['label']}_", f"{args.label}_", 1))
        for r in rows:
            r["label"] = args.label
    changed = []
    for r in rows:
        before = r.get("correct")
        r["golden_version"] = VERSION
        r.update(grade(qmap[r["id"]], gold.get(r["id"]), r))
        if before != r["correct"]:
            changed.append(f"{r['id']}:{'X→O' if r['correct'] else 'O→X'}")
    if args.write or args.label:
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
        print(f"파일 갱신: {path}")
        write_summary(rows[0]["label"], rows, [qmap[r["id"]] for r in rows])
    else:
        print(summary(rows, [qmap[r["id"]] for r in rows]))
    print("판정 변경:", ", ".join(changed) if changed else "없음")


# ───────────────────────── 요약 ─────────────────────────
def _rate(hits: int, n: int) -> str:
    return f"{hits}/{n} ({100 * hits / n:.0f}%)" if n else "-"


def _median(xs: list) -> str:
    xs = [x for x in xs if x is not None]
    return f"{statistics.median(xs):g}" if xs else "-"


def summary(rows: list[dict], questions: list[dict]) -> str:
    qmap = {q["id"]: q for q in questions}
    runs = sorted({r.get("run", 1) for r in rows})
    sql_rows = [r for r in rows if r["category"] != "C6"]
    with_mention = [r for r in rows if qmap[r["id"]].get("must_mention")]
    judged = [r for r in sql_rows if r.get("answer_ok") is not None]
    lines = [
        f"# 골든 {VERSION} 채점 요약: {rows[0].get('label', '-')}",
        "",
        f"- 모델 `{rows[0].get('model')}`, git `{rows[0].get('git_rev')}`, check_query "
        f"{'켬' if rows[0].get('check') else '끔'}, 온톨로지 `{rows[0].get('ontology', 'off')}`, "
        f"실행 {len(runs)}회 × {len(rows) // len(runs)}문항",
        f"- **문항 정답률: {_rate(sum(r['correct'] for r in rows), len(rows))}**",
        "",
        "## 축별",
        "| 축 | 결과 | 비고 |",
        "|---|---|---|",
        f"| 실행 정확도 (C1~C5) | {_rate(sum(r['exec_match'] in ('last', 'earlier', 'union') for r in sql_rows), len(sql_rows))} "
        f"| earlier {sum(r['exec_match'] == 'earlier' for r in sql_rows)}건, union {sum(r['exec_match'] == 'union' for r in sql_rows)}건, "
        f"비율 변형 {sum(bool(r.get('scale_variant')) for r in sql_rows)}건 |",
        f"| 답변 정확도 | {_rate(sum(r['answer_ok'] for r in judged), len(judged))} | 키 값 10개 초과 문항 제외 |",
        f"| 필수 언급 | {_rate(sum(not r['mentions_missing'] for r in with_mention), len(with_mention))} | |",
        f"| 거절 (C6) | {_rate(sum(r['correct'] for r in rows if r['category'] == 'C6'), len(rows) - len(sql_rows))} | |",
        f"| 충실도 (참고) | {_rate(sum(not r['ungrounded_numbers'] for r in rows), len(rows))} | 근거 없는 숫자가 없는 답변 비율 |",
        "",
        "## 분류·난이도별 정답률",
        "| 구분 | 정답률 |",
        "|---|---|",
    ]
    for key, values in (("category", CATEGORIES), ("difficulty", ["easy", "medium", "hard"])):
        for v in values:
            group = [r for r in rows if r[key] == v]
            if group:
                lines.append(f"| {v} | {_rate(sum(r['correct'] for r in group), len(group))} |")
    reasons = Counter(r["fail_reason"] for r in rows if not r["correct"])
    lines += ["", "## 실패 원인", "| 원인 | 건수 |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in reasons.most_common()] or ["| - | 0 |"]

    tokens = [r.get("tokens") for r in rows]
    inputs = [r.get("input_tokens") for r in rows]
    max_inputs = [r.get("max_input_tokens") for r in rows if r.get("max_input_tokens") is not None]
    lines += [
        "",
        "## 비용",
        "| 항목 | 합계 | 문항 중앙값 |",
        "|---|---|---|",
        f"| 시간(초) | {sum(r.get('seconds', 0) for r in rows):.1f} | {_median([r.get('seconds') for r in rows])} |",
        f"| 429 대기(초) | {sum(r.get('retry_wait_seconds', 0) for r in rows):.0f} | "
        f"{_median([r.get('retry_wait_seconds') for r in rows])} |",
        f"| LLM 호출 | {sum(r.get('llm_calls', 0) for r in rows)} | {_median([r.get('llm_calls') for r in rows])} |",
        f"| 쿼리 실행 | {sum(len(r['queries']) for r in rows)} | {_median([len(r['queries']) for r in rows])} |",
        f"| 토큰 | {sum(t for t in tokens if t) if any(tokens) else '-'} | {_median(tokens)} |",
        f"| input 토큰 | {sum(t for t in inputs if t) if any(inputs) else '-'} | {_median(inputs)} |",
        f"| 호출당 최대 input 토큰 | {'최대 ' + str(max(max_inputs)) if max_inputs else '-'} | {_median(max_inputs)} |",
    ]

    by_id: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_id[r["id"]].append(r)
    lines += ["", "## 문항별", "| ID | 정답 | 실행 | 답변 | 언급 누락 | 실패 원인 | 빠진 컬럼 | 추가 컬럼 | 근거 없는 숫자 |",
              "|---|---|---|---|---|---|---|---|---|"]
    for qid, rs in by_id.items():
        hits = sum(r["correct"] for r in rs)
        last = rs[-1]
        ok = ("O" if hits else "X") if len(rs) == 1 else f"{hits}/{len(rs)}"
        answer = {True: "O", False: "X", None: "-"}[last.get("answer_ok")]
        cells = [
            qid,
            ok,
            ", ".join(sorted({str(r.get("exec_match") or "-") for r in rs})),
            answer,
            ", ".join(last["mentions_missing"]) or "-",
            ", ".join(sorted({r["fail_reason"] for r in rs if r["fail_reason"]})) or "-",
            ", ".join(last.get("columns_missing") or []) or "-",
            ", ".join(last.get("columns_extra") or []) or "-",
            ", ".join(f"{n:g}" for n in last["ungrounded_numbers"][:5]) or "-",
        ]
        lines.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
    if len(runs) > 1:
        stable = sum(all(r["correct"] for r in rs) for rs in by_id.values())
        unstable = sum(0 < sum(r["correct"] for r in rs) < len(rs) for rs in by_id.values())
        lines += ["", f"**일관성:** 안정 정답 {stable}문항, 불안정 {unstable}문항 ({len(runs)}회 기준)"]
    return "\n".join(lines) + "\n"


def write_summary(label: str, rows: list[dict], questions: list[dict]) -> None:
    text = summary(rows, questions)
    path = RUNS_DIR / f"{label}_summary.md"
    path.write_text(text)
    print("\n" + text)
    print(f"요약 저장: {path}")


def compare(labels: list[str]) -> None:
    """구성별로 문항 정답 여부(여러 회면 정답 횟수)를 나란히 놓는다."""
    questions, _ = load_golden()
    tables = {}
    for label in labels:
        files = sorted(RUNS_DIR.glob(f"{label}_r*.jsonl"))
        if not files:
            raise SystemExit(f"{label}: 실행 결과가 없습니다 ({RUNS_DIR}/{label}_r*.jsonl)")
        hits: dict[str, list[bool]] = defaultdict(list)
        for f in files:
            for r in read_jsonl(f):
                hits[r["id"]].append(r["correct"])
        tables[label] = hits
    lines = ["| 문항 | 분류 | " + " | ".join(labels) + " |", "|---|---|" + "---|" * len(labels)]
    for q in questions:
        cells = []
        for label in labels:
            h = tables[label].get(q["id"])
            cells.append("-" if not h else ("O" if h[0] else "X") if len(h) == 1 else f"{sum(h)}/{len(h)}")
        lines.append(f"| {q['id']} | {q['category']} | " + " | ".join(cells) + " |")
    totals = []
    for label in labels:
        flat = [x for h in tables[label].values() for x in h]
        totals.append(_rate(sum(flat), len(flat)))
    lines.append("| **정답률** | | " + " | ".join(totals) + " |")
    print("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser(description="골든 데이터셋 채점")
    parser.add_argument("--label", help="구성 이름 (결과 파일 접두어, 기본 base). --rescore와 쓰면 새 이름으로 저장")
    parser.add_argument("--check", action="store_true", help="check_query 노드 사용")
    parser.add_argument("--ontology", choices=ONTOLOGY_MODES, default="off",
                        help="온톨로지 지식 모드 (off: 넣지 않음, ab: 테이블·공통, abc: +질문 검색)")
    parser.add_argument("--runs", type=int, default=1, help="같은 구성 반복 횟수 (일관성 측정)")
    parser.add_argument("--only", help="쉼표로 구분한 문항 ID")
    parser.add_argument("--rescore", help="저장된 JSONL을 DB·LLM 없이 다시 채점")
    parser.add_argument("--write", action="store_true", help="--rescore 결과를 파일에 반영")
    parser.add_argument("--compare", nargs="+", metavar="LABEL", help="구성별 문항 비교표")
    args = parser.parse_args()
    if args.rescore:
        rescore(args)
    elif args.compare:
        compare(args.compare)
    else:
        args.label = args.label or "base"
        run(args)


if __name__ == "__main__":
    main()
