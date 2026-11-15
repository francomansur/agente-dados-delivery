from pathlib import Path

import anthropic
import httpx2
import pytest
from fastapi.testclient import TestClient

from delivery_agent.api.routes import Services, create_app
from delivery_agent.core.audit import AuditLog
from delivery_agent.core.settings import Settings
from delivery_agent.repositories.delivery_repository import Database
from fakes import COUNT_SQL, DONE, QUERY, FakeDatabase, FakeModel

FIELDS = {
    "request_id",
    "status",
    "answer",
    "sql",
    "columns",
    "rows",
    "truncated",
    "attempts",
    "usage",
    "duration_ms",
}


def client_for(model: FakeModel | None, database: FakeDatabase | None = None) -> TestClient:
    return TestClient(create_app(Services(database=database or FakeDatabase(), model=model)))


def test_question_returns_sql_and_rows() -> None:
    client = client_for(FakeModel(QUERY, COUNT_SQL, DONE))

    response = client.post("/questions", json={"question": "Quantos pedidos há?"})

    body = response.json()

    assert response.status_code == 200
    assert set(body) == FIELDS
    assert body["status"] == "answered"
    assert body["answer"] == "Há 1 pedido."
    assert body["sql"] == "SELECT count(*) AS n FROM orders\nLIMIT 101"
    assert body["columns"] == ["n"]
    assert body["rows"] == [[1]]
    assert body["usage"]["model_calls"] == 3


@pytest.mark.parametrize("question", ["", "oi", "x" * 501])
def test_invalid_question_returns_422(question: str) -> None:
    response = client_for(FakeModel()).post("/questions", json={"question": question})

    assert response.status_code == 422


def test_missing_api_key_returns_503() -> None:
    response = client_for(None).post("/questions", json={"question": "Quantos pedidos há?"})

    assert response.status_code == 503
    assert "ANTHROPIC_API_KEY" in response.json()["detail"]


def test_model_api_error_returns_502() -> None:
    error = anthropic.APIConnectionError(
        request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    )

    response = client_for(FakeModel(error)).post("/questions", json={"question": "Quantos pedidos há?"})

    assert response.status_code == 502
    assert response.json()["detail"].startswith("A API do modelo falhou")


def test_database_down_returns_503() -> None:
    client = client_for(FakeModel(QUERY, COUNT_SQL, DONE), FakeDatabase(healthy=False))

    response = client.post("/questions", json={"question": "Quantos pedidos há?"})

    assert response.status_code == 503
    assert response.json()["detail"].startswith("O banco de dados está fora do ar")


def test_health_reports_a_database_that_is_down() -> None:
    response = client_for(None, FakeDatabase(healthy=False)).get("/health")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "detail": "O banco de dados não respondeu."}


def test_docs_have_examples() -> None:
    schema = client_for(None).get("/openapi.json").json()

    assert schema["info"]["title"] == "Agente de dados de delivery"
    assert schema["components"]["schemas"]["QuestionRequest"]["properties"]["question"]["examples"]
    assert schema["components"]["schemas"]["AgentAnswer"]["examples"][0]["status"] == "answered"


@pytest.mark.db
def test_health(settings: Settings, tmp_path: Path) -> None:
    database = Database(settings, AuditLog(tmp_path / "audit.jsonl"))
    database.open()

    with TestClient(create_app(Services(database=database, model=None))) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "detail": None}


def test_health_with_an_unreachable_database(settings: Settings, tmp_path: Path) -> None:
    unreachable = settings.model_copy(update={"database_port": 1})
    database = Database(unreachable, AuditLog(tmp_path / "audit.jsonl"))
    database.open(wait=False)

    with TestClient(create_app(Services(database=database, model=None))) as client:
        response = client.get("/health")

    assert response.status_code == 503
