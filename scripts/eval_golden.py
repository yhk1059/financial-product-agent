"""골든 데이터셋으로 에이전트를 실행·채점한다 (golden/design.md 5절).

uv run python scripts/eval_golden.py --label base [--ontology off|ab|abc|oracle] [--enforce] [--slots] [--runs 3] [--only C1-01,C4-06]
uv run python scripts/eval_golden.py --set holdout --label x_holdout [...]   # 홀드아웃(golden/holdout_v1.yaml) 채점
uv run python scripts/eval_golden.py --rescore data/golden_runs/base_r1.jsonl [--write] [--label base_v2]
    # DB·LLM 없이 현재 골든 버전으로 재채점. --label을 주면 그 이름의 새 파일(_r1.jsonl, _summary.md)로 저장
uv run python scripts/eval_golden.py --compare base base_enf                               # 구성별 문항 비교표

출력: data/golden_runs/{label}_r{i}.jsonl (문항별 실행 기록·채점), {label}_summary.md (요약)
"""

import argparse
import json
import re
import statistics
import subprocess
import uuid
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path

import psycopg
import yaml

from portfolio_agent import db
from portfolio_agent.config import EVAL_ONTOLOGY_MODES, PROJECT_ROOT, settings
from portfolio_agent.golden_eval import grade, sql_sha, to_json
from portfolio_agent.sql_guard import SQLGuardError, validate

VERSION = "v2"
GOLDEN_DIR = PROJECT_ROOT / "golden"
RUNS_DIR = PROJECT_ROOT / "data" / "golden_runs"
CATEGORIES = ["C1", "C2", "C3", "C4", "C5", "C6"]
HOLDOUT_VERSION = "holdout_v1"
# 문항 묶음 → (버전 표기, 문항 파일, 정답 스냅샷). 홀드아웃은 고치지 않는다(바꾸면 holdout_v2로 올린다)
SETS = {
    "golden": (VERSION, f"questions_{VERSION}.yaml", f"gold_results_{VERSION}.json"),
    "holdout": (HOLDOUT_VERSION, f"{HOLDOUT_VERSION}.yaml", f"gold_results_{HOLDOUT_VERSION}.json"),
}


def load_golden(qset: str = "golden") -> tuple[list[dict], dict]:
    _, qfile, gfile = SETS[qset]
    questions = yaml.safe_load((GOLDEN_DIR / qfile).read_text())
    gold = json.loads((GOLDEN_DIR / gfile).read_text())
    stale = [q["id"] for q in questions if q["id"] not in gold or gold[q["id"]].get("sql_sha") != sql_sha(q)]
    if stale:
        raise SystemExit(
            f"정답 스냅샷이 질문 파일과 맞지 않습니다: {', '.join(stale)}\n"
            "먼저 `uv run python scripts/verify_golden.py`를 실행하세요(홀드아웃은 --questions/--out)."
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


def oracle_items() -> dict[str, list[str]]:
    """문항별 정답 항목: glossary·rules 항목의 motivated_by를 문항 id로 뒤집는다(파일 순서 유지).

    개념·속성·관계(model.yaml)는 A층이 테이블로 붙이므로 C층 후보인 용어·규칙만 쓴다.
    """
    items: dict[str, list[str]] = defaultdict(list)
    for name, key in (("glossary", "terms"), ("rules", "rules")):
        for item in yaml.safe_load((PROJECT_ROOT / "ontology" / f"{name}.yaml").read_text())[key]:
            for qid in item.get("motivated_by") or []:
                items[qid].append(item["id"])
    return dict(items)


def run_one(agent, q: dict, gold: dict, meta: dict, oracle_ids: list[str] | None = None) -> dict:
    from portfolio_agent.cli import ask  # 재채점·비교만 할 때는 LLM 모듈을 불러오지 않는다

    record = {"id": q["id"], "category": q["category"], "difficulty": q["difficulty"], **meta}
    try:
        trace = asdict(ask(agent, q["question"], str(uuid.uuid4()), oracle_ids=oracle_ids))
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

    questions, gold = load_golden(args.set)
    if args.only:
        ids = set(args.only.split(","))
        questions = [q for q in questions if q["id"] in ids]
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    agent = build_chat_agent(ontology=args.ontology, enforce=args.enforce, slots=args.slots)
    oracle = oracle_items() if args.ontology == "oracle" else None
    meta = {
        "label": args.label,
        "ontology": args.ontology,
        "enforce": args.enforce,
        "slots": args.slots,
        "model": settings.model,
        "git_rev": git_rev(),
        "golden_version": SETS[args.set][0],
    }
    rows = []
    for i in range(1, args.runs + 1):
        path = RUNS_DIR / f"{args.label}_r{i}.jsonl"
        print(f"── run {i}/{args.runs} → {path.name}")
        with path.open("w") as f:
            for q in questions:
                ids = oracle.get(q["id"], []) if oracle is not None else None
                r = run_one(agent, q, gold, {**meta, "run": i}, ids)
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
                f.flush()
                print_row(r)
                rows.append(r)
    write_summary(args.label, rows, questions)


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def rescore(args) -> None:
    questions, gold = load_golden(args.set)
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
        r["golden_version"] = SETS[args.set][0]
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
        f"# 골든 {rows[0].get('golden_version', VERSION)} 채점 요약: {rows[0].get('label', '-')}",
        "",
        f"- 모델 `{rows[0].get('model')}`, git `{rows[0].get('git_rev')}`, "
        f"온톨로지 `{rows[0].get('ontology', 'off')}`, "
        f"enforce {'켬' if rows[0].get('enforce') else '끔'}, "
        f"slots {'켬' if rows[0].get('slots') else '끔'}, "
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
        f"| 생성 실패(문항 수) | {sum(bool(r.get('generation_errors')) for r in rows)} | - |",
        f"| 토큰 | {sum(t for t in tokens if t) if any(tokens) else '-'} | {_median(tokens)} |",
        f"| input 토큰 | {sum(t for t in inputs if t) if any(inputs) else '-'} | {_median(inputs)} |",
        f"| 호출당 최대 input 토큰 | {'최대 ' + str(max(max_inputs)) if max_inputs else '-'} | {_median(max_inputs)} |",
    ]

    if rows[0].get("slots"):
        lines += slot_summary(rows)

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


def slot_used(r: dict, sql: str) -> bool:
    """확인된 조각이 SQL에 들어갔는가: 그 컬럼 이름과 값이 모두 SQL에 보인다(대소문자·따옴표 무시).

    이름은 식별값(조각의 다른 문자열, 예: item_id = '…')으로 따른 것도 쓴 것으로 본다.
    """
    text = sql.lower().replace('"', "")
    if r["column"].lower() in text and all(f"'{v}'".lower() in text for v in r["values"]):
        return True
    return r["slot"] == "name" and any(f"'{v}'".lower() in text for v in re.findall(r"'([^']+)'", r["fragment"]))


def slot_summary(rows: list[dict]) -> list[str]:
    """칸 진단: LLM이 칸을 뽑았나 → 코드가 처리했나 → SQL이 따랐나(used: 마지막 성공 SQL에 조각이 들어감)."""
    with_slots = [r for r in rows if any(r.get("slots", {}).get(k) for k in ("names", "grades", "values"))]
    status = Counter((x["slot"], x["status"]) for r in rows for x in r.get("resolved", []))
    confirmed = [(r, x) for r in rows for x in r.get("resolved", []) if x["status"] == "confirmed"]

    def last_sql(r):
        ok = [q["sql"] for q in r["queries"] if q["ok"]]
        return ok[-1] if ok else ""

    used = sum(slot_used(x, last_sql(r)) for r, x in confirmed)
    lines = ["", "## 칸(slots)", f"- 칸이 하나라도 있는 문항: {len(with_slots)}/{len(rows)}",
             f"- 확인된 조각이 마지막 성공 SQL에 들어감(used): {used}/{len(confirmed)}",
             "", "| 칸 | 상태 | 개수 |", "|---|---|---|"]
    lines += [f"| {slot} | {st} | {n} |" for (slot, st), n in sorted(status.items())] or ["| - | - | 0 |"]
    lines += ["", "| ID | 정답 | 칸 처리 (상태 · 조각, used) |", "|---|---|---|"]
    for r in rows:
        items = [x for x in r.get("resolved", []) if x["status"] != "skipped"]
        if not items:
            continue
        cells = [f"{x['text']}: {x['status']}" + (f" ({'used' if slot_used(x, last_sql(r)) else 'not used'})"
                                                    if x["status"] == "confirmed" else "") for x in items]
        lines.append(f"| {r['id']} | {'O' if r['correct'] else 'X'} | " + "; ".join(cells).replace("|", "\\|") + " |")
    return lines


def write_summary(label: str, rows: list[dict], questions: list[dict]) -> None:
    text = summary(rows, questions)
    path = RUNS_DIR / f"{label}_summary.md"
    path.write_text(text)
    print("\n" + text)
    print(f"요약 저장: {path}")


def compare(labels: list[str], qset: str = "golden") -> None:
    """구성별로 문항 정답 여부(여러 회면 정답 횟수)를 나란히 놓는다."""
    questions, _ = load_golden(qset)
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
    parser.add_argument("--set", choices=list(SETS), default="golden",
                        help="문항 묶음 (golden: questions_v2, holdout: holdout_v1). --rescore·--compare에도 적용")
    parser.add_argument("--label", help="구성 이름 (결과 파일 접두어, 기본 base). --rescore와 쓰면 새 이름으로 저장")
    parser.add_argument("--ontology", choices=EVAL_ONTOLOGY_MODES, default="off",
                        help="온톨로지 지식 모드 (off: 넣지 않음, ab: 테이블·공통, abc: +질문 검색, "
                             "oracle: 검색 대신 문항의 정답 항목, 진단 전용)")
    parser.add_argument("--enforce", action="store_true", help="규칙 강제(sql_rules) 사용")
    parser.add_argument("--slots", action="store_true", help="칸 추출(analyze_question)·처리(resolve_slots) 노드 사용")
    parser.add_argument("--runs", type=int, default=1, help="같은 구성 반복 횟수 (일관성 측정)")
    parser.add_argument("--only", help="쉼표로 구분한 문항 ID")
    parser.add_argument("--rescore", help="저장된 JSONL을 DB·LLM 없이 다시 채점")
    parser.add_argument("--write", action="store_true", help="--rescore 결과를 파일에 반영")
    parser.add_argument("--compare", nargs="+", metavar="LABEL", help="구성별 문항 비교표")
    args = parser.parse_args()
    if args.rescore:
        rescore(args)
    elif args.compare:
        compare(args.compare, args.set)
    else:
        args.label = args.label or "base"
        run(args)


if __name__ == "__main__":
    main()
