"""도메인 규칙 로딩 테스트 (DB·LLM 불필요)."""

from portfolio_agent import prompts


def _load(tmp_path, monkeypatch, text: str | None) -> dict[str, str]:
    path = tmp_path / "domain_rules.md"
    if text is not None:
        path.write_text(text)
    monkeypatch.setattr(prompts, "RULES_PATH", path)
    return prompts._load_rule_sections()


def test_missing_file_means_no_rules(tmp_path, monkeypatch):
    assert _load(tmp_path, monkeypatch, None) == {}


def test_preamble_and_empty_sections_are_ignored(tmp_path, monkeypatch):
    text = "# 도메인 규칙\n\n- 머리말 설명\n\n## 공통\n\n## prbd01n001 (채권)\n- 규칙 A (근거: C1-01)\n"
    sections = _load(tmp_path, monkeypatch, text)
    assert list(sections) == ["prbd01n001"]
    assert sections["prbd01n001"] == "## prbd01n001 (채권)\n- 규칙 A (근거: C1-01)"


def test_rules_for_without_rules(monkeypatch):
    monkeypatch.setattr(prompts, "RULE_SECTIONS", {})
    assert prompts.rules_for(["pref01n001"]) == prompts.NO_RULES


def test_current_rules_file_has_no_rules():
    """현재 domain_rules.md는 비어 있어야 한다 (규칙 없는 A 버전 기준선). 규칙을 추가하면 이 테스트를 지운다."""
    assert prompts.RULE_SECTIONS == {}
    assert "판매중" not in prompts.generate_prompt(["prbd01n001"], "(스키마)")
