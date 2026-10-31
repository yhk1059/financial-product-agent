"""온톨로지 항목을 프롬프트용 글로 조립한다 (책 7-2 근거 확장·컨텍스트 조립, 7-3 컨텍스트 설계).

- B층: 항상 넣는 공통 규칙과 기준일 용어 → {rules}의 '공통 규칙'
- C층: 질문으로 검색한 용어·규칙 중 선택된 테이블과 관계있는 것 + depends_on 한 칸 확장 → {rules}의 '질문 관련 용어·규칙'
- A층: 선택된 테이블의 개념·속성·관계와 속성 전용 규칙 → 스키마 카드 뒤에 붙이는 '테이블 의미'

읽기(load_knowledge)만 DB를 쓰고, 나머지는 순수 함수다. 추적용 메타데이터(motivated_by, evidence,
generalized_from)와 혼동 주의(confusable_with)는 넣지 않는다. 에이전트는 graph.retrieve_knowledge에서
get_knowledge()와 build_context()를 부른다(--ontology ab|abc).
"""

from dataclasses import dataclass, field
from functools import lru_cache

from . import db
from .retrieval import LAYER_A_RULES, LAYER_B_ITEMS, label_forms


@dataclass
class Knowledge:
    items: dict[str, dict]       # id → kind, label, definition, note
    concepts: dict[str, dict]    # id → table_name, grain, identity, parent_id, discriminator, ...
    attributes: dict[str, dict]  # 'Concept.column' → concept_id, column_name, data_type, unit, ...
    relations: dict[str, dict]   # id → from_id, to_id, cardinality, via, derived
    terms: dict[str, dict]       # id → scope_tables, in_data, order_values, bundle, order_up, forms, canonical
    rules: dict[str, dict]       # id → rule_type, when_text, then_text, disclose, targets, depends_on, overrides


@dataclass
class Context:
    rules_text: str   # 프롬프트 {rules} 자리
    tables_text: str  # 스키마 카드 뒤에 붙이는 A층 블록
    layers: dict[str, list[str]] = field(default_factory=dict)  # A·B·C·expanded 항목 id (보고용)
    oos_text: str = ""  # C층에 든 '데이터에 없는 개념'(in_data false) 줄. 프롬프트 끝 '## 이 데이터에 없는 정보'에 들어간다


def _rows(cur, sql: str) -> list[dict]:
    cur.execute(sql)
    names = [d.name for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def load_knowledge() -> Knowledge:
    """knowledge 스키마를 읽기 계정으로 읽는다. item_motivation(골든 추적)은 읽지 않는다."""
    with db.connect() as conn, conn.cursor() as cur:
        items = {r["id"]: r for r in _rows(cur, "SELECT id, kind, label, definition, note FROM knowledge.item")}
        concepts = {r["id"]: r for r in _rows(cur, "SELECT * FROM knowledge.concept")}
        attributes = {r["id"]: r for r in _rows(cur, "SELECT * FROM knowledge.attribute")}
        relations = {r["id"]: r for r in _rows(cur, "SELECT * FROM knowledge.relation")}
        terms = {r["id"]: {**r, "forms": [], "canonical": []}
                 for r in _rows(cur, "SELECT id, scope_tables, in_data, order_values, bundle, order_up FROM knowledge.term")}
        for r in _rows(cur, "SELECT term_id, form FROM knowledge.term_surface_form ORDER BY term_id, form"):
            terms[r["term_id"]]["forms"].append(r["form"])
        for r in _rows(cur, "SELECT term_id, ref_id, ref_values FROM knowledge.term_canonical ORDER BY term_id, seq"):
            terms[r["term_id"]]["canonical"].append((r["ref_id"], r["ref_values"]))
        rules = {r["id"]: {**r, "targets": [], "depends_on": [], "overrides": []}
                 for r in _rows(cur, "SELECT * FROM knowledge.rule")}
        for r in _rows(cur, "SELECT rule_id, target_id FROM knowledge.rule_applies_to ORDER BY rule_id, target_id"):
            rules[r["rule_id"]]["targets"].append(r["target_id"])
        for r in _rows(cur, "SELECT rule_id, target_rule_id, link_type FROM knowledge.rule_link"
                            " ORDER BY rule_id, target_rule_id"):
            rules[r["rule_id"]][r["link_type"]].append(r["target_rule_id"])
    return Knowledge(items, concepts, attributes, relations, terms, rules)


@lru_cache
def get_knowledge() -> Knowledge:
    """에이전트용: knowledge 스키마를 프로세스당 한 번만 읽는다. 다시 읽으려면 get_knowledge.cache_clear()."""
    return load_knowledge()


# ───────────────────────── 테이블 판정 ─────────────────────────

def _concept_of(kn: Knowledge, ref_id: str) -> str | None:
    return kn.attributes[ref_id]["concept_id"] if ref_id in kn.attributes else ref_id if ref_id in kn.concepts else None


def item_tables(kn: Knowledge, item_id: str) -> set[str]:
    """항목이 걸친 테이블. 용어는 scope_tables, 규칙은 적용 대상의 테이블을 합친다.

    규칙이 용어와 함께 그 용어 정본의 한 테이블 버전(같은 개념의 속성·개념)을 대상으로 삼으면, 그 용어는
    테이블을 넓히지 않는다. 예: R-sale-fund(on_sale + FundClass.sale_yn)는 펀드 테이블만.
    R-name-exact(product_name + Holding.component_name)는 Holding이 정본에 없어 product_name 범위 전체.
    """
    if item_id in kn.concepts:
        return {kn.concepts[item_id]["table_name"]}
    if item_id in kn.attributes:
        return {kn.concepts[kn.attributes[item_id]["concept_id"]]["table_name"]}
    if item_id in kn.terms:
        return set(kn.terms[item_id]["scope_tables"])
    if item_id in kn.rules:
        targets = kn.rules[item_id]["targets"]
        own = {_concept_of(kn, t) for t in targets if t not in kn.terms}
        tables = set().union(*(item_tables(kn, t) for t in targets if t not in kn.terms))
        for t in targets:
            if t in kn.terms and not own & {_concept_of(kn, ref) for ref, _ in kn.terms[t]["canonical"]}:
                tables |= item_tables(kn, t)
        return tables
    return set()


def is_related(kn: Knowledge, item_id: str, tables: list[str]) -> bool:
    """선택된 테이블과 관계있는가. 데이터에 없는 개념(in_data false)과 그 규칙은 거절 근거라 늘 관계있다."""
    if item_id in kn.terms and not kn.terms[item_id]["in_data"]:
        return True
    if item_id in kn.rules and any(t in kn.terms and not kn.terms[t]["in_data"] for t in kn.rules[item_id]["targets"]):
        return True
    return bool(item_tables(kn, item_id) & set(tables))


# ───────────────────────── 그래프 확장 ─────────────────────────

def expand(kn: Knowledge, c_ids: list[str], fixed: set[str]) -> list[str]:
    """C층 규칙의 depends_on 대상을 한 칸 붙인다. 이미 A·B·C층에 있으면 건너뛴다.
    반대 방향과 overrides는 따라가지 않는다 (예외는 질문 신호가 있을 때만 필요하므로 검색에 맡긴다)."""
    have = set(c_ids) | fixed
    added = []
    for i in c_ids:
        for dep in kn.rules.get(i, {}).get("depends_on", []):
            if dep not in have:
                have.add(dep)
                added.append(dep)
    return added


# ───────────────────────── 렌더링 ─────────────────────────

def _values(values: list[str] | None) -> str:
    if not values:
        return ""
    quoted = [f"'{v}'" for v in values]
    return f"={quoted[0]}" if len(quoted) == 1 else f" IN ({', '.join(quoted)})"


def ref_text(kn: Knowledge, ref_id: str, values: list[str] | None = None) -> str:
    """정본 참조를 SQL에서 쓰는 이름으로 바꾼다. 'BondLot.buyable_quantity' → 'prbd01n001.buyable_quantity'."""
    if ref_id in kn.attributes:
        a = kn.attributes[ref_id]
        return f"{kn.concepts[a['concept_id']]['table_name']}.{a['column_name']}{_values(values)}"
    c = kn.concepts[ref_id]
    if c["parent_id"] and c["discriminator_value"]:
        parent = kn.concepts[c["parent_id"]]
        return f"{c['table_name']}.{parent['discriminator']}='{c['discriminator_value']}'"
    if c["derived_from"]:
        return f"{c['table_name']}의 {kn.items[ref_id]['label']}({c['grain']})"
    return c["table_name"]


def _ref_in(kn: Knowledge, ref_id: str, tables: list[str]) -> bool:
    return bool(item_tables(kn, ref_id) & set(tables))


def render_term(kn: Knowledge, term_id: str, tables: list[str]) -> str:
    """- 라벨 [같은 말: …]: 정의 → 테이블.컬럼='값', … | 순서(앞부터): … | 참고: …"""
    t, item = kn.terms[term_id], kn.items[term_id]
    forms = [f for f in t["forms"] if f not in label_forms(item["label"])]
    line = f"- {item['label']}" + (f" [같은 말: {', '.join(forms)}]" if forms else "") + f": {item['definition']}"
    if not t["in_data"]:
        line += " (이 데이터에 없음)"
    else:
        refs = [ref_text(kn, ref, vals) for ref, vals in t["canonical"] if _ref_in(kn, ref, tables)]
        if refs:
            line += f" → {', '.join(refs)}"
    if t["order_values"]:
        line += f" | 순서(앞부터): {', '.join(t['order_values'])}"
    if item["note"]:
        line += f" | 참고: {item['note']}"
    return line


def render_rule(kn: Knowledge, rule_id: str) -> str:
    """- [유형] 언제 → 할 일 (기본 처리보다 우선) / 답변에 밝힐 것: …"""
    r = kn.rules[rule_id]
    line = f"- [{r['rule_type']}] {r['when_text']} → {r['then_text']}"
    for o in r["overrides"]:
        line += f" (이 경우 기본 처리 「{kn.rules[o]['then_text']}」보다 우선)"
    if r["disclose"]:
        line += f" / 답변에 밝힐 것: {r['disclose']}"
    return line


def _render(kn: Knowledge, ids: list[str], tables: list[str]) -> list[str]:
    return [render_term(kn, i, tables) if i in kn.terms else render_rule(kn, i) for i in ids]


def render_rules(kn: Knowledge, tables: list[str], b_ids: list[str], c_ids: list[str]) -> str:
    """{rules} 자리: 공통 규칙(B층) / 질문 관련 용어 / 질문 관련 규칙(C층 + 확장)."""
    blocks = ["### 공통 규칙 (항상 적용)\n" + "\n".join(_render(kn, b_ids, tables))]
    c_terms = [i for i in c_ids if i in kn.terms]
    c_rules = [i for i in c_ids if i in kn.rules]
    if c_terms:
        blocks.append("### 질문 관련 용어\n" + "\n".join(_render(kn, c_terms, tables)))
    if c_rules:
        blocks.append("### 질문 관련 규칙\n" + "\n".join(_render(kn, c_rules, tables)))
    return "\n\n".join(blocks)


def _render_concept(kn: Knowledge, cid: str) -> str:
    c, item = kn.concepts[cid], kn.items[cid]
    line = f"- {item['label']}: {item['definition']}"
    if c["parent_id"] and c["discriminator_value"]:
        line += f" ({kn.concepts[c['parent_id']]['discriminator']}='{c['discriminator_value']}')"
    if c["grain"]:
        line += f" 1행 = {c['grain']}" if not c["derived_from"] else f" 단위 = {c['grain']}"
    if c["identity"]:
        line += f", 식별키 {', '.join(c['identity'])}"
    return line


def _render_attribute(kn: Knowledge, aid: str) -> str:
    a, item = kn.attributes[aid], kn.items[aid]
    line = f"- {a['column_name']} {item['label']}: {item['definition']}"
    if a["data_type"] == "text(숫자)":
        line += " [text로 저장된 숫자]"
    if a["unit"]:
        line += f" [단위 {a['unit']}]"
    if a["missing_means"]:
        line += f" [NULL = {a['missing_means']}]"
    if a["zero_means"]:
        line += f" [0 = {a['zero_means']}]"
    if item["note"]:
        line += f" [주의: {item['note']}]"
    return line


def _render_relation(kn: Knowledge, rid: str) -> str:
    r, item = kn.relations[rid], kn.items[rid]
    line = (f"- {kn.items[r['from_id']]['label']} → {kn.items[r['to_id']]['label']}: {item['label']}"
            f" ({r['cardinality']}, 연결 컬럼 {', '.join(r['via'])})")
    if r["derived"]:
        line += f" [파생: {r['derived']}]"
    if item["note"]:
        line += f" [주의: {item['note']}]"
    return line


def a_layer_ids(kn: Knowledge, tables: list[str]) -> list[str]:
    """A층 항목: 선택된 테이블의 개념·속성·관계, 적용 대상이 그 테이블에 있는 속성 전용 규칙."""
    ids = [cid for cid in kn.concepts if kn.concepts[cid]["table_name"] in tables]
    ids += [aid for aid in kn.attributes if _ref_in(kn, aid, tables)]
    ids += [rid for rid, r in kn.relations.items() if _ref_in(kn, r["from_id"], tables) or _ref_in(kn, r["to_id"], tables)]
    ids += [rid for rid in sorted(LAYER_A_RULES) if is_related(kn, rid, tables)]
    return ids


def render_tables(kn: Knowledge, tables: list[str]) -> str:
    """A층: 테이블마다 개념·속성 블록, 그 뒤에 관계와 속성 규칙."""
    ids = set(a_layer_ids(kn, tables))
    blocks = []
    for table in tables:
        concepts = [c for c in kn.concepts if c in ids and kn.concepts[c]["table_name"] == table]
        attrs = [a for a in kn.attributes if a in ids and _ref_in(kn, a, [table])]
        if not concepts and not attrs:
            continue
        lines = [_render_concept(kn, c) for c in concepts]
        if attrs:
            lines += ["주요 컬럼의 뜻:"] + [_render_attribute(kn, a) for a in attrs]
        blocks.append(f"### {table} 의미\n" + "\n".join(lines))
    rels = [r for r in kn.relations if r in ids]
    if rels:
        blocks.append("### 테이블 사이 관계\n" + "\n".join(_render_relation(kn, r) for r in rels))
    a_rules = [r for r in sorted(LAYER_A_RULES) if r in ids]
    if a_rules:
        blocks.append("### 컬럼 값 처리 규칙\n" + "\n".join(render_rule(kn, r) for r in a_rules))
    return "\n\n".join(blocks)


def build_context(kn: Knowledge, tables: list[str], c_ids: list[str]) -> Context:
    """검색 결과(c_ids, 순위순)와 선택된 테이블로 프롬프트에 넣을 글을 만든다.

    C층은 선택된 테이블과 무관한 항목을 먼저 빼고(is_related) 남은 것에서만 확장한다.
    빈자리를 다음 순위로 채우지 않는다. 테이블 선택이 틀리면 필요한 항목도 빠진다 (2026-10-19 결정).
    """
    b_ids = sorted(i for i in LAYER_B_ITEMS if i in kn.items)
    b_ids = [i for i in b_ids if i in kn.rules] + [i for i in b_ids if i in kn.terms]
    a_ids = a_layer_ids(kn, tables)
    fixed = set(LAYER_B_ITEMS) | set(LAYER_A_RULES) | set(a_ids)
    c_ids = [i for i in c_ids if i not in fixed]
    dropped = [i for i in c_ids if not is_related(kn, i, tables)]
    c_ids = [i for i in c_ids if i not in dropped]
    added = expand(kn, c_ids, fixed)
    return Context(
        rules_text=render_rules(kn, tables, b_ids, c_ids + added),
        tables_text=render_tables(kn, tables),
        layers={"A": a_ids, "B": b_ids, "C": c_ids, "expanded": added, "dropped": dropped},
        oos_text=render_oos(kn, c_ids + added, tables),
    )


def render_oos(kn: Knowledge, ids: list[str], tables: list[str]) -> str:
    """ids 중 데이터에 없는 개념(in_data false)인 용어를 '- 라벨 [같은 말: …]: 정의' 줄로. 없으면 빈 문자열.

    용어에 scope_tables가 있으면 선택된 테이블과 겹칠 때만 넣는다(편입 비중은 구성종목 테이블 질문에서만.
    ETN '레버리지 비중' 같은 다른 뜻의 비중 질문에 거절을 유도하지 않게).
    """
    lines = []
    for i in ids:
        t = kn.terms.get(i)
        if t is None or t["in_data"] or (t["scope_tables"] and not set(t["scope_tables"]) & set(tables)):
            continue
        item = kn.items[i]
        forms = [f for f in t["forms"] if f not in label_forms(item["label"])]
        lines.append(f"- {item['label']}" + (f" [같은 말: {', '.join(forms)}]" if forms else "") + f": {item['definition']}")
    return "\n".join(lines)
