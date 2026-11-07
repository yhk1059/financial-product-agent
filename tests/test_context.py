"""context.py 조립 함수 테스트 (DB·LLM 없음). 실제 온톨로지 대신 작은 가짜 지식 묶음을 쓴다."""

from financial_product_agent.context import Knowledge, build_context, expand, item_tables, ref_text, render_rule


def _item(kind, label=None, definition=None, note=None):
    return {"kind": kind, "label": label, "definition": definition, "note": note}


def _concept(table, grain=None, identity=None, parent=None, disc=None, disc_value=None, derived=None):
    return {"table_name": table, "grain": grain, "identity": identity, "parent_id": parent,
            "discriminator": disc, "discriminator_value": disc_value, "derived_from": derived}


def _attr(concept, column, data_type="text", unit=None, missing=None, zero=None):
    return {"concept_id": concept, "column_name": column, "data_type": data_type, "unit": unit,
            "missing_means": missing, "zero_means": zero}


def _rule(rule_type, when, then, targets, disclose=None, depends_on=(), overrides=()):
    return {"rule_type": rule_type, "when_text": when, "then_text": then, "disclose": disclose,
            "targets": list(targets), "depends_on": list(depends_on), "overrides": list(overrides)}


def kn() -> Knowledge:
    items = {
        "Listed": _item("concept", "상장상품", "상장된 상품이다."),
        "ListedEtf": _item("concept", "상장 ETF", "상장상품 중 ETF다."),
        "Lot": _item("concept", "채권 LOT", "채권 판매 건이다."),
        "Listed.fee": _item("attribute", "보수", "연간 보수다."),
        "Listed.sale_yn": _item("attribute", "판매여부", "판매 여부다."),
        "Lot.qty": _item("attribute", "매수가능수량", "남은 수량이다."),
        "on_sale": _item("term", "판매중·판매 가능", "지금 살 수 있다."),
        "etf": _item("term", "ETF", "상장지수펀드다."),
        "future": _item("term", "미래 수익률", "앞으로의 수익률이다."),
        "as_of_date": _item("term", "지금", "기준일이다."),
        "R-scope-default": _item("rule"),
        "R-zero-missing": _item("rule"),
        "R-zero-is-value": _item("rule"),
        "R-count-lot": _item("rule"),
        "R-sale-lot": _item("rule"),
        "R-item-unit": _item("rule"),
    }
    concepts = {
        "Listed": _concept("tbl_listed", "상장상품 1개", ["code"], disc="grp"),
        "ListedEtf": _concept("tbl_listed", parent="Listed", disc_value="ETF"),
        "Lot": _concept("tbl_bond", "LOT 1개", ["no", "seq"]),
    }
    attributes = {
        "Listed.fee": _attr("Listed", "fee", "text(숫자)", "%", "미수집", "미입력"),
        "Listed.sale_yn": _attr("Listed", "sale_yn"),
        "Lot.qty": _attr("Lot", "qty", "numeric"),
    }
    terms = {
        "on_sale": {"scope_tables": ["tbl_listed", "tbl_bond"], "in_data": True, "order_values": None,
                    "forms": ["판매중", "살 수 있는"],
                    "canonical": [("Listed.sale_yn", ["1"]), ("Lot.qty", None)]},
        "etf": {"scope_tables": ["tbl_listed"], "in_data": True, "order_values": None, "forms": [],
                "canonical": [("ListedEtf", None)]},
        "future": {"scope_tables": [], "in_data": False, "order_values": None, "forms": [], "canonical": []},
        "as_of_date": {"scope_tables": ["tbl_listed", "tbl_bond"], "in_data": True, "order_values": None,
                       "forms": [], "canonical": []},
    }
    rules = {
        "R-scope-default": _rule("조건", "판매 표현이 없다.", "전체를 본다.", ["Listed", "Lot"]),
        "R-zero-missing": _rule("예외", "0이 미입력이다.", "0을 뺀다.", ["Listed.fee"], overrides=["R-zero-is-value"]),
        "R-zero-is-value": _rule("조건", "0이 있다.", "0을 값으로 본다.", ["Listed.fee"]),
        "R-count-lot": _rule("예외", "판매 채권 개수를 묻는다.", "LOT로 센다.", ["Lot", "on_sale"],
                             disclose="LOT 기준", depends_on=["R-sale-lot"], overrides=["R-item-unit"]),
        "R-sale-lot": _rule("조건", "판매 가능 채권을 묻는다.", "수량이 있는 LOT만 본다.", ["Lot.qty"],
                            depends_on=["R-item-unit"]),
        "R-item-unit": _rule("계산", "채권을 센다.", "종목 기준으로 센다.", ["Lot"]),
        "R-hold-sale": _rule("조건", "판매중 구성종목을 묻는다.", "구성종목도 본다.", ["on_sale", "Hold.name"]),
    }
    items |= {"Hold": _item("concept", "구성종목", "ETF 구성종목이다."), "Hold.name": _item("attribute", "종목명", "이름."),
              "R-hold-sale": _item("rule")}
    concepts["Hold"] = _concept("tbl_hold", "등재 1건", ["id"])
    attributes["Hold.name"] = _attr("Hold", "name")
    items["lot_of_listed"] = _item("relation", "상장상품의 LOT다")
    relations = {"lot_of_listed": {"from_id": "Lot", "to_id": "Listed", "cardinality": "n..1",
                                   "via": ["code"], "derived": None}}
    return Knowledge(items, concepts, attributes, relations, terms, rules)


def test_ref_text_uses_real_table_and_values():
    k = kn()
    assert ref_text(k, "Listed.sale_yn", ["1"]) == "tbl_listed.sale_yn='1'"
    assert ref_text(k, "Listed.sale_yn", ["1", "2"]) == "tbl_listed.sale_yn IN ('1', '2')"
    assert ref_text(k, "ListedEtf") == "tbl_listed.grp='ETF'"


def test_canonical_only_selected_tables():
    ctx = build_context(kn(), ["tbl_bond"], ["on_sale"])
    assert "tbl_bond.qty" in ctx.rules_text
    assert "tbl_listed.sale_yn" not in ctx.rules_text
    assert "같은 말: 살 수 있는" in ctx.rules_text  # 라벨에 이미 있는 '판매중'은 반복하지 않는다


def test_expand_one_hop_and_skips_known():
    k = kn()
    assert expand(k, ["R-count-lot"], set()) == ["R-sale-lot"]  # R-sale-lot의 depends_on은 따라가지 않는다
    assert expand(k, ["R-count-lot", "R-sale-lot"], set()) == ["R-item-unit"]
    assert expand(k, ["R-count-lot"], {"R-sale-lot"}) == []


def test_overrides_quoted_not_pulled():
    k = kn()
    ctx = build_context(k, ["tbl_bond"], ["R-count-lot"])
    assert ctx.layers["expanded"] == ["R-sale-lot"]
    assert "「종목 기준으로 센다.」보다 우선" in render_rule(k, "R-count-lot")
    assert "- [계산] 채권을 센다." not in ctx.rules_text  # overrides 대상 규칙 자체는 들어가지 않는다
    assert "답변에 밝힐 것: LOT 기준" in ctx.rules_text


def test_b_layer_always_and_a_layer_only_selected():
    ctx = build_context(kn(), ["tbl_bond"], [])
    assert "판매 표현이 없다." in ctx.rules_text  # B층
    assert "### tbl_bond 의미" in ctx.tables_text
    assert "tbl_listed" not in ctx.tables_text
    assert "0이 미입력이다." not in ctx.tables_text  # A층 규칙은 대상 테이블이 선택될 때만


def test_a_layer_subtype_and_attribute_annotations():
    ctx = build_context(kn(), ["tbl_listed"], [])
    t = ctx.tables_text
    assert "- 상장 ETF: 상장상품 중 ETF다. (grp='ETF')" in t
    assert "1행 = 상장상품 1개, 식별키 code" in t
    assert "[text로 저장된 숫자] [단위 %] [NULL = 미수집] [0 = 미입력]" in t
    assert "0이 미입력이다." in t and "「0을 값으로 본다.」보다 우선" in t
    assert "- 채권 LOT → 상장상품: 상장상품의 LOT다 (n..1, 연결 컬럼 code)" in t  # 한쪽 끝만 선택돼도 넣는다


def test_in_data_false_and_fixed_items_dropped_from_c():
    ctx = build_context(kn(), ["tbl_listed"], ["future", "R-zero-missing", "R-scope-default"])
    assert "미래 수익률: 앞으로의 수익률이다. (이 데이터에 없음)" in ctx.rules_text
    assert ctx.layers["C"] == ["future"]  # A·B층 항목은 C층에서 빠진다


def test_rule_tables_narrowed_by_term_version():
    k = kn()
    assert item_tables(k, "R-count-lot") == {"tbl_bond"}  # on_sale의 채권 버전(Lot)을 함께 가리킨다
    assert item_tables(k, "R-hold-sale") == {"tbl_hold", "tbl_listed", "tbl_bond"}  # Hold는 on_sale 정본에 없다


def test_unrelated_c_dropped_before_expansion():
    ctx = build_context(kn(), ["tbl_listed"], ["on_sale", "R-count-lot"])
    assert ctx.layers["C"] == ["on_sale"]
    assert ctx.layers["dropped"] == ["R-count-lot"]
    assert ctx.layers["expanded"] == []  # 걸러진 규칙의 의존 규칙은 붙지 않는다
    assert "LOT로 센다." not in ctx.rules_text


def test_no_trace_metadata():
    ctx = build_context(kn(), ["tbl_listed", "tbl_bond"], ["on_sale", "R-count-lot"])
    text = ctx.rules_text + ctx.tables_text
    for banned in ("motivated_by", "P-", "profiling:", "혼동"):
        assert banned not in text


def test_oos_text_lists_in_data_false_terms_for_selected_tables():
    """C층의 '데이터에 없는 개념'은 oos_text로 따로 나온다. scope_tables가 있으면 선택된 테이블과 겹칠 때만."""
    k = kn()
    k.items["weight"] = _item("term", "편입 비중", "구성종목의 비율이다.")
    k.terms["weight"] = {"scope_tables": ["tbl_hold"], "in_data": False, "order_values": None, "forms": ["비중"],
                         "canonical": []}
    ctx = build_context(k, ["tbl_listed"], ["future", "weight"])
    assert ctx.oos_text == "- 미래 수익률: 앞으로의 수익률이다."  # 편입 비중은 구성종목 테이블이 없으면 빠진다
    ctx = build_context(k, ["tbl_hold"], ["weight"])
    assert ctx.oos_text == "- 편입 비중 [같은 말: 비중]: 구성종목의 비율이다."
    assert build_context(k, ["tbl_listed"], ["on_sale"]).oos_text == ""
