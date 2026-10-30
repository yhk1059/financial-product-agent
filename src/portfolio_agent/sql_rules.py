"""--enforce: 기계적으로 판별되는 규칙을 프롬프트 대신 코드가 재작성·지적·공시한다 (책 5-1, 5-5, 7-2, 7-4).

- 재작성(rewrite): ORDER BY … DESC → DESC NULLS LAST (P-NULL: 정렬 컬럼의 NULL이 위로 오지 않게)
- 지적(violations): 등급 컬럼의 문자열 대소 비교, 현재 시각 함수, 0이 미입력인 컬럼을 0 제외 없이 계산에 쓰기
  → run_query가 Error:로 돌려준다
- 이름(name_literals): 이름 컬럼에 =/IN으로 쓴 문자열 → run_query가 DB에 정확히 있는지 확인한다(tools.py)
- 알림(result_note): 결과가 LIMIT에 닿았을 때
- 공시(applied_conditions): 성공한 SQL에서 찾은 기준일·결측 제외·0 제외 조건을 답변 끝에 붙인다

컬럼의 주인 테이블은 SELECT 범위마다(FROM/JOIN과 별칭) 정한다. UNION으로 이은 국내·해외처럼 같은 이름의
컬럼이 테이블마다 뜻이 달라도(cu_charge_rt: 국내는 0이 미입력, 해외는 실제 값) 범위별로 따로 판정한다.

모두 순수 함수다(컬럼 주인 판정·applied_conditions는 data/schema_catalog.json을 읽는다).
"""

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

from .config import ALLOWED_TABLES, settings
from .schema_catalog import load_catalog

NULL_DISCLOSE_PCT = 10  # P-NULL: 결측률이 이 값(%) 이상인 컬럼으로 행을 걸렀으면 답변에 밝힌다
_NOW_FUNCS = (exp.CurrentDate, exp.CurrentTimestamp, exp.CurrentTime, exp.Localtimestamp)  # now()는 CurrentTimestamp
ZERO_MISSING_RULE = "R-zero-missing"  # 이 규칙의 적용 대상 속성은 0이 미입력이다
NAME_TERMS = {"product_name": "상품", "issuer": "발행기관", "holding": "구성종목"}  # 이름 용어 → 종류


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


def _attr_column(kn, ref: str) -> tuple[str, str] | None:
    """'Concept.column' 속성 → (테이블, 컬럼). 속성이 아니면 None."""
    attr = kn.attributes.get(ref)
    if attr is None:
        return None
    return kn.concepts[attr["concept_id"]]["table_name"], attr["column_name"]


def zero_missing_columns(kn) -> frozenset[tuple[str, str]]:
    """R-zero-missing 적용 대상 속성 → {(테이블, 컬럼)}. 규칙이 없으면 빈 집합."""
    targets = kn.rules.get(ZERO_MISSING_RULE, {}).get("targets", [])
    return frozenset(c for t in targets if (c := _attr_column(kn, t)))


def name_columns(kn) -> dict[tuple[str, str], str]:
    """이름 용어(상품명·발행기관·구성종목)의 정본 컬럼 → 종류."""
    out = {}
    for tid, kind in NAME_TERMS.items():
        for ref, _ in kn.terms.get(tid, {}).get("canonical", []):
            if col := _attr_column(kn, ref):
                out[col] = kind
    return out


def _is_str(node) -> bool:
    return isinstance(node, exp.Literal) and node.is_string


def _scoped_columns(tree):
    """SELECT 범위마다 (범위, 컬럼 노드, 주인 테이블 또는 None). 하위 쿼리 안의 컬럼은 그 범위에서 따로 나온다.

    주인: 별칭·테이블명이 붙었으면 그 테이블, 없으면 그 범위의 허용 테이블 중 그 컬럼을 가진 것이 하나일 때 그 테이블.
    CTE·하위 쿼리를 거친 컬럼은 주인을 정하지 않는다(None).
    """
    catalog = load_catalog()["tables"]
    try:
        scopes = traverse_scope(tree)
    except Exception:  # sqlglot이 범위를 못 만드는 꼴(드묾)이면 판정하지 않는다
        return
    for scope in scopes:
        if not isinstance(scope.expression, exp.Select):
            continue
        tables = {alias: src.name.lower() for alias, src in scope.sources.items()
                  if isinstance(src, exp.Table) and src.name.lower() in ALLOWED_TABLES}
        for col in scope.find_all(exp.Column):
            if col.table:
                owner = tables.get(col.table)
            else:
                hits = {t for t in tables.values() if any(c["name"] == col.name for c in catalog[t]["columns"])}
                owner = hits.pop() if len(hits) == 1 else None
            yield scope, col, owner


def _bare(node):
    """형 변환·괄호를 벗긴 안쪽 노드 (cu_charge_rt::numeric → cu_charge_rt)."""
    while isinstance(node, exp.Cast | exp.TryCast | exp.Paren):
        node = node.this
    return node


def _zero_excluded(scope, name: str) -> bool:
    """그 범위에 컬럼 name의 0을 빼는 조건이 있는가: > 0, <> 0('0'), >= 양수, NULLIF(컬럼, 0)."""
    for node in scope.find_all(exp.GT, exp.GTE, exp.NEQ, exp.Nullif):
        left, right = _bare(node.this), _bare(node.expression)
        if isinstance(node, exp.Nullif):
            if isinstance(left, exp.Column) and left.name == name and _is_zero(right):
                return True
            continue
        if not isinstance(left, exp.Column):  # 0 < col 같은 뒤집힌 꼴은 NEQ만 받는다(applied_conditions와 같게)
            if isinstance(node, exp.NEQ) and isinstance(right, exp.Column) and right.name == name and _is_zero(left):
                return True
            continue
        if left.name != name:
            continue
        if isinstance(node, exp.GTE):
            if (v := _number(right)) is not None and v > 0:
                return True
        elif isinstance(node, exp.GT):
            if (v := _number(right)) is not None and v >= 0:
                return True
        elif _is_zero(right):
            return True
    return False


def _computed(col, scope) -> bool:
    """컬럼이 집계 함수·ORDER BY·대소 비교 안에서 쓰였는가 (그 범위 안에서만 거슬러 올라간다)."""
    node = col.parent
    while node is not None and node is not scope.expression:
        if isinstance(node, exp.AggFunc | exp.Ordered | exp.GT | exp.GTE | exp.LT | exp.LTE | exp.Between):
            return True
        node = node.parent
    return False


def _zero_violations(tree, zero_missing) -> list[str]:
    found = []
    for scope, col, owner in _scoped_columns(tree):
        if (owner, col.name) not in zero_missing or not _computed(col, scope) or _zero_excluded(scope, col.name):
            continue
        info = _catalog_column(owner, col.name)
        label = f"{col.name}({info['comment']})" if info and info.get("comment") else col.name
        fix = f"{col.name}::numeric > 0" if info and info.get("type") == "text" else f"{col.name} > 0"
        found.append(f"{owner}.{label}의 0은 미입력(값 없음)입니다. 계산·정렬·비교에 쓰려면 {fix}로 0인 행을 빼고 "
                     "계산하고, 뺀 사실을 답변에 밝히세요.")
    return found


def violations(sql: str, ordered: dict, zero_missing: frozenset = frozenset()) -> list[str]:
    """실행 전에 막을 규칙 위반 목록. 비면 통과. zero_missing: {(테이블, 컬럼)} 0이 미입력인 컬럼."""
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
    if zero_missing:
        found += _zero_violations(tree, zero_missing)
    return list(dict.fromkeys(found))


def name_literals(sql: str, names: dict) -> list[tuple[str, str, str, str]]:
    """이름 컬럼에 =/IN으로 쓴 문자열 → [(종류, 테이블, 컬럼, 값)] (순서 유지, 중복 제거)."""
    tree = _parse(sql)
    if tree is None or not names:
        return []
    out = []
    for _, col, owner in _scoped_columns(tree):
        kind = names.get((owner, col.name))
        parent = col.parent
        if kind is None or parent is None:
            continue
        if isinstance(parent, exp.EQ):
            other = parent.expression if parent.this is col else parent.this
            values = [other] if _is_str(other) else []
        elif isinstance(parent, exp.In) and parent.this is col:
            values = [v for v in parent.expressions if _is_str(v)]
        else:
            continue
        out += [(kind, owner, col.name, v.this) for v in values]
    return list(dict.fromkeys(out))


def result_note(n_rows: int) -> str:
    """결과 행 수가 강제 LIMIT에 닿았으면 알림을 돌려준다."""
    if n_rows < settings.default_limit:
        return ""
    return f"\n(LIMIT {settings.default_limit}에 닿아 결과가 잘렸을 수 있음. 개수는 COUNT로 구할 것)"


def _catalog_column(table: str | None, name: str) -> dict | None:
    if table is None:
        return None
    columns = load_catalog()["tables"].get(table, {}).get("columns", [])
    return next((c for c in columns if c["name"] == name), None)


def _number(node) -> float | None:
    """숫자(또는 숫자 문자열) 리터럴의 값. 아니면 None."""
    if not isinstance(node, exp.Literal):
        return None
    try:
        return float(node.this)
    except ValueError:
        return None


def _is_zero(node) -> bool:
    return _number(node) == 0


def _exclusion_kind(col) -> str | None:
    """컬럼이 낀 조건이 결측 제외(IS NOT NULL)면 'null', 0 제외(> 0, <> 0, NULLIF(…, 0))면 'zero'.

    형 변환으로 감싼 꼴(cu_charge_rt::numeric > 0)도 받는다.
    """
    node = col
    while isinstance(node.parent, exp.Cast | exp.TryCast | exp.Paren):
        node = node.parent
    parent = node.parent
    if isinstance(parent, exp.Is):
        return "null" if parent.this is node and parent.args.get("negate") and isinstance(parent.expression, exp.Null) else None
    if isinstance(parent, exp.Nullif):
        return "zero" if parent.this is node and _is_zero(parent.expression) else None
    if isinstance(parent, exp.GT):
        return "zero" if parent.this is node and _is_zero(parent.expression) else None
    if isinstance(parent, exp.NEQ):
        other = parent.expression if parent.this is node else parent.this
        return "zero" if _is_zero(other) else None
    return None


def applied_conditions(sqls: list[str]) -> str:
    """성공한 SQL들에 들어 있는 공시 대상 조건을 '[적용 조건]' 블록으로 만든다. 없으면 빈 문자열."""
    lines = []
    for sql in sqls:
        tree = _parse(sql)
        if tree is None:
            continue
        if settings.as_of_date in sql:
            lines.append(f"기준일: {settings.as_of_date} (데이터 스냅샷)")
        for _, col, owner in _scoped_columns(tree):
            kind = _exclusion_kind(col)
            info = _catalog_column(owner, col.name) if kind else None
            if info is None or (kind == "null" and info["null_pct"] < NULL_DISCLOSE_PCT):
                continue
            label = info["comment"] or info["name"]
            lines.append(f"{label}: 값이 없는 행 제외" if kind == "null" else f"{label}: 0인 행 제외")
    lines = list(dict.fromkeys(lines))
    return "\n\n[적용 조건]\n" + "\n".join(f"- {line}" for line in lines) if lines else ""
