from pathlib import Path

import psycopg
import pytest

from delivery_agent.audit import AuditLog, QueryContext
from delivery_agent.database import Database, fetch_rows
from delivery_agent.errors import ExecutionError, QueryTimeout, SQLRejected
from delivery_agent.settings import Settings

pytestmark = pytest.mark.db

CONTEXT = QueryContext(request_id="test", source="eval", question="pergunta de teste")


def test_returns_columns_rows_and_duration(database: Database) -> None:
    result = database.execute_sql(
        "SELECT city, COUNT(*) AS restaurants FROM restaurants GROUP BY city ORDER BY city", CONTEXT
    )

    assert result.columns == ["city", "restaurants"]
    assert result.rows[0] == ["Belo Horizonte", 18]
    assert len(result.rows) == 7
    assert not result.truncated
    assert result.duration_ms > 0
    assert result.sql.endswith("\nLIMIT 101")


def test_cuts_the_result_at_100_rows(database: Database) -> None:
    result = database.execute_sql("SELECT id FROM orders ORDER BY id", CONTEXT)

    assert len(result.rows) == 100
    assert result.truncated


def test_converts_numbers_and_dates(database: Database) -> None:
    result = database.execute_sql(
        "SELECT total, created_at, created_at::date AS day, status = 'delivered' AS delivered "
        "FROM orders WHERE id = 1",
        CONTEXT,
    )
    total, created_at, day, delivered = result.rows[0]

    assert isinstance(total, float)
    assert created_at == "2026-01-01 11:16:59"
    assert day == "2026-01-01"
    assert isinstance(delivered, bool)


def test_rejected_sql_never_reaches_the_database(database: Database) -> None:
    with pytest.raises(SQLRejected, match="DELETE"):
        database.execute_sql("DELETE FROM orders", CONTEXT)


def test_postgres_errors_become_execution_errors(database: Database) -> None:
    with pytest.raises(ExecutionError, match='Erro do Postgres: column "nope" does not exist'):
        database.execute_sql("SELECT nope FROM orders", CONTEXT)


def test_input_is_validated(database: Database) -> None:
    with pytest.raises(SQLRejected, match=r"10\.000 caracteres"):
        database.execute_sql("SELECT " + "1" * 10_000, CONTEXT)


def test_slow_query_hits_the_timeout(settings: Settings, tmp_path: Path) -> None:
    fast = settings.model_copy(update={"statement_timeout_ms": 200})

    with (
        Database(fast, AuditLog(tmp_path / "audit.jsonl")) as database,
        pytest.raises(QueryTimeout, match="0,2 s"),
    ):
        database.execute_sql("SELECT count(*) FROM orders a, orders b, orders c", CONTEXT)


def test_one_call_runs_a_single_statement(agent_conn: psycopg.Connection) -> None:
    with pytest.raises(psycopg.errors.SyntaxError, match="multiple commands"):
        fetch_rows(agent_conn, "SELECT 1; SELECT 2", 100)


def test_ping(database: Database) -> None:
    assert database.ping()
