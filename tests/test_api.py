import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import anthropic
import httpx2
import pytest
from fastapi.testclient import TestClient

from delivery_agent.api.routes import AnswerSummary
from delivery_agent.core.audit import AuditLog
from delivery_agent.core.settings import Settings
from delivery_agent.llm.prompts import Understanding
from delivery_agent.main import Services, app, create_app
from delivery_agent.repositories.delivery_repository import DeliveryRepository
from delivery_agent.services.question_service import AgentAnswer
from delivery_agent.services.sql_tools import SQLTools
from fakes import COUNT_SQL, DONE, QUERY, FakeModel, FakeRepository, FakeSQLTools

ESSENTIAL = {"request_id", "status", "answer", "columns", "rows", "truncated"}
DETAILS = {"sql", "attempts", "usage", "duration_ms"}
QUESTION = {"question": "Quantos pedidos há?"}
OUT_OF_SCOPE = Understanding(intent="out_of_scope", reason="A pergunta pede para apagar dados.")


@contextmanager
def client_for(
    model: FakeModel | None,
    *,
    tools: FakeSQLTools | SQLTools | None = None,
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


def test_the_answer_has_only_the_essential_by_default() -> None:
    with client_for(FakeModel(QUERY, COUNT_SQL, DONE)) as client:
        response = client.post("/questions", json=QUESTION)

    body = response.json()

    assert response.status_code == 200
    assert set(body) == ESSENTIAL
    assert body["status"] == "answered"
    assert body["answer"] == "Há 1 pedido."
    assert body["columns"] == ["n"]
    assert body["rows"] == [[1]]


def test_details_come_only_when_asked() -> None:
    with client_for(FakeModel(QUERY, COUNT_SQL, DONE)) as client:
        response = client.post("/questions", params={"include_details": True}, json=QUESTION)

    body = response.json()

    assert response.status_code == 200
    assert set(body) == ESSENTIAL | DETAILS
    assert body["sql"] == "SELECT count(*) AS n FROM orders\nLIMIT 101"
    assert body["attempts"] == 1
    assert body["usage"]["model_calls"] == 3
    assert body["duration_ms"] >= 0


def test_a_refusal_has_no_sql_by_default_and_a_null_one_with_details() -> None:
    with client_for(FakeModel(OUT_OF_SCOPE)) as client:
        default = client.post("/questions", json=QUESTION).json()

    with client_for(FakeModel(OUT_OF_SCOPE)) as client:
        detailed = client.post("/questions?include_details=true", json=QUESTION).json()

    assert default["status"] == detailed["status"] == "refused"
    assert "sql" not in default
    assert detailed["sql"] is None


def test_the_audit_log_keeps_the_sql_that_the_default_answer_leaves_out(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.jsonl"
    repository = FakeRepository()
    tools = SQLTools(repository, AuditLog(log_path))  # type: ignore[arg-type]

    with client_for(FakeModel(QUERY, COUNT_SQL, DONE), tools=tools, repository=repository) as client:
        body = client.post("/questions", json=QUESTION).json()

    record = json.loads(log_path.read_text(encoding="utf-8"))

    assert "sql" not in body
    assert record["sql"] == "SELECT count(*) AS n FROM orders\nLIMIT 101"
    assert record["status"] == "ok"
    assert record["question"] == "Quantos pedidos há?"


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


def test_docs_show_the_parameter_and_both_answers() -> None:
    with client_for(None) as client:
        operation = client.get("/openapi.json").json()["paths"]["/questions"]["post"]

    parameter = operation["parameters"][0]
    examples = operation["responses"]["200"]["content"]["application/json"]["examples"]

    assert parameter["name"] == "include_details"
    assert parameter["in"] == "query"
    assert parameter["schema"]["default"] is False
    assert set(examples["padrao"]["value"]) == ESSENTIAL
    assert set(examples["com_detalhes"]["value"]) == ESSENTIAL | DETAILS


def test_the_summary_keeps_the_fields_of_the_full_answer() -> None:
    full, summary = AgentAnswer.model_fields, AnswerSummary.model_fields

    assert set(summary) == ESSENTIAL
    assert all(summary[name].annotation == full[name].annotation for name in summary)
    assert all(summary[name].description == full[name].description for name in summary)


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
