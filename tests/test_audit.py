import json
from pathlib import Path

import pytest

from delivery_agent.core.audit import AuditLog, QueryContext
from delivery_agent.core.errors import ExecutionError, QueryTimeout, SQLRejected
from delivery_agent.core.settings import Settings
from delivery_agent.repositories.delivery_repository import Database

pytestmark = pytest.mark.db

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


def test_every_execution_is_logged(settings: Settings, tmp_path: Path) -> None:
    log_path = tmp_path / "audit.jsonl"
    context = QueryContext(request_id="req-1", source="api", question="Quantos pedidos há?")
    fast = settings.model_copy(update={"statement_timeout_ms": 200})

    with Database(fast, AuditLog(log_path)) as database:
        database.execute_sql("SELECT count(*) FROM orders", context)

        with pytest.raises(SQLRejected):
            database.execute_sql("DELETE FROM orders", context)

        with pytest.raises(ExecutionError):
            database.execute_sql("SELECT nope FROM orders", context)

        with pytest.raises(QueryTimeout):
            database.execute_sql("SELECT count(*) FROM orders a, orders b, orders c", context)

    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]

    assert [record["status"] for record in records] == ["ok", "rejected", "error", "timeout"]
    assert all(set(record) == FIELDS for record in records)
    assert all(record["request_id"] == "req-1" and record["source"] == "api" for record in records)
    assert all(record["question"] == "Quantos pedidos há?" for record in records)
    assert records[0]["row_count"] == 1
    assert records[0]["sql"].endswith("\nLIMIT 101")
    assert records[1]["sql"] == "DELETE FROM orders"
    assert records[2]["error"].startswith("Erro do Postgres")
