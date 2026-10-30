"""--enforce: 기계적으로 판별되는 규칙을 프롬프트 대신 코드가 재작성·지적·공시한다 (책 5-1, 5-5, 7-2, 7-4).

- 재작성(rewrite): ORDER BY … DESC → DESC NULLS LAST (P-NULL: 정렬 컬럼의 NULL이 위로 오지 않게)
- 지적(violations): 등급 컬럼의 문자열 대소 비교, 현재 시각 함수 → run_query가 Error:로 돌려준다
- 알림(result_note): 결과가 LIMIT에 닿았을 때
- 공시(applied_conditions): 성공한 SQL에서 찾은 기준일·결측 제외·0 제외 조건을 답변 끝에 붙인다

모두 순수 함수다(applied_conditions만 data/schema_catalog.json을 읽는다).
"""

import sqlglot
from sqlglot import exp

from .config import ALLOWED_TABLES, settings
from .schema_catalog import load_catalog

NULL_DISCLOSE_PCT = 10  # P-NULL: 결측률이 이 값(%) 이상인 컬럼으로 행을 걸렀으면 답변에 밝힌다
_NOW_FUNCS = (exp.CurrentDate, exp.CurrentTimestamp, exp.CurrentTime, exp.Localtimestamp)  # now()는 CurrentTimestamp


def _parse(sql: str) -> exp.Expression | None:
    try:
        return sqlglot.parse_one(sql, read="postgres")
    except (sqlglot.errors.ParseError, sqlglot.errors.TokenError):
        return None


def rewrite(sql: str) -> str:
    """ORDER BY … DESC를 DESC NULLS LAST로 바꾼다. 파싱에 실패하거나 DESC가 없으면 원문 그대로.

    제약: sqlglot은 DESC와 DESC NULLS FIRST를 구분하지 못해(둘 다 nulls_first=True)
    명시적으로 쓴 DESC NULLS FIRST도 NULLS LAST가 된다.
    """
    tree = _parse(sql)
    desc = [o for o in tree.find_all(exp.Ordered) if o.args.get("desc")] if tree else []
    if not desc:
        return sql
    for o in desc:
        o.set("nulls_first", False)
    return tree.sql(dialect="postgres")


def ordered_columns(kn) -> dict[str, tuple[str, list[str]]]:
    """순서가 있는 용어(order_values)의 정본 컬럼 → (용어 라벨, 순서 목록). 테이블은 보지 않고 컬럼 이름만 쓴다."""
    out = {}
    for tid, term in kn.terms.items():
        if not term["order_values"]:
            continue
        for ref, _ in term["canonical"]:
            if "." in ref:
                out[ref.split(".", 1)[1]] = (kn.items[tid]["label"], list(term["order_values"]))
    return out


def _is_str(node) -> bool:
    return isinstance(node, exp.Literal) and node.is_string


def violations(sql: str, ordered: dict) -> list[str]:
    """실행 전에 막을 규칙 위반 목록. 비면 통과."""
    tree = _parse(sql)
    if tree is None:
        return []
    found = []
    for node in tree.find_all(exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between):
        if isinstance(node, exp.Between):
            col, others = node.this, [node.args.get("low"), node.args.get("high")]
        else:
            col, others = (node.this, [node.expression]) if isinstance(node.this, exp.Column) else (node.expression, [node.this])
        if isinstance(col, exp.Column) and col.name in ordered and any(_is_str(o) for o in others):
            label, order = ordered[col.name]
            found.append(
                f"{col.name}({label})을 문자열로 대소 비교하면 사전순이라 등급 순서와 다릅니다. "
                f"등급 순서: {', '.join(order)}. 원하는 등급을 {col.name} IN (...)으로 나열하세요.")
    if any(tree.find_all(*_NOW_FUNCS)):
        found.append(f"현재 시각 함수(CURRENT_DATE, now() 등)는 쓸 수 없습니다. "
                     f"데이터는 기준일 스냅샷이므로 DATE '{settings.as_of_date}'을 쓰세요.")
    return list(dict.fromkeys(found))


def result_note(n_rows: int) -> str:
    """결과 행 수가 강제 LIMIT에 닿았으면 알림을 돌려준다."""
    if n_rows < settings.default_limit:
        return ""
    return f"\n(LIMIT {settings.default_limit}에 닿아 결과가 잘렸을 수 있음. 개수는 COUNT로 구할 것)"


def _column_info(name: str, tables: set[str]) -> dict | None:
    """쿼리의 테이블 중 그 컬럼을 가진 테이블이 하나일 때만 컬럼 정보를 돌려준다."""
    catalog = load_catalog()["tables"]
    hits = [c for t in sorted(tables) if t in catalog for c in catalog[t]["columns"] if c["name"] == name]
    return hits[0] if len(hits) == 1 else None


def _is_zero(node) -> bool:
    return isinstance(node, exp.Literal) and node.this == "0"


def applied_conditions(sqls: list[str]) -> str:
    """성공한 SQL들에 들어 있는 공시 대상 조건을 '[적용 조건]' 블록으로 만든다. 없으면 빈 문자열."""
    lines = []
    for sql in sqls:
        tree = _parse(sql)
        if tree is None:
            continue
        if settings.as_of_date in sql:
            lines.append(f"기준일: {settings.as_of_date} (데이터 스냅샷)")
        tables = {t.name.lower() for t in tree.find_all(exp.Table)} & set(ALLOWED_TABLES)
        for node in tree.find_all(exp.Is, exp.GT, exp.NEQ):
            if isinstance(node, exp.Is):
                if not (node.args.get("negate") and isinstance(node.expression, exp.Null)):
                    continue
                col, kind = node.this, "null"
            elif isinstance(node.this, exp.Column) and _is_zero(node.expression):
                col, kind = node.this, "zero"
            elif isinstance(node, exp.NEQ) and isinstance(node.expression, exp.Column) and _is_zero(node.this):
                col, kind = node.expression, "zero"
            else:
                continue
            info = _column_info(col.name, tables) if isinstance(col, exp.Column) else None
            if info is None or (kind == "null" and info["null_pct"] < NULL_DISCLOSE_PCT):
                continue
            label = info["comment"] or info["name"]
            lines.append(f"{label}: 값이 없는 행 제외" if kind == "null" else f"{label}: 0인 행 제외")
    lines = list(dict.fromkeys(lines))
    return "\n\n[적용 조건]\n" + "\n".join(f"- {line}" for line in lines) if lines else ""
