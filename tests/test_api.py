from collections.abc import Iterator
from contextlib import contextmanager

import anthropic
import httpx2
import pytest
from fastapi.testclient import TestClient

from delivery_agent.core.settings import Settings
from delivery_agent.main import Services, app, create_app
from delivery_agent.repositories.delivery_repository import DeliveryRepository
from fakes import COUNT_SQL, DONE, QUERY, FakeModel, FakeRepository, FakeSQLTools

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


@contextmanager
def client_for(
    model: FakeModel | None,
    *,
    tools: FakeSQLTools | None = None,
    repository: FakeRepository | DeliveryRepository | None = None,
) -> Iterator[TestClient]:
    services = Services(
        repository=repository or FakeRepository(),  # type: ignore[arg-type]
        tools=tools or FakeSQLTools(),  # type: ignore[arg-type]
        model=model,  # type: ignore[arg-type]
    )

    # The context manager runs the lifespan, which hands the services to the routes.
    with TestClient(create_app(services)) as client:
        yield client


def test_question_returns_sql_and_rows() -> None:
    with client_for(FakeModel(QUERY, COUNT_SQL, DONE)) as client:
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
    with client_for(FakeModel()) as client:
        response = client.post("/questions", json={"question": question})

    assert response.status_code == 422


def test_missing_api_key_returns_503() -> None:
    with client_for(None) as client:
        response = client.post("/questions", json={"question": "Quantos pedidos há?"})

    assert response.status_code == 503
    assert "ANTHROPIC_API_KEY" in response.json()["detail"]


def test_model_api_error_returns_502() -> None:
    error = anthropic.APIConnectionError(
        request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    )

    with client_for(FakeModel(error)) as client:
        response = client.post("/questions", json={"question": "Quantos pedidos há?"})

    assert response.status_code == 502
    assert response.json()["detail"].startswith("A API do modelo falhou")


def test_database_down_returns_503() -> None:
    with client_for(FakeModel(QUERY, COUNT_SQL, DONE), tools=FakeSQLTools(healthy=False)) as client:
        response = client.post("/questions", json={"question": "Quantos pedidos há?"})

    assert response.status_code == 503
    assert response.json()["detail"].startswith("O banco de dados está fora do ar")


def test_health_reports_a_database_that_is_down() -> None:
    with client_for(None, repository=FakeRepository(healthy=False)) as client:
        response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "detail": "O banco de dados não respondeu."}


def test_docs_have_examples() -> None:
    with client_for(None) as client:
        schema = client.get("/openapi.json").json()

    assert schema["info"]["title"] == "Agente de dados de delivery"
    assert schema["components"]["schemas"]["QuestionRequest"]["properties"]["question"]["examples"]
    assert schema["components"]["schemas"]["AgentAnswer"]["examples"][0]["status"] == "answered"


@pytest.mark.db
def test_health(repository: DeliveryRepository) -> None:
    # The app that uvicorn runs: its lifespan builds the services from the settings. The fixture only
    # skips the test when Postgres is down.
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "detail": None}


def test_health_with_an_unreachable_database(settings: Settings) -> None:
    unreachable = settings.model_copy(update={"database_port": 1})
    repository = DeliveryRepository(unreachable)
    repository.open(wait=False)

    with client_for(None, repository=repository) as client:
        response = client.get("/health")

    assert response.status_code == 503
