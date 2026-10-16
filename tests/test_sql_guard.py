import pytest

from portfolio_agent.sql_guard import SQLGuardError, validate


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT pd_nm FROM pref01n001 WHERE pd_sale_yn = '1' LIMIT 5",
        "WITH t AS (SELECT item_id FROM domestic_etf_holdings) SELECT count(*) FROM t",
        "SELECT p.pd_nm, count(*) FROM public.pref01n001 p JOIN domestic_etf_holdings h ON h.item_id = p.pd_itm_no GROUP BY 1",
        "SELECT pd_nm FROM pref01n001 UNION ALL SELECT pd_nm FROM pref02n001",
        "SELECT cu_charge_rt::numeric FROM pref01n001 WHERE cu_charge_rt ~ '^[0-9.]+$'",
    ],
)
def test_allows_select(sql):
    assert validate(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM pref01n001",
        "UPDATE pref01n001 SET pd_nm = 'x'",
        "DROP TABLE pref01n001",
        "INSERT INTO pref01n001 (pd_itm_no) VALUES ('x')",
        "SELECT * INTO tmp FROM pref01n001",
        "SELECT 1; DELETE FROM pref01n001",
        "SELECT * FROM semantic.concepts",
        "SELECT * FROM pg_catalog.pg_user",
        "SELECT * FROM information_schema.tables",
        "SELECT * FROM some_other_table",
        "SELECT pg_sleep(10) FROM pref01n001",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT set_config('default_transaction_read_only', 'off', false)",
        "COPY pref01n001 TO '/tmp/x'",
        "SELECT * FROM pref01n001 FOR UPDATE",
        "SELEC * FROM",
    ],
)
def test_blocks_unsafe(sql):
    with pytest.raises(SQLGuardError):
        validate(sql)


def test_adds_default_limit():
    assert "LIMIT 200" in validate("SELECT pd_nm FROM pref01n001")
    assert "LIMIT 5" in validate("SELECT pd_nm FROM pref01n001 LIMIT 5")


def test_quotes_korean_and_digit_aliases():
    out = validate("SELECT pd_nm AS 상품명, du_er_1y AS 1년수익률 FROM pref01n001")
    assert '"1년수익률"' in out and '"상품명"' in out
