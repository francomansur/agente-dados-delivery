import pytest

from delivery_agent.errors import SQLRejected
from delivery_agent.sql_validator import validate_sql

VALID_QUERIES = [
    "SELECT r.city, SUM(o.total) AS revenue FROM orders o JOIN restaurants r ON r.id = o.restaurant_id "
    "WHERE o.status = 'delivered' GROUP BY r.city ORDER BY revenue DESC",
    "WITH t AS (SELECT EXTRACT(DOW FROM created_at) AS dow, COUNT(*) AS n FROM orders GROUP BY 1) "
    "SELECT * FROM t ORDER BY n DESC",
    "SELECT ROUND(100.0 * COUNT(*) FILTER (WHERE status = 'canceled') / COUNT(*), 2) AS rate FROM orders",
    "SELECT DATE_TRUNC('month', created_at)::date AS month, COUNT(*) FROM orders GROUP BY 1 ORDER BY 1",
    "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY delivery_minutes) FROM orders",
    "SELECT DISTINCT ON (r.city) r.city, r.name FROM restaurants r ORDER BY r.city, r.name",
    "SELECT name FROM restaurants WHERE name ILIKE '%sushi%'",
    "SELECT city, string_agg(name, ', ' ORDER BY name) FROM restaurants GROUP BY city",
    "SELECT x FROM generate_series(1, 3) AS g(x)",
    "SELECT 'it''s' AS s",
    "SELECT $$a;b$$ AS s",
    "SELECT 1 AS a UNION SELECT 2 ORDER BY a",
    "(SELECT 1) UNION (SELECT 2)",
    "SELECT * FROM orders OFFSET 10",
    "SELECT name, RANK() OVER (PARTITION BY city ORDER BY base_delivery_fee DESC) FROM restaurants",
    "SELECT * FROM restaurants r, LATERAL (SELECT COUNT(*) FROM orders o WHERE o.restaurant_id = r.id) c",
    "SELECT CASE WHEN delivery_minutes > promised_minutes THEN 'atrasado' ELSE 'no prazo' END, COUNT(*) "
    "FROM orders WHERE status = 'delivered' GROUP BY 1",
]

REJECTED_QUERIES = [
    ("", "vazia"),
    ("   ", "vazia"),
    ("-- só um comentário", "vazia"),
    ("SELEC 1", "Não consegui analisar"),
    ("SELECT 1; DROP TABLE orders", "único comando"),
    ("SELECT 1; SELECT 2", "único comando"),
    # Strings and comments that hide a second statement, read the way Postgres reads them.
    ("SELECT E'\\'' ; DELETE FROM orders; --'", "único comando"),
    ("SELECT $$ $tag$ $$ ; DELETE FROM orders", "único comando"),
    ("SELECT 1 /* /* */ */ ; DELETE FROM orders", "único comando"),
    ('SELECT U&"\\0070g_sleep"(1)', "Unicode"),
    ("INSERT INTO reviews (order_id, rating) VALUES (1, 5)", "INSERT"),
    ("UPDATE orders SET total = 0", "UPDATE"),
    ("DELETE FROM orders", "DELETE"),
    ("MERGE INTO reviews r USING orders o ON r.order_id = o.id WHEN MATCHED THEN DELETE", "MERGE"),
    ("DROP TABLE orders", "DROP"),
    ("TRUNCATE orders", "TRUNCATE"),
    ("COPY orders TO STDOUT", "COPY"),
    ("SET statement_timeout = 0", "SET"),
    ("GRANT SELECT ON customers TO agent_reader", "GRANT"),
    ("BEGIN", "BEGIN"),
    ("DO $$ BEGIN DELETE FROM orders; END $$", "DO"),
    ("EXPLAIN ANALYZE DELETE FROM orders", "EXPLAIN"),
    ("CALL p()", "CALL"),
    ("VACUUM", "VACUUM"),
    ("LOCK TABLE orders", "LOCK"),
    ("VALUES (1)", "Só consultas de leitura"),
    ("WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d", "DELETE"),
    ("WITH u AS (UPDATE orders SET total = 0 RETURNING id) SELECT * FROM u", "UPDATE"),
    ("SELECT * INTO orders_copy FROM orders", "SELECT ... INTO"),
    ("SELECT * FROM orders FOR UPDATE", "FOR UPDATE"),
    ("SELECT * FROM orders FOR SHARE", "FOR UPDATE ou FOR SHARE"),
    ("SELECT pg_sleep(10)", "pg_sleep"),
    ('SELECT "pg_sleep"(10)', "pg_sleep"),
    ("SELECT pg_catalog.pg_sleep(10)", "pg_sleep"),
    ("SELECT PG_SLEEP(10)", "pg_sleep"),
    ("SELECT set_config('statement_timeout', '0', false)", "set_config"),
    ("SELECT pg_read_file('/etc/passwd')", "pg_read_file"),
    ("SELECT lo_import('/etc/passwd')", "lo_import"),
    ("SELECT * FROM dblink('host=x', 'select 1') AS t(x int)", "dblink"),
    ("SELECT pg_terminate_backend(1)", "pg_terminate_backend"),
    ("SELECT pg_advisory_lock(1)", "pg_advisory_lock"),
    ("SELECT pg_try_advisory_lock(1)", "pg_try_advisory_lock"),
    ("SELECT count(*) FROM orders WHERE id IN (SELECT pg_sleep(1))", "pg_sleep"),
]


@pytest.mark.parametrize("sql", VALID_QUERIES)
def test_accepts_read_queries_and_runs_the_original_text(sql: str) -> None:
    assert validate_sql(sql) == f"{sql}\nLIMIT 101"


@pytest.mark.parametrize(("sql", "message"), REJECTED_QUERIES)
def test_rejects_with_a_message_that_explains_why(sql: str, message: str) -> None:
    with pytest.raises(SQLRejected, match=message.replace("(", r"\(").replace(".", r"\.")):
        validate_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM orders LIMIT 5",
        "SELECT * FROM orders ORDER BY id FETCH FIRST 5 ROWS ONLY",
        "SELECT 1 UNION SELECT 2 LIMIT 1",
    ],
)
def test_keeps_an_existing_outer_limit(sql: str) -> None:
    assert validate_sql(sql) == sql


def test_an_inner_limit_does_not_count_as_outer() -> None:
    sql = "SELECT * FROM (SELECT * FROM orders LIMIT 5) t"

    assert validate_sql(sql) == f"{sql}\nLIMIT 101"


def test_appends_the_limit_on_a_new_line_after_a_trailing_comment() -> None:
    assert validate_sql("SELECT 1 -- comentário final") == "SELECT 1 -- comentário final\nLIMIT 101"


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT 1;", "SELECT 1\nLIMIT 101"),
        ("SELECT 1 ;  \n", "SELECT 1\nLIMIT 101"),
        ("SELECT 1 LIMIT 5;", "SELECT 1 LIMIT 5"),
        ("SELECT $$;$$ AS s;", "SELECT $$;$$ AS s\nLIMIT 101"),
    ],
)
def test_removes_trailing_semicolons(sql: str, expected: str) -> None:
    assert validate_sql(sql) == expected
