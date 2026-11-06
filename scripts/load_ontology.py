"""ontology/*.yaml을 PostgreSQL knowledge 스키마에 적재한다 (YAML이 정본, DB는 사본).

uv run python scripts/load_ontology.py            # DDL 적용 후 전체 다시 적재
uv run python scripts/load_ontology.py --dry-run  # DB 없이 행 수만 계산

- 접속: PGHOST/PGPORT/PGDATABASE + KNOWLEDGE_PGUSER/KNOWLEDGE_PGPASSWORD (knowledge 스키마 전용 쓰기 계정)
- DDL: ontology/sql/knowledge_schema.sql (CREATE IF NOT EXISTS라 다시 실행해도 안전)
- 트랜잭션 하나로 모든 행을 지우고 다시 넣는다. 참조가 틀리면 FK 오류로 전체가 롤백된다.
"""

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone

import yaml

from portfolio_agent import db
from portfolio_agent.config import KNOWLEDGE_DDL_PATH, ONTOLOGY_DIR, PROJECT_ROOT

FILES = ("model.yaml", "glossary.yaml", "rules.yaml", "review_log.yaml")

# 삽입 순서 (삭제는 역순)
TABLES: dict[str, tuple[str, ...]] = {
    "item": ("id", "kind", "label", "definition", "note", "generalized_from"),
    "item_evidence": ("item_id", "seq", "source", "source_kind"),
    "item_motivation": ("item_id", "question_id"),
    "concept": ("id", "table_name", "grain", "identity", "parent_id", "discriminator",
                "discriminator_value", "derived_from"),
    "attribute": ("id", "concept_id", "column_name", "data_type", "unit", "missing_means", "zero_means"),
    "relation": ("id", "from_id", "to_id", "cardinality", "via", "derived"),
    "term": ("id", "scope_tables", "in_data", "order_values", "examples", "bundle", "order_up"),
    "term_canonical": ("term_id", "seq", "ref_id", "ref_values"),
    "term_surface_form": ("term_id", "form"),
    "term_confusable": ("term_id", "seq", "target_id", "target_text", "why"),
    "rule": ("id", "rule_type", "when_text", "then_text", "disclose"),
    "rule_applies_to": ("rule_id", "target_id"),
    "rule_link": ("rule_id", "target_rule_id", "link_type"),
    "review_entry": ("section", "id", "body"),
}


def load_yaml() -> dict[str, dict]:
    return {f: yaml.safe_load((ONTOLOGY_DIR / f).read_text(encoding="utf-8")) for f in FILES}


def evidence_kind(source: str, profiling_ids: set[str]) -> str:
    """근거 문자열을 정책·결정·프로파일링·카탈로그로 분류한다. 모르는 형식은 오류."""
    if source.startswith("P-"):
        return "policy"
    if source.startswith("design§8:") or source[:1] in ("G", "S") and source[1:].isdigit():
        return "decision"
    if source.startswith("profiling:"):
        if source.removeprefix("profiling:") not in profiling_ids:
            raise ValueError(f"review_log.profiling에 없는 근거: {source}")
        return "profiling"
    if source.startswith("schema_catalog:"):
        return "catalog"
    raise ValueError(f"분류할 수 없는 근거: {source}")


def _concept_order(concepts: list[dict]) -> list[dict]:
    """parent·derived_from이 먼저 들어가도록 정렬한다."""
    done: set[str] = set()
    ordered: list[dict] = []
    pending = list(concepts)
    while pending:
        ready = [c for c in pending
                 if all(c.get(k) in (None, *done) for k in ("parent", "derived_from"))]
        if not ready:
            raise ValueError(f"개념 parent/derived_from 순환 또는 누락: {[c['id'] for c in pending]}")
        for c in ready:
            ordered.append(c)
            done.add(c["id"])
            pending.remove(c)
    return ordered


def build_rows(docs: dict[str, dict]) -> dict[str, list[tuple]]:
    """YAML 문서를 테이블별 행 목록으로 바꾼다 (DB 없이 동작)."""
    model, glossary = docs["model.yaml"], docs["glossary.yaml"]
    rules, review = docs["rules.yaml"], docs["review_log.yaml"]
    rows: dict[str, list[tuple]] = {t: [] for t in TABLES}
    motivation: set[tuple[str, str]] = set()

    def add_item(id_, kind, x, *, label=True, definition=True):
        rows["item"].append((id_, kind, x.get("label") if label else None,
                             x.get("definition") if definition else None,
                             x.get("note"), x.get("generalized_from")))
        motivation.update((id_, q) for q in x.get("motivated_by", []))

    for c in _concept_order(model["concepts"]):
        add_item(c["id"], "concept", c)
        rows["concept"].append((c["id"], c["table"], c.get("grain"), c.get("identity"),
                                c.get("parent"), c.get("discriminator"),
                                c.get("discriminator_value"), c.get("derived_from")))
    for c in model["concepts"]:
        for a in c.get("attributes", []):
            aid = f"{c['id']}.{a['column']}"
            add_item(aid, "attribute", a)
            rows["attribute"].append((aid, c["id"], a["column"], a["type"], a.get("unit"),
                                      a.get("missing_means"), a.get("zero_means")))
    for r in model["relations"]:
        add_item(r["id"], "relation", r)
        rows["relation"].append((r["id"], r["from"], r["to"], r["cardinality"], r["via"], r.get("derived")))
    for t in glossary["terms"]:
        add_item(t["id"], "term", t)
    for r in rules["rules"]:
        add_item(r["id"], "rule", r, label=False, definition=False)

    ids = {row[0] for row in rows["item"]}
    if len(ids) != len(rows["item"]):
        raise ValueError("YAML 사이에 id가 겹친다")
    profiling_ids = {p["id"] for p in review["profiling"]}

    for t in glossary["terms"]:
        rows["term"].append((t["id"], t["scope_tables"], t.get("in_data", True),
                             t.get("order"), t.get("examples"), t.get("bundle", False), t.get("order_up")))
        for i, c in enumerate(t["canonical"]):
            ref, values = (c["ref"], c["values"]) if isinstance(c, dict) else (c, None)
            rows["term_canonical"].append((t["id"], i, ref, values))
        rows["term_surface_form"].extend((t["id"], f) for f in dict.fromkeys(t["surface_forms"]))
        for i, c in enumerate(t["confusable_with"]):
            target = c["target"]
            rows["term_confusable"].append(
                (t["id"], i, target if target in ids else None,
                 None if target in ids else target, c["why"]))

    for r in rules["rules"]:
        rows["rule"].append((r["id"], r["type"], r["when"], r["then"], r.get("disclose")))
        rows["rule_applies_to"].extend((r["id"], a) for a in dict.fromkeys(r["applies_to"]))
        for link in ("overrides", "depends_on"):
            rows["rule_link"].extend((r["id"], target, link) for target in r.get(link, []))

    for x in glossary["terms"] + rules["rules"]:
        rows["item_evidence"].extend(
            (x["id"], i, e, evidence_kind(e, profiling_ids)) for i, e in enumerate(x["evidence"]))

    rows["item_motivation"] = sorted(motivation)

    for section, entries in review.items():
        for i, e in enumerate(entries, 1):
            rows["review_entry"].append(
                (section, e.get("id", f"{section}-{i}"), json.dumps(e, ensure_ascii=False, default=str)))
    return rows


def _git() -> tuple[str | None, bool | None]:
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT,
                                capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "ontology"], cwd=PROJECT_ROOT,
                               capture_output=True, text=True, check=True).stdout.strip() != ""
        return commit, dirty
    except (OSError, subprocess.CalledProcessError):
        return None, None


def load(rows: dict[str, list[tuple]]) -> dict[str, int]:
    file_sha = {f: hashlib.sha256((ONTOLOGY_DIR / f).read_bytes()).hexdigest() for f in FILES}
    counts = {t: len(r) for t, r in rows.items()}
    commit, dirty = _git()
    with db.connect_knowledge() as conn, conn.transaction(), conn.cursor() as cur:
        cur.execute(KNOWLEDGE_DDL_PATH.read_text(encoding="utf-8"))
        cur.execute("SET LOCAL search_path = knowledge, public")
        for table in ["source_version", *reversed(TABLES)]:
            cur.execute(f"DELETE FROM {table}")
        for table, cols in TABLES.items():
            if rows[table]:
                ph = ", ".join(["%s"] * len(cols))
                cur.executemany(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({ph})", rows[table])
        db_counts = {}
        for table in TABLES:
            cur.execute(f"SELECT count(*) FROM {table}")
            db_counts[table] = cur.fetchone()[0]
        if db_counts != counts:
            raise RuntimeError(f"행 수 불일치: YAML {counts} / DB {db_counts}")
        cur.execute(
            "INSERT INTO source_version (loaded_at, git_commit, git_dirty, file_sha256, row_counts)"
            " VALUES (%s, %s, %s, %s, %s)",
            (datetime.now(timezone.utc), commit, dirty, json.dumps(file_sha), json.dumps(counts)))
    return db_counts


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="DB에 접속하지 않고 행 수만 출력")
    args = ap.parse_args()

    rows = build_rows(load_yaml())
    counts = {t: len(r) for t, r in rows.items()} if args.dry_run else load(rows)
    width = max(map(len, counts))
    for table, n in counts.items():
        print(f"{table:<{width}}  {n:>4}")
    print("(dry-run: DB에 쓰지 않음)" if args.dry_run else "\nknowledge 스키마 적재 완료")


if __name__ == "__main__":
    main()
