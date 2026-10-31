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

import re

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

from .config import ALLOWED_TABLES, settings
from .schema_catalog import load_catalog
from .sql_guard import _DIGIT_ALIAS

NULL_DISCLOSE_PCT = 10  # P-NULL: 결측률이 이 값(%) 이상인 컬럼으로 행을 걸렀으면 답변에 밝힌다
RULE_MARK = "[규칙]"  # 실행 전에 코드가 막은 오류의 표시. graph.count_attempt는 이 오류를 실행 횟수에서 뺀다(FREE_RULE_BLOCKS까지)
ITEM_UNIT_CONCEPT = {"prbd01n001": "Bond"}  # P-UNIT: 여러 행이 같은 종목인 테이블 → 종목 개념 (식별 컬럼으로 중복 제거)
ROW_UNIT_WHEN = {"prbd01n001": "buyable_quantity"}  # P-UNIT 예외: 이 컬럼 조건(판매 가능 건)이 있으면 행 단위로 센다
CLASS_UNIT_TABLES = ("prfd01n001",)  # 1행 = 클래스. COUNT(DISTINCT 식별 컬럼 아닌 것)은 펀드 수가 아니다
CASH_TERM = "cash_like_holding"  # 현금성 항목 용어: canonical 값이 제외 목록
HOLDINGS_TABLE, HOLDING_NAME, HOLDING_ETF = "domestic_etf_holdings", "component_name", "item_id"
_DATE_LITERAL = re.compile(r"'\d{4}-\d{2}-\d{2}'")
ALL_OF = re.compile(r"둘 다|모두|전부|함께|동시에|다 (담|포함|들어)")  # '여러 종목을 모두 담은' 질문의 말
_NOW_FUNCS = (exp.CurrentDate, exp.CurrentTimestamp, exp.CurrentTime, exp.Localtimestamp)  # now()는 CurrentTimestamp
ZERO_MISSING_RULE = "R-zero-missing"  # 이 규칙의 적용 대상 속성은 0이 미입력이다
ZERO_VALUE_RULE = "R-zero-is-value"   # 이 규칙의 적용 대상 속성은 0이 실제 값이다(빼면 안 된다)
NAME_TERMS = {"product_name": "상품", "issuer": "발행기관", "holding": "구성종목"}  # 이름 용어 → 종류


def _parse(sql: str) -> exp.Expression | None:
    try:
        return sqlglot.parse_one(sql, read="postgres")
    except (sqlglot.errors.ParseError, sqlglot.errors.TokenError):
        return None


def rewrite(sql: str, zero_missing: frozenset = frozenset()) -> str:
    """P-NULL·P-ZERO 재작성. 파싱에 실패하거나 고칠 것이 없으면 원문 그대로.

    - ORDER BY … DESC → DESC NULLS LAST
    - 결측률 NULL_DISCLOSE_PCT 이상인 컬럼으로 GROUP BY 하면 그 SELECT의 WHERE에 컬럼 IS NOT NULL을 더한다
      (결측 그룹이 한 행으로 나오지 않게. 이미 IS NOT NULL·=·IN 조건이 있으면 그대로)
    - zero_missing(0이 미입력인 {(테이블, 컬럼)})의 컬럼을 집계·정렬·비교에 쓰면서 0을 빼지 않은 SELECT의 WHERE에
      NULLIF(컬럼, '')::numeric > 0(text) 또는 컬럼 > 0을 더한다. UNION의 국내·해외는 SELECT마다 따로 본다
    제약: sqlglot은 DESC와 DESC NULLS FIRST를 구분하지 못해(둘 다 nulls_first=True)
    명시적으로 쓴 DESC NULLS FIRST도 NULLS LAST가 된다.
    """
    tree = _parse(sql)
    if tree is None:  # 숫자로 시작하는 별칭(AS 3년수익률)은 guard가 따옴표를 붙여 실행하므로 같은 꼴로 다시 파싱한다
        sql = _DIGIT_ALIAS.sub(r'\1"\2"', sql)
        tree = _parse(sql)
    if tree is None:
        return sql
    changed = False
    for o in tree.find_all(exp.Ordered):
        if o.args.get("desc"):
            o.set("nulls_first", False)
            changed = True
    for scope in _select_scopes(tree):
        select, tables = scope.expression, _scope_tables(scope)
        group = select.args.get("group")
        for g in group.expressions if group is not None else []:
            col = _bare(g)
            if not isinstance(col, exp.Column):
                continue
            info = _catalog_column(_owner_of(col, tables), col.name)
            if info is None or info["null_pct"] < NULL_DISCLOSE_PCT or _constrained(select, col.name):
                continue
            select.where(exp.Is(this=col.copy(), expression=exp.Null()).not_(), copy=False)
            changed = True
        for name, owner in {(c.name, _owner_of(c, tables)) for c in select.find_all(exp.Column)
                            if c.find_ancestor(exp.Select) is select}:
            if (owner, name) not in zero_missing or _zero_excluded(scope, name):
                continue
            cols = [c for c in select.find_all(exp.Column) if c.name == name and c.find_ancestor(exp.Select) is select]
            if not any(_computed(c, scope) for c in cols):
                continue
            info = _catalog_column(owner, name)
            select.where(_zero_condition(cols[0], info and info.get("type") == "text"), copy=False)
            changed = True
    return tree.sql(dialect="postgres") if changed else sql


def _zero_condition(col, text: bool):
    """0을 빼는 조건: NULLIF(col, '')::numeric > 0 (text 컬럼) 또는 col > 0."""
    left = col.copy()
    if text:
        left = exp.Cast(this=exp.Nullif(this=left, expression=exp.Literal.string("")),
                        to=exp.DataType.build("numeric"))
    return exp.GT(this=left, expression=exp.Literal.number(0))


def _constrained(select, name: str) -> bool:
    """SELECT의 WHERE에 컬럼 name의 IS NOT NULL, =, IN 조건이 있는가."""
    where = select.args.get("where")
    if where is None:
        return False
    for node in where.find_all(exp.Is, exp.EQ, exp.In):
        col = _bare(node.this)
        if isinstance(col, exp.Column) and col.name == name:
            if isinstance(node, exp.Is):
                if node.args.get("negate") or isinstance(node.parent, exp.Not):
                    return True
            else:
                return True
    return False


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


def zero_value_columns(kn) -> frozenset[tuple[str, str]]:
    """R-zero-is-value 적용 대상 속성 → {(테이블, 컬럼)}: 0이 실제 값이라 빼면 안 되는 컬럼."""
    targets = kn.rules.get(ZERO_VALUE_RULE, {}).get("targets", [])
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
    """형 변환·괄호·빈 문자열 NULLIF를 벗긴 안쪽 노드 (NULLIF(cu_charge_rt, '')::numeric → cu_charge_rt)."""
    while isinstance(node, exp.Cast | exp.TryCast | exp.Paren) or _empty_nullif(node):
        node = node.this
    return node


def _empty_nullif(node) -> bool:
    return isinstance(node, exp.Nullif) and _is_str(node.expression) and node.expression.this == ""


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
        fix = f"NULLIF({col.name}, '')::numeric > 0" if info and info.get("type") == "text" else f"{col.name} > 0"
        found.append(f"{owner}.{label}의 0은 미입력(값 없음)입니다. 계산·정렬·비교에 쓰려면 {fix}로 0인 행을 빼고 "
                     "계산하고, 뺀 사실을 답변에 밝히세요.")
    return found


def unit_config(kn) -> dict[str, tuple[str, str | None]]:
    """테이블 → (종목 식별 컬럼, 행 단위 허용 조건 컬럼). ITEM_UNIT_CONCEPT의 개념 identity에서 읽는다."""
    out = {}
    for table, concept in ITEM_UNIT_CONCEPT.items():
        ident = kn.concepts.get(concept, {}).get("identity") or []
        if len(ident) == 1:
            out[table] = (ident[0], ROW_UNIT_WHEN.get(table))
    return out


def class_id_columns(kn) -> dict[str, frozenset[str]]:
    """클래스 단위 테이블 → 그 테이블 개념들의 식별 컬럼(이 컬럼의 DISTINCT 수만 펀드 수다)."""
    out = {}
    for table in CLASS_UNIT_TABLES:
        cols = {c for cid, c_ in kn.concepts.items() if c_["table_name"] == table for c in (c_.get("identity") or [])}
        if cols:
            out[table] = frozenset(cols)
    return out


def cash_values(kn) -> list[str]:
    """현금성 항목 용어의 canonical 값 목록. 용어가 없으면 빈 목록."""
    for _, values in kn.terms.get(CASH_TERM, {}).get("canonical", []):
        if values:
            return list(values)
    return []


def _scope_tables(scope) -> dict[str, str]:
    return {alias: src.name.lower() for alias, src in scope.sources.items()
            if isinstance(src, exp.Table) and src.name.lower() in ALLOWED_TABLES}


def _owner_of(col, tables: dict[str, str]) -> str | None:
    catalog = load_catalog()["tables"]
    if col.table:
        return tables.get(col.table)
    hits = {t for t in tables.values() if any(c["name"] == col.name for c in catalog.get(t, {}).get("columns", []))}
    return hits.pop() if len(hits) == 1 else None


def _select_scopes(tree):
    try:
        scopes = traverse_scope(tree)
    except Exception:
        return []
    return [s for s in scopes if isinstance(s.expression, exp.Select)]


def _agg_arg(agg) -> tuple[bool, exp.Column | None]:
    """집계 함수의 인자 → (DISTINCT인가, 컬럼 또는 None(*·식))."""
    arg = agg.this
    distinct = isinstance(arg, exp.Distinct)
    if distinct:
        arg = arg.expressions[0] if len(arg.expressions) == 1 else None
    arg = _bare(arg) if arg is not None else None
    return distinct, arg if isinstance(arg, exp.Column) else None


def _unit_violations(tree, units: dict, class_ids: dict) -> list[str]:
    """P-UNIT: 종목이 여러 행인 테이블의 행 단위 집계, 클래스 테이블의 엉뚱한 DISTINCT 수."""
    found = []
    for scope in _select_scopes(tree):
        tables = _scope_tables(scope)
        for agg in scope.expression.find_all(exp.Count, exp.Avg, exp.Sum):
            if agg.find_ancestor(exp.Select) is not scope.expression:
                continue
            distinct, col = _agg_arg(agg)
            owners = {_owner_of(col, tables)} if col is not None else set(tables.values())
            for table in owners:
                if table in units:
                    id_col, row_when = units[table]
                    if distinct and col is not None and col.name == id_col:
                        continue
                    if row_when and any(c.name == row_when for c in scope.expression.find_all(exp.Column)):
                        continue
                    found.append(f"{table}은 같은 종목({id_col})이 여러 행입니다. 종목 수는 COUNT(DISTINCT {id_col})로 세고, "
                                 f"평균 같은 집계는 (SELECT DISTINCT {id_col}, 컬럼 FROM {table} WHERE …) 서브쿼리 위에서 계산하세요. "
                                 f"행 단위로 세는 것은 판매 건수({row_when} > 0)를 물을 때뿐입니다.")
                elif table in class_ids and isinstance(agg, exp.Count) and distinct and col is not None \
                        and col.name not in class_ids[table]:
                    ids = ", ".join(sorted(class_ids[table]))
                    found.append(f"{table}은 1행 = 펀드 클래스입니다. 클래스 수는 COUNT(*)로 세고, 대표펀드 기준이면 "
                                 f"식별 컬럼({ids})의 COUNT(DISTINCT …)로 셉니다. {col.name}의 DISTINCT 수는 펀드 수가 아닙니다.")
    return found


def _holding_name_cols(scope, tables):
    return [c for c in scope.expression.find_all(exp.Column)
            if c.name == HOLDING_NAME and _owner_of(c, tables) == HOLDINGS_TABLE]


def _cash_violations(tree, cash: list[str]) -> list[str]:
    """P-CASH: 구성종목명을 세거나 나열하면서 현금성 항목을 빼지 않은 SELECT."""
    if not cash:
        return []
    found = []
    for scope in _select_scopes(tree):
        tables = _scope_tables(scope)
        if HOLDINGS_TABLE not in tables.values():
            continue
        select = scope.expression
        projected = any(_owner_of(c, tables) == HOLDINGS_TABLE for e in select.expressions
                        for c in e.find_all(exp.Column) if c.name == HOLDING_NAME)
        if not projected:
            continue
        filtered = excluded = False
        for c in _holding_name_cols(scope, tables):
            node = c
            while isinstance(node.parent, exp.Cast | exp.Paren):
                node = node.parent
            parent = node.parent
            if isinstance(parent, exp.EQ | exp.Like | exp.ILike) and not isinstance(parent.parent, exp.Not):
                filtered = True
            elif isinstance(parent, exp.In) and parent.this is node:
                literals = {v.this for v in parent.expressions if _is_str(v)}
                if isinstance(parent.parent, exp.Not) and literals & set(cash):
                    excluded = True
                elif not isinstance(parent.parent, exp.Not) and literals:
                    filtered = True
            elif isinstance(parent, exp.NEQ) and any(_is_str(o) and o.this in cash for o in (parent.this, parent.expression)):
                excluded = True
        if filtered or excluded:
            continue
        quoted = ", ".join(f"'{v}'" for v in cash)
        found.append(f"구성종목({HOLDING_NAME})에는 현금성 항목({quoted})이 섞여 있습니다. 종목을 세거나 나열할 때는 "
                     f"{HOLDING_NAME} NOT IN ({quoted}) 조건을 넣으세요.")
    return found


def _set_logic_violations(tree, question: str) -> list[str]:
    """R-holding-set-logic: '둘 다/모두 담은' 질문인데 종목 IN 목록만 쓰고 HAVING COUNT(DISTINCT …) = n이 없는 SQL."""
    if not question or not ALL_OF.search(question):
        return []
    if any(tree.find_all(exp.Intersect)):
        return []
    found = []
    for scope in _select_scopes(tree):
        tables = _scope_tables(scope)
        if HOLDINGS_TABLE not in tables.values():
            continue
        names = set()
        for c in _holding_name_cols(scope, tables):
            parent = c.parent
            if isinstance(parent, exp.In) and parent.this is c and not isinstance(parent.parent, exp.Not):
                names |= {v.this for v in parent.expressions if _is_str(v)}
        if len(names) < 2:
            continue
        having = scope.expression.args.get("having")
        if having and any(isinstance(cnt.this, exp.Distinct) for cnt in having.find_all(exp.Count)):
            continue
        n = len(names)
        found.append(f"여러 종목을 모두 담은 ETF를 IN만으로 고르면 '하나라도 담은' ETF가 됩니다. "
                     f"SELECT {HOLDING_ETF} FROM {HOLDINGS_TABLE} WHERE {HOLDING_NAME} IN (…) GROUP BY {HOLDING_ETF} "
                     f"HAVING COUNT(DISTINCT {HOLDING_NAME}) = {n} 꼴로 거르고, 그 결과를 세거나 조인하세요.")
        break
    return found


def _ratio_violations(tree) -> list[str]:
    """R-ratio-denominator: 분자가 결측률 NULL_DISCLOSE_PCT 이상인 컬럼을 보는데 분모가 COUNT(*)인 비율."""
    found = []
    for scope in _select_scopes(tree):
        tables = _scope_tables(scope)
        for div in scope.expression.find_all(exp.Div):
            if div.find_ancestor(exp.Select) is not scope.expression:
                continue
            denom = _bare(div.expression)
            if not (isinstance(denom, exp.Count) and isinstance(denom.this, exp.Star)):
                continue
            for col in div.this.find_all(exp.Column):
                info = _catalog_column(_owner_of(col, tables), col.name)
                if info and info["null_pct"] >= NULL_DISCLOSE_PCT:
                    found.append(f"비율의 분모 COUNT(*)는 {col.name}({info['comment'] or ''})이 NULL인 행도 셉니다. "
                                 f"분모를 COUNT({col.name})로 바꿔 값이 있는 행만 세고, 뺀 사실을 답변에 밝히세요.")
                    break
    return found


def rule_violations(sql: str, units: dict | None = None, class_ids: dict | None = None, cash: list[str] = (),
                    question: str = "") -> list[str]:
    """P-UNIT·P-CASH·집합 논리·비율 분모 검사. 비면 통과. 모두 순수 함수(카탈로그만 읽는다)."""
    tree = _parse(sql)
    if tree is None:
        return []
    found = _unit_violations(tree, units or {}, class_ids or {})
    found += _cash_violations(tree, list(cash))
    found += _set_logic_violations(tree, question)
    found += _ratio_violations(tree)
    return list(dict.fromkeys(found))


def _zero_value_violations(tree, zero_value) -> list[str]:
    """R-zero-is-value: 0이 실제 값인 컬럼의 0을 뺀 SELECT."""
    found = []
    for scope in _select_scopes(tree):
        tables = _scope_tables(scope)
        for name in {c.name for c in scope.expression.find_all(exp.Column)}:
            owners = {_owner_of(c, tables) for c in scope.expression.find_all(exp.Column) if c.name == name}
            if any((o, name) in zero_value for o in owners) and _zero_excluded(scope, name):
                table = next(o for o in owners if (o, name) in zero_value)
                info = _catalog_column(table, name)
                label = f"{name}({info['comment']})" if info and info.get("comment") else name
                found.append(f"{table}.{label}의 0은 실제 값입니다. 0인 행을 빼지 말고 계산·비교에 넣으세요.")
    return found


def violations(sql: str, ordered: dict, zero_missing: frozenset = frozenset(),
               zero_value: frozenset = frozenset()) -> list[str]:
    """실행 전에 막을 규칙 위반 목록. 비면 통과.

    zero_missing: {(테이블, 컬럼)} 0이 미입력인 컬럼. zero_value: {(테이블, 컬럼)} 0이 실제 값이라 빼면 안 되는 컬럼.
    """
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
    if zero_value:
        found += _zero_value_violations(tree, zero_value)
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
    while isinstance(node.parent, exp.Cast | exp.TryCast | exp.Paren) or _empty_nullif(node.parent):
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


def _aggregate_exclusions(tree) -> list[str]:
    """집계 함수가 조용히 뺀 결측: COUNT(컬럼)·COUNT(DISTINCT 컬럼)은 결측이 하나라도 있으면,
    AVG·SUM·MIN·MAX는 결측률이 NULL_DISCLOSE_PCT 이상이면 '값이 없는 행 제외'로 밝힌다."""
    lines = []
    for scope in _select_scopes(tree):
        tables = _scope_tables(scope)
        for agg in scope.expression.find_all(exp.AggFunc):
            if agg.find_ancestor(exp.Select) is not scope.expression:
                continue
            _, col = _agg_arg(agg)
            if col is None:
                continue
            info = _catalog_column(_owner_of(col, tables), col.name)
            if info is None or info["null_pct"] <= 0:
                continue
            if isinstance(agg, exp.Count) or info["null_pct"] >= NULL_DISCLOSE_PCT:
                lines.append(f"{info['comment'] or info['name']}: 값이 없는 행 제외")
    return lines


def applied_conditions(sqls: list[str], extra: list[str] = ()) -> str:
    """성공한 SQL들에 들어 있는 공시 대상 조건을 '[적용 조건]' 블록으로 만든다. 없으면 빈 문자열.

    extra: 끝에 붙일 줄(예: --slots의 이름 해석).
    """
    lines = []
    for sql in sqls:
        tree = _parse(sql)
        if tree is None:
            continue
        if settings.as_of_date in sql or _DATE_LITERAL.search(sql):  # 기준일에서 계산한 다른 날짜도 기준일 조건이다
            lines.append(f"기준일: {settings.as_of_date} (데이터 스냅샷)")
        for _, col, owner in _scoped_columns(tree):
            kind = _exclusion_kind(col)
            info = _catalog_column(owner, col.name) if kind else None
            if info is None or (kind == "null" and info["null_pct"] < NULL_DISCLOSE_PCT):
                continue
            label = info["comment"] or info["name"]
            lines.append(f"{label}: 값이 없는 행 제외" if kind == "null" else f"{label}: 0인 행 제외")
        lines += _aggregate_exclusions(tree)
    lines = list(dict.fromkeys([*lines, *extra]))
    return "\n\n[적용 조건]\n" + "\n".join(f"- {line}" for line in lines) if lines else ""
