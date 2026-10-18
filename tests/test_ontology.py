"""ontology YAML(model·glossary·rules·review_log) 구조·실재성·누수·커버리지 검사 (DB·LLM 불필요)."""

import re
from fnmatch import fnmatch
from functools import cache

import pytest
import sqlglot
import yaml
from sqlglot import exp

from portfolio_agent.config import ALLOWED_TABLES, PROJECT_ROOT
from portfolio_agent.schema_catalog import load_catalog

ONTOLOGY = PROJECT_ROOT / "ontology"
GOLDEN = PROJECT_ROOT / "golden"

MODEL = yaml.safe_load((ONTOLOGY / "model.yaml").read_text())
GLOSSARY = yaml.safe_load((ONTOLOGY / "glossary.yaml").read_text())["terms"]
RULES = yaml.safe_load((ONTOLOGY / "rules.yaml").read_text())["rules"]
REVIEW = yaml.safe_load((ONTOLOGY / "review_log.yaml").read_text())
QUESTIONS = yaml.safe_load((GOLDEN / "questions_v2.yaml").read_text())
HOLDOUT = yaml.safe_load((GOLDEN / "holdout_v1.yaml").read_text())
POLICIES = set(re.findall(r"\*\*(P-[A-Z]+)\*\*", (GOLDEN / "design.md").read_text()))
WORKSHEET = (GOLDEN / "analysis" / "extraction_worksheet.md").read_text()

CONCEPTS = {c["id"]: c for c in MODEL["concepts"]}
RELATIONS = {r["id"]: r for r in MODEL["relations"]}
TERMS = {t["id"]: t for t in GLOSSARY}
RULE_IDS = {r["id"]: r for r in RULES}
DECISIONS = {d["id"] for d in REVIEW["decisions"]}
CONFLICTS = {c["id"] for c in REVIEW["conflicts"]}
PROFILING = {p["id"]: p for p in REVIEW["profiling"]}
GOLDEN_IDS = {q["id"] for q in QUESTIONS}
RULE_TYPES = {"조건", "시점", "예외", "계산"}
DESIGN_DECISIONS = {"G1", "G2", "S1", "S2", "S3", "S4"}

# 골든 질문에만 나오는 고유명사: examples 밖에 나오면 문항 전용 지식이 섞인 것이다
GOLDEN_PROPER_NOUNS = ["에코프로", "KODEX 200", "TIGER 200", "중소기업은행", "기업은행", "삼성전자", "SK하이닉스", "마이다스"]
# 홀드아웃 질문의 고유명사: ontology 어디에도 나오면 안 된다(홀드아웃 → 사전 누수)
HOLDOUT_PROPER_NOUNS = ["KODEX 레버리지", "한전", "한국전력", "대한전선", "현대차", "애플", "Apple"]
QUESTION_ID = re.compile(r"(?<![A-Za-z0-9])(C\d|H)-\d\d(?!\d)")  # 한글이 바로 붙어도 잡는다
META_KEYS = {"motivated_by"}

# 홀드아웃 tests_knowledge(순서대로) → 그 지식을 해결하는 ontology 항목
HOLDOUT_COVERAGE = {
    "H-01": [["etp", "R-product-group"], ["asset_class"]],
    "H-02": [["Fund", "R-fund-unit-representative"], ["on_sale", "R-sale-fund"], ["fund_type"]],
    "H-03": [["DomesticListedProduct.cu_charge_rt", "R-zero-missing"], ["R-text-numeric"], ["R-null-exclude"]],
    "H-04": [["issuer", "R-name-resolve"], ["bond_item", "R-bond-unit-item"]],
    "H-05": [["product_name"], ["net_assets"]],
    "H-06": [["net_assets", "R-zero-missing"], ["R-topn-nulls"], ["investment_region", "asset_class"]],
    "H-07": [["R-credit-grade-order"], ["R-bond-unit-item"], ["R-scope-default"]],
    "H-08": [["risk_grade", "R-risk-grade-order"], ["R-risk-grade-order"]],
    "H-09": [["mmf"], ["R-sale-fund"], ["R-null-exclude"]],
    "H-10": [["R-sale-bond"], ["R-bond-unit-item"]],
    "H-11": [["R-zero-is-value"], ["etn", "R-product-group"]],
    "H-12": [["R-sale-domestic-listed"], ["etf_holds", "R-holding-etf-count"], ["R-name-exact"]],
    "H-13": [["R-as-of-date", "R-period-window"], ["R-bond-unit-item"]],
    "H-14": [["etf_holds", "holding"]],
}


# ───────────────────────── 참조 해석 ─────────────────────────
@cache
def catalog() -> dict[str, dict[str, dict]]:
    return {t: {c["name"]: c for c in v["columns"]} for t, v in load_catalog()["tables"].items()}


def catalog_columns() -> set[str]:
    return {c for cols in catalog().values() for c in cols}


def attributes(concept_id: str) -> dict[str, dict]:
    """개념의 속성(상위 개념 속성 상속 포함)."""
    c = CONCEPTS[concept_id]
    inherited = attributes(c["parent"]) if c.get("parent") else {}
    return {**inherited, **{a["column"]: a for a in c.get("attributes", [])}}


def model_ref_ok(ref: str) -> bool:
    if ref in CONCEPTS or ref in RELATIONS:
        return True
    concept, _, column = ref.partition(".")
    return concept in CONCEPTS and column in attributes(concept)


def any_ref_ok(ref: str) -> bool:
    """개념·관계·속성·용어·규칙 id 또는 카탈로그 컬럼(bare, table.column)."""
    if model_ref_ok(ref) or ref in TERMS or ref in RULE_IDS:
        return True
    table, _, column = ref.partition(".")
    if column:
        return table in catalog() and column in catalog()[table]
    return ref in catalog_columns()


def ref_values(ref: str) -> set[str] | None:
    """속성의 값 목록: 카탈로그 값 목록, 없으면 profiling의 verified_values."""
    concept, _, column = ref.partition(".")
    info = catalog()[CONCEPTS[concept]["table"]][column]
    if "values" in info:
        return set(info["values"])
    verified = [set(v[ref]) for p in PROFILING.values() for k, v in p.items() if k == "verified_values" and ref in v]
    return set.union(*verified) if verified else None


def texts(node, skip=META_KEYS):
    """항목 안의 문자열을 모두 모은다(메타데이터 키 제외)."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for k, v in node.items():
            if k not in skip:
                yield from texts(v, skip)
    elif isinstance(node, list):
        for v in node:
            yield from texts(v, skip)


def all_items():
    """(파일, 항목 id, 항목) — motivated_by를 가져야 하는 모든 항목."""
    for c in MODEL["concepts"]:
        yield "model", c["id"], c
        for a in c.get("attributes", []):
            yield "model", f"{c['id']}.{a['column']}", a
    for r in MODEL["relations"]:
        yield "model", r["id"], r
    for t in GLOSSARY:
        yield "glossary", t["id"], t
    for r in RULES:
        yield "rules", r["id"], r


ITEMS = list(all_items())


# ───────────────────────── 구조 ─────────────────────────
def test_ids_unique():
    ids = [i for _, i, _ in ITEMS if "." not in i]
    dup = {i for i in ids if ids.count(i) > 1}
    assert not dup, dup


@pytest.mark.parametrize("c", MODEL["concepts"], ids=lambda c: c["id"])
def test_concept_fields(c):
    assert re.fullmatch(r"[A-Z][A-Za-z]+", c["id"])
    assert {"label", "definition", "table"} <= c.keys()
    assert c["table"] in ALLOWED_TABLES
    if c.get("parent"):
        assert c["parent"] in CONCEPTS and CONCEPTS[c["parent"]]["table"] == c["table"]
        assert CONCEPTS[c["parent"]].get("discriminator") and c.get("discriminator_value")
    else:
        assert c.get("grain") and c.get("identity")
    if c.get("derived_from"):
        assert c["derived_from"] in CONCEPTS


@pytest.mark.parametrize("r", MODEL["relations"], ids=lambda r: r["id"])
def test_relation_endpoints(r):
    assert r["from"] in CONCEPTS and r["to"] in CONCEPTS, (r["from"], r["to"])
    assert re.fullmatch(r"(1|n)\.\.(1|n)", r["cardinality"])
    tables = {CONCEPTS[r["from"]]["table"], CONCEPTS[r["to"]]["table"]}
    for col in r["via"]:
        assert any(col in catalog()[t] for t in tables), col


@pytest.mark.parametrize("t", GLOSSARY, ids=lambda t: t["id"])
def test_glossary_fields(t):
    assert re.fullmatch(r"[a-z][a-z0-9_]*", t["id"])
    assert {"label", "definition", "canonical", "surface_forms", "confusable_with"} <= t.keys()
    assert set(t.get("scope_tables", [])) <= set(ALLOWED_TABLES)
    if t.get("in_data") is False:
        assert t["canonical"] == []
    else:
        assert t["canonical"]
    for c in t["canonical"]:
        ref = c["ref"] if isinstance(c, dict) else c
        assert model_ref_ok(ref), ref
    for c in t["confusable_with"]:
        assert c["target"] and c["why"]
        if re.fullmatch(r"[A-Za-z_][\w.]*", c["target"]):  # 식별자 모양이면 반드시 해석되어야 한다
            assert any_ref_ok(c["target"]), c["target"]


@pytest.mark.parametrize("r", RULES, ids=lambda r: r["id"])
def test_rule_fields(r):
    assert r["id"].startswith("R-")
    assert r["type"] in RULE_TYPES
    assert r["when"] and r["then"] and r["evidence"]
    for ref in r["applies_to"]:
        assert model_ref_ok(ref) or ref in TERMS, ref
    for ref in [*r.get("overrides", []), *r.get("depends_on", [])]:
        assert ref in RULE_IDS and ref != r["id"], ref


def _evidence_ok(e: str) -> bool:
    if e in POLICIES or e in DESIGN_DECISIONS:
        return True
    kind, _, key = e.partition(":")
    if kind == "profiling":
        return key in PROFILING
    if kind == "schema_catalog":
        return key in catalog_columns()
    return kind.startswith("design§")


@pytest.mark.parametrize("item", [t for t in GLOSSARY] + RULES, ids=lambda i: i["id"])
def test_evidence_resolves(item):
    bad = [e for e in item.get("evidence", []) if not _evidence_ok(e)]
    assert not bad, bad


def test_review_log_links():
    item_ids = {i for _, i, _ in ITEMS} | DECISIONS | CONFLICTS | POLICIES
    for p in REVIEW["profiling"]:
        for ref in p["used_by"]:
            assert ref in item_ids, (p["id"], ref)
    for q in REVIEW["open_questions"]:
        assert set(q["related"]) <= DECISIONS, q["id"]
    assert {d["type"] for d in REVIEW["decisions"]} <= {"merge", "drop", "reverse", "derive", "demote", "keep_low_confidence"}


DEFINED = [(s, i, x) for s, i, x in ITEMS if s == "glossary" or i not in RELATIONS and s == "model"]


@pytest.mark.parametrize("src,item_id,item", DEFINED, ids=lambda x: x if isinstance(x, str) else "")
def test_definition_one_sentence(src, item_id, item):
    """정의는 한 문장이다(한 문장으로 안 써지면 개념이 둘 이상 섞인 것, 책 4-1)."""
    d = item.get("definition")
    assert d, f"{src}:{item_id} 정의 없음"
    assert len(re.findall(r"[.!?](?=\s|$)", d)) <= 1, d


# ───────────────────────── 실재성 ─────────────────────────
@pytest.mark.parametrize("c", MODEL["concepts"], ids=lambda c: c["id"])
def test_columns_exist(c):
    cols = catalog()[c["table"]]
    missing = [x for x in [*c.get("identity", []), c.get("discriminator")] if x and x not in cols]
    missing += [a["column"] for a in c.get("attributes", []) if a["column"] not in cols]
    assert not missing, missing
    if c.get("discriminator_value"):
        assert c["discriminator_value"] in ref_values(f"{c['parent']}.{CONCEPTS[c['parent']]['discriminator']}")


@pytest.mark.parametrize("t", [t for t in GLOSSARY if any(isinstance(c, dict) for c in t["canonical"])], ids=lambda t: t["id"])
def test_canonical_values_exist(t):
    for c in t["canonical"]:
        if isinstance(c, dict):
            known = ref_values(c["ref"])
            assert known is not None, f"{c['ref']}: 값 목록도 profiling 기록도 없음"
            assert set(c["values"]) <= known, set(c["values"]) - known


def test_grade_orders_exist():
    crd = ref_values("Bond.crd_grd")
    assert set(TERMS["credit_rating"]["order"]) == crd
    risk = ref_values("FundClass.zrin_fd_ivst_risk_grd_nm")
    assert set(TERMS["risk_grade"]["order"]) <= risk
    # 국내 상장상품·채권은 띄어쓰기 없는 이름에 (n등급)이 붙는다: 순서 n이 이름과 맞아야 한다
    for ref in ("DomesticListedProduct.pd_risk_nm", "Bond.pd_risk_nm"):
        for v in ref_values(ref) - {"해당없음"}:
            name, n = re.fullmatch(r"(.+)\((\d)등급\)", v).groups()
            assert TERMS["risk_grade"]["order"][int(n) - 1].replace(" ", "") == name, v


# ───────────────────────── 누수 방지 ─────────────────────────
def question_id_leaks(item) -> list[str]:
    return [s for s in texts(item) if QUESTION_ID.search(s)]


def proper_noun_leaks(item, nouns, skip=META_KEYS) -> list[str]:
    return [n for s in texts(item, skip) for n in nouns if n in s]


def _norm(s: str) -> str:
    s = re.sub(r"\b[a-z_]\w*\.(?=[a-z_])", "", s.lower())  # 테이블 별칭(p., h.) 제거
    return re.sub(r"[\s\"]", "", s)


@cache
def gold_predicates() -> dict[str, list[str]]:
    out = {}
    for q in QUESTIONS:
        if not q.get("gold_sql"):
            continue
        preds = []
        for node in sqlglot.parse_one(q["gold_sql"], read="postgres").find_all(exp.Where, exp.Having):
            stack = [node.this]
            while stack:
                e = stack.pop()
                if isinstance(e, exp.And):
                    stack += [e.left, e.right]
                else:
                    preds.append(_norm(e.sql(dialect="postgres")))
        out[q["id"]] = [p for p in preds if len(p) >= 6]
    return out


def gold_sql_overlap(item) -> dict[str, list[str]]:
    """같은 골든 정답 SQL의 술어가 3개 이상 그대로 들어 있으면 SQL을 옮겨 적은 것으로 본다."""
    text = _norm(" ".join(texts(item)))
    hits = {qid: [p for p in preds if p in text] for qid, preds in gold_predicates().items()}
    return {qid: h for qid, h in hits.items() if len(h) >= 3}


@pytest.mark.parametrize("src,item_id,item", ITEMS, ids=lambda x: x if isinstance(x, str) else "")
def test_no_question_ids_outside_motivated_by(src, item_id, item):
    assert not question_id_leaks(item)


@pytest.mark.parametrize("src,item_id,item", ITEMS, ids=lambda x: x if isinstance(x, str) else "")
def test_golden_proper_nouns_only_in_examples(src, item_id, item):
    assert not proper_noun_leaks(item, GOLDEN_PROPER_NOUNS, META_KEYS | {"examples"})


@pytest.mark.parametrize("src,item_id,item", ITEMS, ids=lambda x: x if isinstance(x, str) else "")
def test_no_holdout_proper_nouns(src, item_id, item):
    assert not proper_noun_leaks(item, HOLDOUT_PROPER_NOUNS)


@pytest.mark.parametrize("src,item_id,item", ITEMS, ids=lambda x: x if isinstance(x, str) else "")
def test_no_gold_sql_copy(src, item_id, item):
    assert not gold_sql_overlap(item)


def test_lints_detect_leaks():
    """린트가 실제로 잡는지: 정의에 문항 id·고유명사·정답 SQL 술어를 넣으면 걸려야 한다."""
    planted = {**TERMS["mmf"], "definition": "C2-05는 외화 MMF를 포함한다."}
    assert question_id_leaks(planted)
    assert proper_noun_leaks({"definition": "KODEX 200의 구성종목이다."}, GOLDEN_PROPER_NOUNS)
    assert not proper_noun_leaks({"examples": ["KODEX 200"]}, GOLDEN_PROPER_NOUNS, META_KEYS | {"examples"})
    copied = {"then": "sale_yn = '판매중' AND prvo_pbff_desc = '공모' AND zrin_btyp_nm = '해외주식형'"}
    assert gold_sql_overlap(copied)
    assert not gold_sql_overlap({"then": "판매여부가 '판매중'인 클래스만 남긴다."})


# ───────────────────────── 추적·커버리지 ─────────────────────────
@pytest.mark.parametrize("src,item_id,item", ITEMS, ids=lambda x: x if isinstance(x, str) else "")
def test_motivated_by(src, item_id, item):
    mb = item.get("motivated_by")
    assert mb, f"{src}:{item_id} motivated_by 없음"
    assert set(mb) <= GOLDEN_IDS, set(mb) - GOLDEN_IDS


def test_every_golden_question_covered():
    covered = {i for _, _, item in ITEMS for i in item["motivated_by"]}
    assert GOLDEN_IDS <= covered, sorted(GOLDEN_IDS - covered)


@pytest.mark.parametrize("q", HOLDOUT, ids=lambda q: q["id"])
def test_holdout_knowledge_resolved(q):
    mapping = HOLDOUT_COVERAGE[q["id"]]
    assert len(mapping) == len(q["tests_knowledge"])
    for refs in mapping:
        assert refs and all(any_ref_ok(r) for r in refs), refs


def test_worksheet_refs_resolve():
    """워크시트 처리 열의 항목이 YAML이나 review_log에 있어야 한다(숨은 조건이 빠지지 않았는지)."""
    legend = {"PascalCase", "Concept.column", "snake_case", "R-…", "log:…"}  # 표기 설명
    bad = []
    for tok in set(re.findall(r"`([^`]+)`", WORKSHEET)) - legend:
        if tok.startswith("log:"):
            ok = tok[4:] in DECISIONS | CONFLICTS
        elif tok.startswith("R-"):
            ok = tok in RULE_IDS or ("*" in tok and any(fnmatch(r, tok) for r in RULE_IDS))
        elif re.fullmatch(r"[A-Za-z_][\w.]*", tok):
            ok = any_ref_ok(tok) or tok in REVIEW
        else:
            continue  # 파일 경로 등
        if not ok:
            bad.append(tok)
    assert not bad, sorted(set(bad))
