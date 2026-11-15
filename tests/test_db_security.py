"""Security layers 1 and 2, tested straight against Postgres, without the validator in front."""

import time

import psycopg
import pytest

from delivery_agent.audit import QueryContext
from delivery_agent.database import Database
from delivery_agent.settings import Settings

pytestmark = pytest.mark.db

CONTEXT = QueryContext(request_id="test", source="eval")
WRITES = [
    "INSERT INTO reviews (id, order_id, rating, created_at) VALUES (999999, 1, 5, now())",
    "UPDATE orders SET total = 0 WHERE id = 1",
    "DELETE FROM orders WHERE id = 1",
    "DROP TABLE orders",
    "TRUNCATE orders",
    "CREATE TABLE stolen (id int)",
]


def test_agent_cannot_read_customer_pii(agent_conn: psycopg.Connection) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="permission denied for table customers"):
        agent_conn.execute("SELECT phone FROM customers")


@pytest.mark.parametrize("statement", WRITES)
def test_agent_cannot_write(agent_conn: psycopg.Connection, statement: str) -> None:
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        agent_conn.execute(statement)


@pytest.mark.parametrize("statement", WRITES)
def test_agent_cannot_write_even_after_turning_read_only_off(settings: Settings, statement: str) -> None:
    with psycopg.connect(settings.agent_conninfo(), autocommit=True) as conn:
        conn.execute("SET default_transaction_read_only = off")

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(statement)


def test_public_view_has_no_pii(agent_conn: psycopg.Connection) -> None:
    cursor = agent_conn.execute("SELECT * FROM customers_public LIMIT 1")

    assert [column.name for column in cursor.description or []] == [
        "id",
        "neighborhood",
        "city",
        "signup_date",
        "is_subscriber",
    ]


def test_role_settings(agent_conn: psycopg.Connection) -> None:
    assert agent_conn.execute("SHOW default_transaction_read_only").fetchone() == ("on",)
    assert agent_conn.execute("SHOW statement_timeout").fetchone() == ("5s",)


def test_slow_query_is_canceled(agent_conn: psycopg.Connection) -> None:
    started = time.monotonic()

    with pytest.raises(psycopg.errors.QueryCanceled):
        agent_conn.execute("SELECT count(*) FROM orders a, orders b, orders c")

    assert time.monotonic() - started < 10


def test_set_config_is_revoked(agent_conn: psycopg.Connection) -> None:
    with pytest.raises(
        psycopg.errors.InsufficientPrivilege, match="permission denied for function set_config"
    ):
        agent_conn.execute("SELECT set_config('statement_timeout', '0', false)")


def test_transaction_is_read_only(database: Database) -> None:
    result = database.execute_sql(
        "SELECT current_setting('transaction_read_only') AS read_only, "
        "current_setting('statement_timeout') AS timeout",
        CONTEXT,
    )

    assert result.rows == [["on", "5s"]]
