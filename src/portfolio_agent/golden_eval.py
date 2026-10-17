"""골든 데이터셋 채점 함수 (golden/design.md 5절). DB·LLM 없이 동작하는 순수 함수만 둔다.

입력 형식
- q: questions_v1.yaml 문항 하나
- gold: gold_results_v1.json 항목 하나 ({"columns", "rows"})
- record: 실행 기록. AskTrace 필드에 더해 queries[i]마다 재실행 결과
  {"rerun_columns", "rerun_rows"}(성공한 쿼리만)를 가진다. 행 값은 to_json으로 바꾼 값이다.
"""

import decimal
import hashlib
import itertools
import re
from datetime import date, datetime
from functools import lru_cache

import sqlglot
from sqlglot import exp

DATA_DATE_PARTS = {2026.0, 8.0, 21.0}  # 데이터 기준일 2026-08-21 (P-DATE)
MAX_ANSWER_VALUES = 10  # 이보다 키 값이 많은 목록은 answer_ok를 판정하지 않는다
_NUM_STR = re.compile(r"-?\d+(?:\.\d+)?")


def sql_sha(q: dict) -> str | None:
    """정답 SQL 지문. verify_golden.py가 기록하고 eval_golden.py가 정답 스냅샷이 최신인지 확인한다."""
    sql = q.get("gold_sql")
    return hashlib.sha256(sql.encode()).hexdigest()[:12] if sql else None


# ───────────────────────── 값 정규화 ─────────────────────────
def to_json(v):
    """재실행 결과를 JSONL에 저장할 값으로 바꾼다. Decimal은 자릿수를 지키려고 문자열로 둔다."""
    if isinstance(v, decimal.Decimal):
        return str(v)
    if isinstance(v, date | datetime):
        return v.isoformat()
    return v


def to_number(v) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int | float | decimal.Decimal):
        return float(v)
    if isinstance(v, str) and _NUM_STR.fullmatch(v.strip()):
        return float(v)
    return None


def _decimals(v) -> int:
    """값에 적힌 소수 자릿수 (0.51 → 2, '0.5100' → 4, 78 → 0)."""
    s = v.strip() if isinstance(v, str) else repr(v) if isinstance(v, float) else str(v)
    return len(s.split(".", 1)[1]) if "." in s and "e" not in s.lower() else 0


def _round(x: float, d: int) -> decimal.Decimal:
    # PostgreSQL ROUND와 같은 사사오입. float 이진 오차를 피하려고 repr 문자열에서 시작한다.
    return decimal.Decimal(repr(x)).quantize(decimal.Decimal(1).scaleb(-d), decimal.ROUND_HALF_UP)


def values_equal(agent, gold) -> bool:
    """5.4-4 숫자 정규화 규칙. 숫자가 아니면 앞뒤 공백을 뗀 문자열로 비교한다."""
    a, g = to_number(agent), to_number(gold)
    if a is None or g is None:
        if agent is None or gold is None:
            return agent is None and gold is None
        return str(agent).strip() == str(gold).strip()
    if isinstance(gold, int):  # 개수 등 정수 정답은 정확히 일치해야 한다
        return a == g
    if _round(a, 2) == _round(g, 2):
        return True
    d = _decimals(agent)
    return d >= 1 and _round(a, d) == _round(g, d)


# ───────────────────────── 결과 비교 (실행 정확도) ─────────────────────────
def _same_multiset(agent_items: list, gold_items: list, eq) -> bool:
    if len(agent_items) != len(gold_items):
        return False
    left = list(agent_items)
    for g in gold_items:
        for i, a in enumerate(left):
            if eq(a, g):
                del left[i]
                break
        else:
            return False
    return True


def _dedupe(items: list, eq) -> list:
    out: list = []
    for x in items:
        if not any(eq(x, y) for y in out):
            out.append(x)
    return out


def _tuple_eq(a: tuple, g: tuple) -> bool:
    return all(values_equal(x, y) for x, y in zip(a, g, strict=True))


def _key_rows(q: dict, gold: dict) -> list[tuple]:
    idx = [gold["columns"].index(k) for k in q["key_columns"]]
    return [tuple(r[i] for i in idx) for r in gold["rows"]]


def _match_scalar(q, gold, cols, rows) -> dict | None:
    g = _key_rows(q, gold)[0][0]
    if len(rows) != 1:
        return None
    for v in rows[0]:
        if values_equal(v, g):
            return {}
    if q["answer_type"] == "scalar":  # 비율 ×100·÷100 변형 허용
        for v in rows[0]:
            n = to_number(v)
            if n is not None and any(values_equal(round(n * f, 10), g) for f in (100, 0.01)):
                return {"scale_variant": True}
    return None


def _match_list(q, gold, cols, rows) -> dict | None:
    gold_keys = [k[0] for k in _key_rows(q, gold)]
    for j in range(len(cols)):
        col = [r[j] for r in rows]
        if q.get("order_matters"):
            n = len(gold_keys)
            if len(col) < n:
                continue
            alts = {}
            for alt in q.get("accept_alternatives", []):  # {"rank": 1부터, "key": 값} (P-TIE)
                alts.setdefault(alt["rank"] - 1, []).append(alt["key"])
            if all(
                any(values_equal(col[i], c) for c in [gold_keys[i], *alts.get(i, [])]) for i in range(n)
            ):
                return {"agent_columns": [cols[j]]}
        elif _same_multiset(_dedupe(col, values_equal), gold_keys, values_equal):
            return {"agent_columns": [cols[j]]}
    return None


def _match_table(q, gold, cols, rows) -> dict | None:
    """키 튜플 다중집합 비교. label_columns는 값 대신 일대일 대응으로 비교한다."""
    keys = q["key_columns"]
    labels = [i for i, k in enumerate(keys) if k in q.get("label_columns", [])]
    gold_rows = _key_rows(q, gold)
    for perm in itertools.permutations(range(len(cols)), len(keys)):
        agent_rows = [tuple(r[j] for j in perm) for r in rows]
        if _table_eq(agent_rows, gold_rows, labels):
            return {"agent_columns": [cols[j] for j in perm]}
    return None


def _table_eq(agent_rows: list[tuple], gold_rows: list[tuple], labels: list[int]) -> bool:
    if not labels:
        return _same_multiset(agent_rows, gold_rows, _tuple_eq)
    a_vals = [sorted({str(r[i]) for r in agent_rows}) for i in labels]
    g_vals = [sorted({str(r[i]) for r in gold_rows}) for i in labels]
    if any(len(a) != len(g) or len(a) > 6 for a, g in zip(a_vals, g_vals)):
        return False
    for mapping in itertools.product(*(itertools.permutations(g) for g in g_vals)):
        maps = [dict(zip(a, m)) for a, m in zip(a_vals, mapping)]
        remapped = [
            tuple(maps[labels.index(i)][str(v)] if i in labels else v for i, v in enumerate(r))
            for r in agent_rows
        ]
        if _same_multiset(remapped, gold_rows, _tuple_eq):
            return True
    return False


_MATCHERS = {"count": _match_scalar, "scalar": _match_scalar, "list": _match_list, "table": _match_table}


def _key_variants(q: dict) -> list[list[str]]:
    """key_columns와, key_alternatives(같은 상품을 가리키는 다른 식별 컬럼)로 바꾼 조합들 (S4)."""
    alts = q.get("key_alternatives", {})
    return [list(c) for c in itertools.product(*([k, *alts.get(k, [])] for k in q["key_columns"]))]


def match_result(q: dict, gold: dict, cols: list[str], rows: list[list]) -> dict | None:
    """에이전트 결과 하나가 정답과 맞으면 대응 정보(dict), 아니면 None.

    키 컬럼마다 대체 식별 컬럼(key_alternatives) 중 어느 것으로 대응해도 맞은 것으로 본다.
    """
    for keys in _key_variants(q):
        info = _MATCHERS[q["answer_type"]]({**q, "key_columns": keys}, gold, cols, rows)
        if info is not None:
            return info
    return None


def exec_match(q: dict, gold: dict, queries: list[dict]) -> tuple[str, dict]:
    """5.4: 마지막 성공 쿼리 → 앞선 성공 쿼리(S2) → (table 문항만) 성공 쿼리 합집합 순으로 비교한다."""
    done = [x for x in queries if x.get("ok") and "rerun_rows" in x]
    if not done:
        return "no_query", {}
    for status, x in [("last", done[-1]), *(("earlier", y) for y in reversed(done[:-1]))]:
        info = match_result(q, gold, x["rerun_columns"], x["rerun_rows"])
        if info is not None:
            return status, info
    if q["answer_type"] == "table" and len(done) > 1 and _union_covers(q, gold, done):
        return "union", {}
    return "none", {}


def _union_covers(q: dict, gold: dict, done: list[dict]) -> bool:
    """행이 모두 정답 행(라벨 제외)에 들어맞는 쿼리들을 모아 정답 행을 빠짐없이 덮으면 합집합 정답으로 본다.

    정답에 없는 행을 하나라도 낸 쿼리(탐색용 조회 등)는 합집합에 넣지 않는다.
    """
    keep = [i for i, k in enumerate(q["key_columns"]) if k not in q.get("label_columns", [])]
    gold_rows = [tuple(r[i] for i in keep) for r in _key_rows(q, gold)]
    covered: set[int] = set()
    for x in done:
        cols, rows = x["rerun_columns"], x["rerun_rows"]
        for perm in itertools.permutations(range(len(cols)), len(keep)):
            hit = _subset_indices([tuple(r[j] for j in perm) for r in rows], gold_rows)
            if hit:
                covered |= hit
                break
    return len(covered) == len(gold_rows)


def _subset_indices(agent_rows: list[tuple], gold_rows: list[tuple]) -> set[int]:
    """agent_rows가 모두 서로 다른 정답 행에 대응하면 그 정답 행 번호들, 아니면 빈 집합."""
    used: set[int] = set()
    for a in agent_rows:
        i = next((i for i, g in enumerate(gold_rows) if i not in used and _tuple_eq(a, g)), None)
        if i is None:
            return set()
        used.add(i)
    return used


# ───────────────────────── 답변 텍스트 검사 ─────────────────────────
def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "").lower()


def _variants(v) -> list[str]:
    """답변에서 찾을 표기 후보 (콤마, 반올림, 억원·조원 환산)."""
    n = to_number(v)
    out = [str(v)] if isinstance(v, str) else []  # '069500' 같은 코드의 앞자리 0 보존
    if n is None:
        return out or [str(v)]
    if n == int(n) and _decimals(v) == 0:
        out += [str(int(n)), f"{int(n):,}"]
    else:
        out += [f"{n:g}", f"{n:.2f}", f"{n:.1f}"]
        if abs(n) >= 100:  # 작은 수를 정수로 반올림하면 '1' 같은 흔한 문자열이 되어 오판한다
            out += [str(round(n)), f"{round(n):,}"]
    if abs(n) >= 1e8:  # 금액 (P-UNITS)
        eok = n / 1e8
        out += [f"{eok:,.0f}억", f"{eok:.0f}억", f"{n / 1e12:.1f}조", f"{n / 1e12:.2f}조"]
    return out


def answer_values(q: dict, gold: dict) -> list[list]:
    """답변에 나와야 할 정답 값. 값마다 [정답 값, 대체 식별 값…] 묶음이고 하나만 나와도 된다.

    라벨 컬럼은 정답 SQL이 지어낸 값이라 뺀다.
    """
    cols, alts = gold["columns"], q.get("key_alternatives", {})
    groups = [
        [r[cols.index(c)] for c in [k, *alts.get(k, [])]]
        for r in gold["rows"]
        for k in q["key_columns"]
        if k not in q.get("label_columns", [])
    ]
    return groups if len(groups) <= MAX_ANSWER_VALUES else []


def check_answer(q: dict, gold: dict, answer: str) -> tuple[bool | None, list]:
    """5.5 answer_ok. 판정 대상 값이 없으면 (None, []). 빠진 값은 대표 값(정답 키 컬럼)으로 보고한다."""
    groups = answer_values(q, gold)
    if not groups:
        return None, []
    text = _norm(answer)
    missing = [g[0] for g in groups if not any(_norm(c) in text for v in g for c in _variants(v))]
    return not missing, missing


def check_mentions(q: dict, answer: str) -> list[str]:
    """5.6 필수 언급: 충족하지 못한 must_mention 항목 목록."""
    text = _norm(answer)
    return [
        label
        for label, kw in zip(q.get("must_mention", []), q.get("mention_keywords", []), strict=True)
        if not any(_norm(alt) in text for alt in kw.split("|"))
    ]


def check_forbidden(q: dict, answer: str) -> list[str]:
    """금지 패턴은 공백을 유지한 원문에 적용한다."""
    return [p for p in q.get("forbidden_patterns", []) if re.search(p, answer or "")]


_DATE_TEXT = re.compile(r"\d{4}[-./]\d{1,2}[-./]\d{1,2}|\d{4}년|\d{1,2}월|\d{1,2}일")
_NUM_TEXT = re.compile(r"\d[\d,]*(?:\.\d+)?")
_SCALES = (1, 1e4, 1e8, 1e12, 0.01, 100)  # 그대로, 만·억·조 환산, % ↔ 비율


def _numbers(text: str) -> list[tuple[float, int]]:
    out = []
    for m in _NUM_TEXT.finditer(text):
        s = m.group().replace(",", "").rstrip(".")
        try:
            out.append((float(s), _decimals(s)))
        except ValueError:
            pass
    return out


def ungrounded_numbers(question: str, answer: str, queries: list[dict]) -> list[float]:
    """5.5 답변 충실도(참고 지표): 성공한 쿼리 출력에서 찾을 수 없는 답변 속 숫자."""
    source = [n for x in queries if x.get("ok") for n, _ in _numbers(x.get("output", ""))]
    skip = {n for n, _ in _numbers(question)} | DATA_DATE_PARTS
    out = []
    for n, d in _numbers(_DATE_TEXT.sub(" ", answer or "")):
        if n in skip or (d == 0 and n <= MAX_ANSWER_VALUES):  # 기준일·질문 속 숫자·순위 번호
            continue
        tol = 0.5 * 10**-d
        if not any(abs(n * s - o) <= tol * s + 1e-9 for s in _SCALES for o in source):
            out.append(n)
    return out


# ───────────────────────── 컬럼 진단 (판정에는 쓰지 않음) ─────────────────────────
@lru_cache
def _table_columns() -> frozenset[str]:
    from .schema_catalog import load_catalog  # 캐시 파일만 읽는다 (DB 불필요)

    return frozenset(c["name"] for t in load_catalog()["tables"].values() for c in t["columns"])


def sql_columns(sql: str) -> set[str] | None:
    """SQL이 참조하는 실제 테이블 컬럼 이름. 별칭·CTE 컬럼은 스키마에 없으므로 빠진다.

    실행된 SQL과 똑같이 sql_guard.validate를 거쳐 파싱한다(숫자로 시작하는 별칭 보정 포함).
    """
    from .sql_guard import SQLGuardError, validate

    try:
        tree = sqlglot.parse_one(validate(sql), read="postgres")
    except (SQLGuardError, sqlglot.errors.ParseError):
        return None
    names = {c.name.lower() for c in tree.find_all(exp.Column)}
    return names & _table_columns()


def column_diff(q: dict, queries: list[dict]) -> tuple[list[str] | None, list[str] | None]:
    """정답 SQL과 마지막 성공 쿼리의 컬럼 차이 (columns_missing, columns_extra)."""
    done = [x for x in queries if x.get("ok")]
    if not q.get("gold_sql") or not done:
        return None, None
    gold, agent = sql_columns(q["gold_sql"]), sql_columns(done[-1]["sql"])
    if gold is None or agent is None:
        return None, None
    # 대체 식별 컬럼(key_alternatives)끼리는 하나만 써도 빠진 것으로 보지 않는다 (S4)
    for key, alts in q.get("key_alternatives", {}).items():
        group = {key, *alts}
        if agent & group:
            gold, agent = gold - group, agent - group
    return sorted(gold - agent), sorted(agent - gold)


# ───────────────────────── 문항 판정 ─────────────────────────
def grade(q: dict, gold: dict | None, record: dict) -> dict:
    """5.7 문항 판정. record에 error가 있으면 실행 중 예외로 본다."""
    answer = record.get("answer", "")
    queries = record.get("queries", [])
    out = {
        "exec_match": None,
        "scale_variant": False,
        "answer_ok": None,
        "answer_missing": [],
        "mentions_missing": check_mentions(q, answer),
        "forbidden_hits": check_forbidden(q, answer),
        "ungrounded_numbers": ungrounded_numbers(q["question"], answer, queries),
    }
    out["columns_missing"], out["columns_extra"] = column_diff(q, queries)
    if q["answer_type"] == "refusal":
        out["correct"] = not record.get("error") and not out["mentions_missing"] and not out["forbidden_hits"]
    else:
        out["exec_match"], info = exec_match(q, gold, queries)
        out["scale_variant"] = bool(info.get("scale_variant"))
        out["answer_ok"], missing = check_answer(q, gold, answer)
        out["answer_missing"] = [str(v) for v in missing]
        out["correct"] = (
            not record.get("error")
            and out["exec_match"] in ("last", "earlier", "union")
            and out["answer_ok"] is not False
            and not out["mentions_missing"]
        )
    out["fail_reason"] = None if out["correct"] else fail_reason(q, record, out)
    return out


def fail_reason(q: dict, record: dict, g: dict) -> str:
    if record.get("error"):
        return "error"
    if q["answer_type"] == "refusal":
        return "refusal_fail"
    if g["exec_match"] in ("none", "no_query") and not set(q["tables"]) <= set(record.get("selected_tables", [])):
        return "table_miss"
    if g["exec_match"] == "no_query":
        return "no_query"
    if g["exec_match"] == "none":
        return "wrong_result"
    if g["answer_ok"] is False:
        return "answer_mismatch"
    return "missing_mention"
