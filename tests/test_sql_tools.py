"""The SQL tools' full path: Pydantic input, validator, repository and audit log."""

import json
from pathlib import Path

import pytest

from delivery_agent.core.audit import AuditLog, QueryContext
from delivery_agent.core.errors import DatabaseUnavailable, ExecutionError, QueryTimeout, SQLRejected
from delivery_agent.core.settings import Settings
from delivery_agent.repositories.delivery_repository import Column, DeliveryRepository, Relation
from delivery_agent.services.sql_tools import SQLTools
from fakes import FakeRepository

CONTEXT = QueryContext(request_id="req-1", source="api", question="Quantos pedidos há?")
FIELDS = {
    "timestamp",
    "request_id",
    "source",
    "question",
    "sql",
    "status",
    "duration_ms",
    "row_count",
    "error",
}
ORDERS = Relation(
    name="orders",
    is_view=False,
    comment="Pedidos.",
    columns=[
        Column(name="id", data_type="integer", comment="Identificador do pedido.", category="N"),
        Column(name="status", data_type="text", comment="Situação do pedido.", category="S"),
        Column(name="neighborhood", data_type="text", comment="Bairro do cliente.", category="S"),
        Column(name="created_at", data_type="timestamp without time zone", comment="Criação.", category="D"),
    ],
)
CUSTOMERS = Relation(
    name="customers_public",
    is_view=True,
    comment="Clientes, sem dados pessoais.",
    columns=[Column(name="id", data_type="integer", comment="Identificador do cliente.", category="N")],
)
VALUES = {
    "status": ["canceled", "delivered"],
    "neighborhood": ["Olho d'Água", *(f"Bairro {number:02d}" for number in range(1, 20))],
}


def tools_with(repository: FakeRepository, tmp_path: Path) -> tuple[SQLTools, Path]:
    log_path = tmp_path / "audit.jsonl"

    return SQLTools(repository, AuditLog(log_path)), log_path  # type: ignore[arg-type]


def read_log(log_path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]


def test_approved_sql_runs_with_the_limit_and_is_logged(tmp_path: Path) -> None:
    repository = FakeRepository()
    tools, log_path = tools_with(repository, tmp_path)

    result = tools.execute_sql("SELECT count(*) FROM orders", CONTEXT)

    assert repository.selected == [("SELECT count(*) FROM orders\nLIMIT 101", 100)]
    assert result.rows == [[1]]
    assert [(record["status"], record["row_count"]) for record in read_log(log_path)] == [("ok", 1)]


def test_input_is_validated(tmp_path: Path) -> None:
    repository = FakeRepository()
    tools, log_path = tools_with(repository, tmp_path)

    with pytest.raises(SQLRejected, match=r"10\.000 caracteres"):
        tools.execute_sql("SELECT " + "1" * 10_000, CONTEXT)

    assert repository.selected == []
    assert [record["status"] for record in read_log(log_path)] == ["rejected"]


def test_rejected_sql_never_reaches_the_repository(tmp_path: Path) -> None:
    repository = FakeRepository()
    tools, log_path = tools_with(repository, tmp_path)

    with pytest.raises(SQLRejected, match="DELETE"):
        tools.execute_sql("DELETE FROM orders", CONTEXT)

    assert repository.selected == []
    assert read_log(log_path)[0]["sql"] == "DELETE FROM orders"


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (QueryTimeout("passou do tempo"), "timeout"),
        (ExecutionError("Erro do Postgres"), "error"),
        (DatabaseUnavailable("o banco não respondeu"), "error"),
    ],
)
def test_repository_errors_are_logged_and_raised(tmp_path: Path, error: Exception, status: str) -> None:
    tools, log_path = tools_with(FakeRepository(error=error), tmp_path)

    with pytest.raises(type(error)):
        tools.execute_sql("SELECT 1", CONTEXT)

    assert [(record["status"], record["error"]) for record in read_log(log_path)] == [(status, str(error))]


def test_schema_description_lists_values_examples_and_ranges(tmp_path: Path) -> None:
    tools, _ = tools_with(FakeRepository(relations=[CUSTOMERS, ORDERS], values=VALUES), tmp_path)

    description = tools.describe_schema()

    assert "\n## customers_public (view)\nClientes, sem dados pessoais.\n" in description
    assert "\n## orders (tabela)\nPedidos.\n- id (integer): Identificador do pedido.\n" in description
    assert "- status (text): Situação do pedido. Valores: 'canceled', 'delivered'.\n" in description
    assert "Bairro do cliente. Exemplos: 'Olho d''Água', 'Bairro 01', 'Bairro 02'.\n" in description
    assert "Criação. De 2026-01-01 00:05:00 a 2026-10-31 23:55:00.\n" in description
    assert description.endswith("`EXTRACT(DOW FROM created_at)`, em que 0 é domingo e 6 é sábado.\n")


def test_schema_description_is_built_once(tmp_path: Path) -> None:
    repository = FakeRepository(relations=[ORDERS], values=VALUES)
    tools, _ = tools_with(repository, tmp_path)

    assert tools.describe_schema() == tools.describe_schema()
    assert repository.catalog_reads == 1


@pytest.mark.db
def test_runs_model_sql_on_postgres(tools: SQLTools) -> None:
    result = tools.execute_sql(
        "SELECT city, COUNT(*) AS restaurants FROM restaurants GROUP BY city ORDER BY city", CONTEXT
    )

    assert result.sql.endswith("\nLIMIT 101")
    assert result.columns == ["city", "restaurants"]
    assert len(result.rows) == 7


@pytest.mark.db
def test_cuts_the_result_at_100_rows(tools: SQLTools) -> None:
    result = tools.execute_sql("SELECT id FROM orders ORDER BY id", CONTEXT)

    assert len(result.rows) == 100
    assert result.truncated


@pytest.mark.db
def test_every_execution_is_logged(
    settings: Settings, repository: DeliveryRepository, tmp_path: Path
) -> None:
    log_path = tmp_path / "audit.jsonl"
    audit_log = AuditLog(log_path)
    tools = SQLTools(repository, audit_log)
    tools.execute_sql("SELECT count(*) FROM orders", CONTEXT)

    with pytest.raises(SQLRejected):
        tools.execute_sql("DELETE FROM orders", CONTEXT)

    with pytest.raises(ExecutionError):
        tools.execute_sql("SELECT nope FROM orders", CONTEXT)

    # Only the slow query gets the short time limit, so a busy machine cannot time out the others.
    fast = settings.model_copy(update={"statement_timeout_ms": 200})

    with DeliveryRepository(fast) as fast_repository, pytest.raises(QueryTimeout):
        SQLTools(fast_repository, audit_log).execute_sql(
            "SELECT count(*) FROM orders a, orders b, orders c", CONTEXT
        )

    records = read_log(log_path)

    assert [record["status"] for record in records] == ["ok", "rejected", "error", "timeout"]
    assert all(set(record) == FIELDS for record in records)
    assert all(record["request_id"] == "req-1" and record["source"] == "api" for record in records)
    assert all(record["question"] == "Quantos pedidos há?" for record in records)
    assert records[0]["row_count"] == 1
    assert str(records[0]["sql"]).endswith("\nLIMIT 101")
    assert records[1]["sql"] == "DELETE FROM orders"
    assert str(records[2]["error"]).startswith("Erro do Postgres")
