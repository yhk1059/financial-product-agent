"""LLM이 만든 SQL을 실행 전에 검증한다.

허용: public의 허용 테이블만 읽는 단일 SELECT(WITH 포함) 문.
DB 계정 자체도 읽기 전용이지만, 여기서 한 번 더 막는다.
"""

import re

import sqlglot
from sqlglot import exp

from .config import ALLOWED_TABLES, settings

_FORBIDDEN_NODES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Command,
    exp.Into,  # SELECT ... INTO
    exp.Lock,
)
_FORBIDDEN_FUNC_PREFIXES = ("pg_", "dblink", "lo_", "set_config", "current_setting", "query_to_xml")


# LLM이 자주 쓰는 숫자로 시작하는 별칭(AS 1년수익률)은 PostgreSQL 문법 오류라서 따옴표로 감싼다.
_DIGIT_ALIAS = re.compile(r'(\bAS\s+)([0-9][^\s,()"\']*)', re.IGNORECASE)
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
# 문제 토큰 바로 앞이 'AS 단어 '이면 공백이 든 별칭이다 (AS 개인 세후수익률, 가드가 고친 AS "1년" 수익률).
_ALIAS_BEFORE = re.compile(r'\bAS\s+("[^"]*"|[^\s,()"\']+)\s+$', re.IGNORECASE)
ALIAS_SPACE_HINT = (
    '별칭에 공백이 있으면 큰따옴표로 감싸세요(예: AS "개인 세후수익률"). '
    "ORDER BY 등에서 그 별칭을 다시 쓸 때도 같은 따옴표를 씁니다."
)


class SQLGuardError(ValueError):
    pass


def syntax_error_message(e: Exception) -> str:
    """sqlglot 오류를 LLM이 고칠 수 있는 글로 바꾼다.

    sqlglot은 문제 토큰을 터미널 밑줄 코드로만 표시하므로, 오류 정보의 앞뒤 문맥으로 >>토큰<< 표시를 직접 만든다.
    """
    err = (getattr(e, "errors", None) or [{}])[0]
    token = err.get("highlight")
    if not token:  # TokenError 등 위치 정보가 없는 오류
        return f"SQL 구문 오류: {_ANSI.sub('', str(e))}"
    before, after = err.get("start_context", ""), err.get("end_context", "")
    msg = f"SQL 구문 오류: '{token}' 근처에서 문법이 맞지 않습니다.\n  {before}>>{token}<<{after}"
    if _ALIAS_BEFORE.search(before):
        msg += f"\n{ALIAS_SPACE_HINT}"
    return msg


def validate(sql: str) -> str:
    """검증된 SQL을 반환한다. LIMIT이 없으면 기본 LIMIT을 붙인다."""
    sql = _DIGIT_ALIAS.sub(r'\1"\2"', sql)
    try:
        statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    except (sqlglot.errors.ParseError, sqlglot.errors.TokenError) as e:  # TokenError: 따옴표 짝 불일치 등
        raise SQLGuardError(syntax_error_message(e)) from e

    if len(statements) != 1:
        raise SQLGuardError("SQL 문은 하나만 실행할 수 있습니다.")
    tree = statements[0]

    if not isinstance(tree, exp.Query):
        raise SQLGuardError("SELECT 조회문만 실행할 수 있습니다.")
    for node in tree.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            raise SQLGuardError(f"허용되지 않는 구문입니다: {node.key.upper()}")
        if isinstance(node, exp.Func):
            name = (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()
            if name.startswith(_FORBIDDEN_FUNC_PREFIXES):
                raise SQLGuardError(f"허용되지 않는 함수입니다: {name}")

    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        name = table.name.lower()
        schema = table.db.lower()
        if not name:  # 함수 호출 형태의 FROM (generate_series 등)
            continue
        if schema and schema != "public":
            raise SQLGuardError(f"public 스키마 외 접근은 허용되지 않습니다: {schema}.{name}")
        if name in cte_names and not schema:
            continue
        if name not in ALLOWED_TABLES:
            raise SQLGuardError(
                f"허용되지 않은 테이블입니다: {name}. 사용 가능: {', '.join(ALLOWED_TABLES)}"
            )

    if isinstance(tree, exp.Select | exp.SetOperation) and not tree.args.get("limit"):
        tree = tree.limit(settings.default_limit)
    # identify=True: 모든 식별자를 따옴표로 감싸 한글 별칭도 안전하게 만든다.
    return tree.sql(dialect="postgres", identify=True)
