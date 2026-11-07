"""knowledge 스키마의 용어·규칙을 문서로 조립해 임베딩하고 item_embedding(pgvector)에 저장한다.

uv run python scripts/embed_ontology.py            # 바뀐 문서만 임베딩
uv run python scripts/embed_ontology.py --dry-run  # API·쓰기 없이 조립된 문서와 할 일만 출력

- 먼저 scripts/load_ontology.py로 YAML을 적재해 둔다. 문서 재료는 knowledge 테이블에서 읽는다.
- 문서 조립: financial_product_agent.embedding (term_doc, rule_doc). content_sha가 같으면 다시 부르지 않는다.
- 더 이상 없는 항목의 벡터는 지운다. 쓰기는 API 호출이 끝난 뒤 트랜잭션 하나로 한다.
"""

import argparse
import time

from financial_product_agent import db
from financial_product_agent.config import KNOWLEDGE_DDL_PATH
from financial_product_agent.embedding import (
    EMBED_MODEL,
    VARIANT,
    content_sha,
    embed,
    rule_doc,
    term_doc,
    to_pgvector,
)

CALL_INTERVAL_S = 1.1  # 분당 호출 한도 여유

TERMS_SQL = """
SELECT i.id, i.label, i.definition,
       coalesce(array_agg(f.form ORDER BY f.form) FILTER (WHERE f.form IS NOT NULL), '{}')
FROM knowledge.term t
JOIN knowledge.item i ON i.id = t.id
LEFT JOIN knowledge.term_surface_form f ON f.term_id = t.id
GROUP BY i.id, i.label, i.definition
ORDER BY i.id
"""

RULES_SQL = """
SELECT r.id, r.when_text,
       coalesce(array_agg(DISTINCT ti.label) FILTER (WHERE ti.label IS NOT NULL), '{}')
FROM knowledge.rule r
LEFT JOIN knowledge.rule_applies_to a ON a.rule_id = r.id
LEFT JOIN knowledge.item ti ON ti.id = a.target_id
GROUP BY r.id, r.when_text
ORDER BY r.id
"""


def build_docs(cur) -> dict[str, str]:
    cur.execute(TERMS_SQL)
    docs = {tid: term_doc(label, forms, definition) for tid, label, definition, forms in cur.fetchall()}
    cur.execute(RULES_SQL)
    docs |= {rid: rule_doc(when, labels) for rid, when, labels in cur.fetchall()}
    return docs


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="API를 부르지 않고 DB에도 쓰지 않는다")
    args = ap.parse_args()

    with db.connect_knowledge() as conn, conn.cursor() as cur:
        if not args.dry_run:
            cur.execute(KNOWLEDGE_DDL_PATH.read_text(encoding="utf-8"))
            conn.commit()
        docs = build_docs(cur)
        existing = {}
        cur.execute("SELECT to_regclass('knowledge.item_embedding')")
        if cur.fetchone()[0] is not None:  # dry-run은 DDL을 적용하지 않아 테이블이 없을 수 있다
            cur.execute(
                "SELECT item_id, content_sha FROM knowledge.item_embedding WHERE model = %s AND variant = %s",
                (EMBED_MODEL, VARIANT))
            existing = dict(cur.fetchall())
        conn.rollback()

    shas = {i: content_sha(d) for i, d in docs.items()}
    todo = [i for i in docs if existing.get(i) != shas[i]]
    stale = sorted(set(existing) - set(docs))

    if args.dry_run:
        for i, d in docs.items():
            mark = "*" if i in todo else " "
            print(f"{mark} {i}\n    {d}")
        print(f"\n문서 {len(docs)}개 (용어·규칙), 임베딩할 것 {len(todo)}개(*), 지울 것 {len(stale)}개"
              f" — model={EMBED_MODEL}, variant={VARIANT}\n(dry-run: API 호출·DB 쓰기 없음)")
        return

    vectors = {}
    for n, i in enumerate(todo, 1):
        vectors[i] = embed(docs[i])
        print(f"\r임베딩 {n}/{len(todo)}", end="", flush=True)
        if n < len(todo):
            time.sleep(CALL_INTERVAL_S)
    if todo:
        print()

    with db.connect_knowledge() as conn, conn.transaction(), conn.cursor() as cur:
        if stale:
            cur.execute(
                "DELETE FROM knowledge.item_embedding WHERE model = %s AND variant = %s AND item_id = ANY(%s)",
                (EMBED_MODEL, VARIANT, stale))
        cur.executemany(
            "INSERT INTO knowledge.item_embedding (item_id, model, variant, content_sha, content, vec)"
            " VALUES (%s, %s, %s, %s, %s, %s::vector)"
            " ON CONFLICT (item_id, model, variant) DO UPDATE SET content_sha = EXCLUDED.content_sha,"
            " content = EXCLUDED.content, vec = EXCLUDED.vec, embedded_at = now()",
            [(i, EMBED_MODEL, VARIANT, shas[i], docs[i], to_pgvector(v)) for i, v in vectors.items()])
        cur.execute("SELECT count(*) FROM knowledge.item_embedding WHERE model = %s AND variant = %s",
                    (EMBED_MODEL, VARIANT))
        total = cur.fetchone()[0]
    print(f"문서 {len(docs)}개: 새로 임베딩 {len(todo)}, 그대로 {len(docs) - len(todo)}, 삭제 {len(stale)}"
          f" → item_embedding {total}행 (model={EMBED_MODEL}, variant={VARIANT})")


if __name__ == "__main__":
    main()
