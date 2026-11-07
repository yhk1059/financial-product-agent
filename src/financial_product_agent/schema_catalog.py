"""허용 테이블의 스키마 카드(컬럼 설명·타입·결측률·값 분포)를 만들고 캐시한다.

캐시 생성: `uv run python scripts/build_schema_cache.py`
"""

import json
from functools import lru_cache

from psycopg import sql

from . import db
from .config import ALLOWED_TABLES, SCHEMA_CACHE_PATH

MAX_CATEGORY_VALUES = 20  # 이 개수 이하의 고유값을 가진 text 컬럼은 값 목록을 보여준다
_NUMERIC_TYPES = {"numeric", "double precision", "bigint", "integer", "smallint", "real"}


def _profile_table(conn, table: str) -> dict:
    comment = conn.execute(
        "SELECT obj_description(%s::regclass)", (f"public.{table}",)
    ).fetchone()[0]
    cols = conn.execute(
        """SELECT column_name, data_type,
                  col_description(%s::regclass, ordinal_position)
           FROM information_schema.columns
           WHERE table_schema = 'public' AND table_name = %s
           ORDER BY ordinal_position""",
        (f"public.{table}", table),
    ).fetchall()
    tbl = sql.Identifier(table)
    row_count = conn.execute(sql.SQL("SELECT count(*) FROM {}").format(tbl)).fetchone()[0]

    columns = []
    for name, dtype, col_comment in cols:
        col = sql.Identifier(name)
        non_null, distinct = conn.execute(
            sql.SQL("SELECT count({c}), count(DISTINCT {c}) FROM {t}").format(c=col, t=tbl)
        ).fetchone()
        info = {
            "name": name,
            "type": dtype,
            "comment": col_comment or "",
            "null_pct": round(100 * (row_count - non_null) / row_count, 1) if row_count else 0,
            "distinct": distinct,
        }
        if dtype == "text" and 0 < distinct <= MAX_CATEGORY_VALUES:
            rows = conn.execute(
                sql.SQL(
                    "SELECT {c}, count(*) FROM {t} WHERE {c} IS NOT NULL GROUP BY 1 ORDER BY 2 DESC"
                ).format(c=col, t=tbl)
            ).fetchall()
            info["values"] = {v: n for v, n in rows}
        elif (dtype in _NUMERIC_TYPES or dtype == "date") and non_null:
            lo, hi = conn.execute(
                sql.SQL("SELECT min({c}), max({c}) FROM {t}").format(c=col, t=tbl)
            ).fetchone()
            info["range"] = [str(lo), str(hi)]
        elif dtype == "text" and non_null:
            rows = conn.execute(
                sql.SQL(
                    "SELECT DISTINCT {c} FROM {t} WHERE {c} IS NOT NULL LIMIT 3"
                ).format(c=col, t=tbl)
            ).fetchall()
            info["examples"] = [r[0][:60] for r in rows]
        columns.append(info)

    return {"name": table, "comment": comment or "", "row_count": row_count, "columns": columns}


def build_cache() -> dict:
    with db.connect() as conn:
        fks = conn.execute(
            """SELECT conrelid::regclass::text, pg_get_constraintdef(oid)
               FROM pg_constraint
               WHERE connamespace = 'public'::regnamespace AND contype IN ('f', 'p', 'u')"""
        ).fetchall()
        catalog = {
            "tables": {t: _profile_table(conn, t) for t in ALLOWED_TABLES},
            "constraints": [f"{t}: {d}" for t, d in fks if t in ALLOWED_TABLES],
        }
    SCHEMA_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCHEMA_CACHE_PATH.write_text(json.dumps(catalog, ensure_ascii=False, indent=1, default=str))
    load_catalog.cache_clear()
    return catalog


@lru_cache
def load_catalog() -> dict:
    if not SCHEMA_CACHE_PATH.exists():
        return build_cache()
    return json.loads(SCHEMA_CACHE_PATH.read_text())


def table_overview() -> str:
    """시스템 프롬프트용: 테이블 목록과 설명."""
    cat = load_catalog()
    lines = [
        f"- {t['name']} ({t['row_count']:,}행): {t['comment']}" for t in cat["tables"].values()
    ]
    return "\n".join(lines)


_SHORT_TYPES = {
    "text": "text",
    "numeric": "num",
    "double precision": "num",
    "bigint": "int",
    "integer": "int",
    "date": "date",
}
MAX_VALUES_SHOWN = 10


def _short_num(v: str) -> str:
    try:
        f = float(v)
    except ValueError:
        return v
    return f"{f:.6g}"


def _format_column(c: dict) -> str:
    """한 줄 요약. CLOVA Studio 분당 토큰 한도 때문에 최대한 짧게 쓴다."""
    line = f"- {c['name']} {_SHORT_TYPES.get(c['type'], c['type'])}: {c['comment']}"
    if c["null_pct"] >= 1:
        line += f" [결측{c['null_pct']:.0f}%]"
    if "values" in c:
        vals = list(c["values"])
        line += " {" + ",".join(vals[:MAX_VALUES_SHOWN]) + (",…" if len(vals) > MAX_VALUES_SHOWN else "") + "}"
    elif "range" in c and c["type"] == "date":
        line += f" ({c['range'][0]}~{c['range'][1]})"
    elif "range" in c:
        line += f" ({_short_num(c['range'][0])}~{_short_num(c['range'][1])})"
    elif c.get("examples"):
        line += f" 예:{c['examples'][0][:30]}"
    return line


def schema_card(tables: list[str]) -> str:
    """선택된 테이블의 스키마 카드. 값이 전부 비어 있는 컬럼은 이름만 따로 적는다."""
    cat = load_catalog()
    blocks = []
    for name in tables:
        t = cat["tables"].get(name)
        if t is None:
            continue
        usable = [c for c in t["columns"] if c["null_pct"] < 100]
        empty = [c["name"] for c in t["columns"] if c["null_pct"] >= 100]
        cols = "\n".join(_format_column(c) for c in usable)
        if empty:
            cols += f"\n- (값이 전부 비어 있어 사용 불가: {', '.join(empty)})"
        blocks.append(f"### {name} ({t['row_count']:,}행): {t['comment']}\n{cols}")
    related = [c for c in cat["constraints"] if any(t in c for t in tables)]
    if related:
        blocks.append("### 제약조건\n" + "\n".join(f"- {c}" for c in related))
    return "\n\n".join(blocks)
