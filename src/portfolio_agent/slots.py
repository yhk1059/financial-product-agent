"""--slots: 질문의 칸(이름·등급 범위·범주 값)을 코드가 SQL 조각으로 바꾼다 (책 7-1 값만 넘기고 판단은 코드, 7-2, 5-4).

- analyze_question(LLM, graph.py)은 질문에 적힌 원문 단어만 QuestionSlots로 넣는다.
- validate_slots는 원문에 없는 값, 순서 목록에 없는 등급 값을 버린다(지어낸 값 방지).
- resolve는 온톨로지(용어의 canonical·order_values·bundle·order_up, 개념 identity, 관계 via)·스키마 카탈로그·
  읽기 전용 DB 조회(lookup)로 칸을 Resolved로 바꾼다. 처리하지 못한 칸은 skipped로 두고 글에 넣지 않는다.
- render_resolved는 confirmed·candidates·not_found만 '## 확인된 값' 블록 줄로 만든다.
- slot_violations(--enforce)는 확인된 등급·값 묶음과 같은 컬럼에서 다른 값을 쓴 SQL을 찾는다.

DB 조회는 주입한 lookup이 하므로 이 모듈의 함수는 모두 순수 함수다(tests/test_slots.py).
고유명사·문항 id는 코드와 프롬프트에 넣지 않는다(누출 린트 test_no_proper_nouns_in_slot_code).
"""

import re
from typing import Literal, Protocol

import sqlglot
from pydantic import BaseModel, Field
from sqlglot import exp

NAME_TERMS = {"상품": "product_name", "발행기관": "issuer", "구성종목": "holding"}  # 칸 종류 → 이름 용어
MAX_CANDIDATES = 10   # 이름 후보는 짧은 것부터 이만큼
MAX_IDS = 10          # 식별값 목록 상한
MAX_STRIKES = 2       # 칸 검사로 이만큼 막았으면 그 뒤로는 막지 않고 알림만 붙인다
SLOT_ERROR_MARK = "이 조각을 그대로 쓰세요"  # 칸 검사 오류 글에 들어가는 말 (막은 횟수를 셀 때 쓴다)
STATUS_SHOWN = ("confirmed", "candidates", "not_found")
_CORP = re.compile(r"\(주\)|\(유\)|㈜|주식회사")
_GRADE_NO = re.compile(r"\((\d+)등급\)")


# ───────────────────────── 칸 스키마 ─────────────────────────

class NameSlot(BaseModel):
    text: str = Field(description="질문에 쓰인 이름 그대로. 고치거나 늘리지 않는다")
    kind: Literal["상품", "발행기관", "구성종목"] = Field(description="이름이 가리키는 것의 종류")


class GradeSlot(BaseModel):
    term: str = Field(description="등급 종류를 가리키는 원문 말(예: 신용등급, 위험등급, 등급). 없으면 빈 문자열")
    value: str = Field(description="기준이 되는 등급 값 원문")
    direction: Literal["이상", "이하", "초과", "미만", "같음"] = Field(description="원문의 방향어를 다섯 중 하나로 옮긴 것")


class QuestionSlots(BaseModel):
    names: list[NameSlot] = Field(description="상품·발행기관·구성종목 이름. 없으면 빈 목록")
    grades: list[GradeSlot] = Field(description="등급 조건. 없으면 빈 목록")
    values: list[str] = Field(description="범주나 분류로 보이는 원문 단어. 이름은 넣지 않는다. 없으면 빈 목록")


class Lookup(Protocol):
    """DB 조회(db.lookup_values·db.lookup_ids). 테스트는 가짜를 넣는다."""

    def values(self, table: str, column: str, text: str, mode: str) -> list[str]: ...  # exact|normalized|partial

    def ids(self, table: str, column: str, value: str, id_column: str) -> list[str]: ...


# ───────────────────────── 정규화 ─────────────────────────

def squash(s: str) -> str:
    """공백을 없애고 소문자로."""
    return re.sub(r"\s+", "", s).lower()


def normalize_name(s: str) -> str:
    """이름 비교용: 공백·대소문자·법인 표기((주), (유), 주식회사)를 무시한다. db.lookup_values('normalized')와 같은 규칙."""
    return squash(_CORP.sub("", s))


def _grade_key(s: str) -> str:
    """등급 값 비교용: 공백과 '(n등급)'을 뺀다 ('매우높은위험(1등급)' → '매우높은위험')."""
    return squash(_GRADE_NO.sub("", s))


def grade_index(order: list[str], value: str) -> int | None:
    """value의 순서 위치. 공백 무시, 끝의 0 생략형(AA → AA0), 'n등급'(n번째), '이름(n등급)'을 받는다."""
    v = squash(value)
    if m := re.fullmatch(r"(\d+)등급", v):
        n = int(m.group(1))
        return n - 1 if 1 <= n <= len(order) else None
    v = _grade_key(value)
    for i, o in enumerate(order):
        k = _grade_key(o)
        if v == k or (k.endswith("0") and v == k[:-1]):
            return i
    return None


def grade_range(order: list[str], i: int, direction: str) -> list[str]:
    """order의 앞쪽이 "이상"(높은 쪽)일 때 i번째를 기준으로 방향에 맞는 등급들."""
    return {"이상": order[: i + 1], "이하": order[i:], "초과": order[:i], "미만": order[i + 1:],
            "같음": [order[i]]}[direction]


def _quote(values: list[str]) -> str:
    return ", ".join("'" + v.replace("'", "''") + "'" for v in values)


def _in(table: str, column: str, values: list[str]) -> str:
    return f"{table}.{column} = {_quote(values)}" if len(values) == 1 else f"{table}.{column} IN ({_quote(values)})"


def _item(slot: str, text: str, status: str, table: str = "", column: str = "", values=(), fragment: str = "",
          note: str = "", kind: str = "") -> dict:
    return {"slot": slot, "kind": kind, "text": text, "status": status, "table": table, "column": column,
            "values": list(values), "fragment": fragment, "note": note}


# ───────────────────────── 온톨로지 읽기 ─────────────────────────

def _attr(kn, ref: str) -> tuple[str, str, str] | None:
    """'Concept.column' → (테이블, 컬럼, 개념 id). 속성이 아니면 None."""
    a = kn.attributes.get(ref)
    if a is None:
        return None
    return kn.concepts[a["concept_id"]]["table_name"], a["column_name"], a["concept_id"]


def _forms(kn, tid: str) -> set[str]:
    return {squash(kn.items[tid]["label"]), *(squash(f) for f in kn.terms[tid]["forms"])}


def grade_terms(kn) -> dict[str, list[str]]:
    """순서 있는 용어 → "이상" 쪽이 앞인 순서 목록 (order_up last면 뒤집는다)."""
    out = {}
    for tid, t in kn.terms.items():
        if t.get("order_values"):
            order = list(t["order_values"])
            out[tid] = order[::-1] if t.get("order_up") == "last" else order
    return out


# ───────────────────────── 검증 ─────────────────────────

def validate_slots(slots: dict, dialogue: str, kn) -> tuple[dict, list[dict]]:
    """원문(최근 대화)에 없는 이름·값, 어떤 순서 목록에도 없는 등급 값을 버린다 → (남은 칸, dropped 기록)."""
    said = squash(dialogue)
    orders = grade_terms(kn)
    kept = {"names": [], "grades": [], "values": []}
    dropped = []
    for n in slots.get("names", []):
        if n["text"].strip() and squash(n["text"]) in said:
            kept["names"].append(n)
        else:
            dropped.append(_item("name", n["text"], "dropped", kind=n.get("kind", ""), note="원문에 없음"))
    for g in slots.get("grades", []):
        text = f"{g['term']} {g['value']} {g['direction']}".strip()
        if not g["value"].strip() or squash(g["value"]) not in said:
            dropped.append(_item("grade", text, "dropped", note="원문에 없음"))
        elif not any(grade_index(o, g["value"]) is not None for o in orders.values()):
            dropped.append(_item("grade", text, "dropped", note="순서 목록에 없는 값"))
        else:
            kept["grades"].append(g)
    for v in slots.get("values", []):
        if v.strip() and squash(v) in said:
            kept["values"].append(v)
        else:
            dropped.append(_item("value", v, "dropped", note="원문에 없음"))
    return kept, dropped


# ───────────────────────── 처리 ─────────────────────────

def _pick_grade_term(kn, g: dict, orders: dict) -> str | None:
    """term 원문이 가리키는 순서 용어. 모호하거나 값이 그 용어 순서에 없으면 값이 들어 있는 용어로 정한다."""
    by_value = [tid for tid, o in orders.items() if grade_index(o, g["value"]) is not None]
    t = squash(g["term"])
    by_term = [tid for tid in orders if t and t in _forms(kn, tid)]
    if len(by_term) == 1 and by_term[0] in by_value:
        return by_term[0]
    return by_value[0] if len(by_value) == 1 else None


def resolve_grade(kn, catalog: dict, tables: list[str], g: dict) -> list[dict]:
    orders = grade_terms(kn)
    text = f"{g['term']} {g['value']} {g['direction']}".strip()
    tid = _pick_grade_term(kn, g, orders)
    if tid is None:
        return [_item("grade", text, "skipped", note="등급 용어를 정하지 못함")]
    order = orders[tid]
    picked = grade_range(order, grade_index(order, g["value"]), g["direction"])
    keys = {_grade_key(o) for o in picked}
    numbers = {str(order.index(o) + 1) for o in picked}
    label = kn.items[tid]["label"]
    out = []
    for ref, _ in kn.terms[tid]["canonical"]:
        if (a := _attr(kn, ref)) is None or a[0] not in tables:
            continue
        table, column, _ = a
        col = _catalog_column(catalog, table, column)
        stored = list((col or {}).get("values") or {})
        if stored and all(v.isdigit() for v in stored):  # 등급 코드(1~n): n번째 등급
            values = sorted((v for v in stored if v in numbers), key=int)
        elif stored:  # 저장된 표기가 순서 라벨과 다를 수 있어(띄어쓰기, '(n등급)') 같은 것으로 보이는 값을 모두 모은다
            values = [v for v in stored if _grade_key(v) in keys]
        else:
            values = picked
        if not values:
            continue
        note = "등급이 비어 있는 행은 빠짐" if col and col.get("null_pct", 0) > 0 else ""
        out.append(_item("grade", f"{label} '{g['value']}' {g['direction']}", "confirmed", table, column, values,
                         _in(table, column, values), note))
    return out or [_item("grade", text, "skipped", note="선택된 테이블에 그 등급 컬럼이 없음")]


def _catalog_column(catalog: dict, table: str, column: str) -> dict | None:
    return next((c for c in catalog["tables"].get(table, {}).get("columns", []) if c["name"] == column), None)


def resolve_value(kn, catalog: dict, tables: list[str], text: str) -> list[dict]:
    """범주 값: 1) 값 묶음 용어(bundle)의 표현이면 canonical 값 전체, 2) 아니면 카탈로그 범주 값."""
    t = squash(text)
    for tid, term in kn.terms.items():
        if not term.get("bundle") or t not in _forms(kn, tid):
            continue
        out = []
        for ref, values in term["canonical"]:
            if values and (a := _attr(kn, ref)) and a[0] in tables:
                out.append(_item("value", text, "confirmed", a[0], a[1], values, _in(a[0], a[1], list(values))))
        return out or [_item("value", text, "skipped", note="선택된 테이블에 그 묶음 컬럼이 없음")]
    hits = _catalog_hits(catalog, tables, lambda v: squash(v) == t)
    if not hits:  # 원문이 값을 포함한다 (국고채권 → 국고채). 숫자·한 글자 값은 우연히 걸리므로 보지 않는다
        hits = _catalog_hits(catalog, tables,
                             lambda v: len(squash(v)) >= 2 and not squash(v).isdigit() and squash(v) in t)
    if not hits:
        return [_item("value", text, "skipped", note="범주 값에서 찾지 못함")]
    out = []
    for table in dict.fromkeys(h[0] for h in hits):
        mine = [h for h in hits if h[0] == table]
        if len(mine) == 1:
            _, column, value = mine[0]
            out.append(_item("value", text, "confirmed", table, column, [value], _in(table, column, [value])))
        else:
            shown = ", ".join(f"{c} = '{v}'" for _, c, v in mine)
            out.append(_item("value", text, "candidates", table, "", [v for _, _, v in mine], shown))
    return out


def _catalog_hits(catalog: dict, tables: list[str], match) -> list[tuple[str, str, str]]:
    return [(table, c["name"], v) for table in tables for c in catalog["tables"].get(table, {}).get("columns", [])
            for v in (c.get("values") or {}) if match(v)]


def _identity(kn, concept: str) -> str | None:
    ident = kn.concepts[concept].get("identity") or []
    return ident[0] if len(ident) == 1 else None


def _relation_targets(kn, table: str, id_column: str, tables: list[str]) -> list[tuple[str, str]]:
    """식별 컬럼으로 이어지는 다른 선택 테이블의 컬럼 (관계 via: [이쪽 컬럼, 저쪽 컬럼])."""
    out = []
    for r in kn.relations.values():
        src, dst = kn.concepts[r["from_id"]]["table_name"], kn.concepts[r["to_id"]]["table_name"]
        mine, theirs = r["via"][0], r["via"][-1]
        if src == table and dst != table and dst in tables and mine == id_column:
            out.append((dst, theirs))
    return list(dict.fromkeys(out))


def resolve_name(kn, tables: list[str], lookup: Lookup, n: dict) -> list[dict]:
    kind, text = n["kind"], n["text"].strip()
    tid = NAME_TERMS[kind]
    refs = [a for ref, _ in kn.terms.get(tid, {}).get("canonical", []) if (a := _attr(kn, ref)) and a[0] in tables]
    if not refs:
        return [_item("name", text, "skipped", kind=kind, note="선택된 테이블에 그 이름 컬럼이 없음")]
    out = []
    for table, column, concept in refs:
        exact = lookup.values(table, column, text, "exact")
        if exact:
            out.append(_confirmed_name(kn, tables, lookup, kind, text, table, column, concept, exact[0], ""))
            continue
        if kind != "구성종목":  # 구성종목은 정확히 일치만 확정한다(R-name-exact)
            norm = lookup.values(table, column, text, "normalized")
            if len(norm) == 1:
                note = f"이름 해석: '{text}' → '{norm[0]}'"
                out.append(_confirmed_name(kn, tables, lookup, kind, text, table, column, concept, norm[0], note))
                continue
        cands = lookup.values(table, column, text, "partial")[:MAX_CANDIDATES]
        status = "candidates" if cands else "not_found"
        out.append(_item("name", text, status, table, column, cands, kind=kind))
    return out


def _confirmed_name(kn, tables, lookup, kind, text, table, column, concept, value, note) -> dict:
    """확정한 이름 조각: 이름 조건, 상품이면 식별값(개념 identity, MAX_IDS개 이하일 때)과 관계로 이어지는 선택 테이블의 조건."""
    fragment = f"{table}.{column} = {_quote([value])}"
    id_col = _identity(kn, concept) if kind == "상품" else None  # 발행기관의 행 식별값은 기관이 아니라 채권이다
    ids = lookup.ids(table, column, value, id_col) if id_col else []
    if 0 < len(ids) <= MAX_IDS:
        fragment += f" ({_in(table, id_col, ids).split('.', 1)[1]})"
        for dst, col in _relation_targets(kn, table, id_col, tables):
            fragment += f", 구성종목은 {_in(dst, col, ids)}"
    return _item("name", text, "confirmed", table, column, [value], fragment, note, kind=kind)


def resolve(slots: dict, kn, catalog: dict, lookup: Lookup, tables: list[str], dialogue: str = "") -> list[dict]:
    """칸 → Resolved 목록 (dropped·skipped 포함, 기록용). dialogue가 있으면 원문 검증을 먼저 한다."""
    kept, out = validate_slots(slots, dialogue, kn) if dialogue else (slots, [])
    for n in kept.get("names", []):
        out += resolve_name(kn, tables, lookup, n)
    for g in kept.get("grades", []):
        out += resolve_grade(kn, catalog, tables, g)
    for v in kept.get("values", []):
        out += resolve_value(kn, catalog, tables, v)
    return out


# ───────────────────────── 렌더링 ─────────────────────────

def _line(r: dict) -> str:
    if r["slot"] == "grade":
        return f"{r['text']} → {r['fragment']}" + (f" ({r['note']})" if r["note"] else "")
    if r["slot"] == "value":
        if r["status"] == "candidates":
            return f"범주 '{r['text']}' → 맞는 값이 여러 개({r['table']}): {r['fragment']}. 질문이 가리키는 것을 고른다"
        return f"범주 '{r['text']}' → {r['fragment']}"
    kind, text = r["kind"], r["text"]
    if r["status"] == "confirmed":
        return f"{kind} '{text}' → {r['fragment']}" + (f" ({r['note']}, 답변에 밝힐 것)" if r["note"] else "")
    where = f"{r['table']}.{r['column']}"
    if r["status"] == "not_found":
        return f"{kind} '{text}' → {where}의 저장된 이름에서 찾지 못함(표기가 다를 수 있음)"
    shown = _quote(r["values"])
    if kind == "구성종목":
        return (f"구성종목 '{text}' → {where}에 정확히 일치하는 종목명 없음. 비슷한 표기(표기 확인용이며 편입 판정에 "
                f"쓰지 않는다): {shown}")
    return (f"{kind} '{text}' → {where}에 정확히 일치하는 값 없음. 저장된 이름 후보: {shown}. 질문이 가리키는 것이 "
            "후보 중 하나면 그 값으로 정확히 일치시켜 조회하고, 해석을 답변에 밝힌다. 부분 일치(LIKE) 결과를 그대로 "
            "집계하지 않는다")


def render_resolved(resolved: list[dict]) -> str:
    """confirmed·candidates·not_found만 블록 줄로. 없으면 빈 문자열(칸 없는 프롬프트와 같다)."""
    return "\n".join(f"- {_line(r)}" for r in resolved if r["status"] in STATUS_SHOWN)


def name_notes(resolved: list[dict], sqls: list[str]) -> list[str]:
    """답변 [적용 조건]에 붙일 이름 해석: 정규화로 확정한 이름, 또는 이번 턴 성공 SQL에 쓰인 후보 값."""
    text = "\n".join(sqls)
    out = []
    for r in resolved:
        if r["slot"] != "name":
            continue
        if r["status"] == "confirmed" and r["note"] and r["values"][0] in text:
            out.append(r["note"])
        elif r["status"] == "candidates" and r["kind"] != "구성종목":
            out += [f"이름 해석: '{r['text']}' → '{v}'" for v in r["values"] if f"'{v}'" in text]
    return list(dict.fromkeys(out))


# ───────────────────────── enforce: 확인된 값과 다른 SQL ─────────────────────────

def _sql_sets(tree, column: str) -> list[tuple[str, set[str] | None]]:
    """SQL에서 column에 건 조건 → [(종류, 값 집합)]. 종류: 'set'(= / IN 문자열), 'range'(대소 비교)."""
    out = []
    for node in tree.find_all(exp.EQ, exp.In, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between):
        col = node.this
        while isinstance(col, exp.Cast | exp.Paren):
            col = col.this
        if not isinstance(col, exp.Column) or col.name != column:
            continue
        if isinstance(node, exp.EQ):
            if isinstance(node.expression, exp.Literal):
                out.append(("set", {node.expression.this}))
        elif isinstance(node, exp.In):
            vals = [v for v in node.expressions if isinstance(v, exp.Literal)]
            if vals and len(vals) == len(node.expressions):
                out.append(("set", {v.this for v in vals}))
        else:
            out.append(("range", None))
    return out


def slot_violations(sql: str, resolved: list[dict]) -> list[str]:
    """확인된 등급과 값 집합이 다르거나(대소 비교 포함), 값 묶음의 일부만 쓴 SQL → 오류 글. 비면 통과.

    컬럼 이름으로만 맞춘다(같은 이름의 컬럼이 다른 테이블에도 있으면 그 테이블이 쿼리에 있을 때만 본다).
    """
    try:
        tree = sqlglot.parse_one(sql, read="postgres")
    except (sqlglot.errors.ParseError, sqlglot.errors.TokenError):
        return []
    tables = {t.name.lower() for t in tree.find_all(exp.Table)}
    found = []
    for r in resolved:
        if r["status"] != "confirmed" or r["slot"] not in ("grade", "value") or r["table"] not in tables:
            continue
        want = set(r["values"])
        for kind, got in _sql_sets(tree, r["column"]):
            if r["slot"] == "grade" and (kind == "range" or got != want):
                found.append(f"질문의 {r['text']}은(는) {r['fragment']}입니다. {SLOT_ERROR_MARK}.")
            elif r["slot"] == "value" and kind == "set" and got < want:
                found.append(f"'{r['text']}'은(는) {r['fragment']}입니다(값 일부만 쓰면 안 됩니다). {SLOT_ERROR_MARK}.")
    return list(dict.fromkeys(found))
