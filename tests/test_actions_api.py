"""The routes of the review action: a pending answer, then POST /actions/{action_id}/approve or /reject."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from delivery_agent.core.audit import AuditLog
from delivery_agent.core.settings import Settings
from delivery_agent.llm.prompts import ReviewDraft
from delivery_agent.main import Services, create_app
from delivery_agent.repositories.delivery_repository import DeliveryRepository
from delivery_agent.repositories.review_repository import ReviewRepository
from delivery_agent.services.review_actions import ReviewActions
from delivery_agent.services.sql_tools import SQLTools
from fakes import REVIEW, REVIEW_DRAFT, FakeModel, FakeRepository, FakeReviewDesk, FakeSQLTools

REQUEST = {"question": "Avalie o pedido 1234 com nota 4: chegou frio"}
ESSENTIAL = {"request_id", "status", "answer", "columns", "rows", "truncated"}


@contextmanager
def client_for(
    model: FakeModel | None,
    reviews: FakeReviewDesk | ReviewActions | None,
    *,
    tools: FakeSQLTools | SQLTools | None = None,
    repository: FakeRepository | DeliveryRepository | None = None,
) -> Iterator[TestClient]:
    services = Services(
        repository=repository or FakeRepository(),  # type: ignore[arg-type]
        tools=tools or FakeSQLTools(),  # type: ignore[arg-type]
        model=model,  # type: ignore[arg-type]
        reviews=reviews,
    )

    with TestClient(create_app(services)) as client:
        yield client


def test_a_review_request_comes_back_pending_with_what_will_be_written() -> None:
    desk = FakeReviewDesk()

    with client_for(FakeModel(REVIEW, REVIEW_DRAFT), desk) as client:
        response = client.post("/questions", json=REQUEST)

    body = response.json()

    assert response.status_code == 200
    assert set(body) == ESSENTIAL | {"action"}
    assert body["status"] == "pending_approval"
    assert body["action"]["type"] == "register_review"
    assert body["action"]["review"] == {"order_id": 1234, "rating": 4, "comment": "Chegou frio."}
    assert len(body["action"]["action_id"]) == 32
    assert desk.written == []


def test_approval_writes_once_and_a_second_decision_is_a_conflict() -> None:
    desk = FakeReviewDesk()

    with client_for(FakeModel(REVIEW, REVIEW_DRAFT), desk) as client:
        action_id = client.post("/questions", json=REQUEST).json()["action"]["action_id"]
        approved = client.post(f"/actions/{action_id}/approve")
        again = client.post(f"/actions/{action_id}/approve")
        rejected = client.post(f"/actions/{action_id}/reject")

    assert approved.status_code == 200
    assert approved.json() == {
        "action_id": action_id,
        "status": "written",
        "answer": "Avaliação registrada: pedido 1234, nota 4.",
        "review": {"order_id": 1234, "rating": 4, "comment": "Chegou frio."},
    }
    assert (again.status_code, rejected.status_code) == (409, 409)
    assert "já foi aprovada" in again.json()["detail"]
    assert len(desk.written) == 1


def test_rejection_writes_nothing() -> None:
    desk = FakeReviewDesk()

    with client_for(FakeModel(REVIEW, REVIEW_DRAFT), desk) as client:
        action_id = client.post("/questions", json=REQUEST).json()["action"]["action_id"]
        rejected = client.post(f"/actions/{action_id}/reject")
        approved = client.post(f"/actions/{action_id}/approve")

    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert approved.status_code == 409
    assert desk.written == []


@pytest.mark.parametrize("ask_first", [False, True])
def test_an_unknown_action_is_not_found(ask_first: bool) -> None:
    with client_for(FakeModel(REVIEW, REVIEW_DRAFT), FakeReviewDesk()) as client:
        if ask_first:
            client.post("/questions", json=REQUEST)

        response = client.post("/actions/nope/approve")

    assert response.status_code == 404
    assert "Não encontrei a ação nope" in response.json()["detail"]


def test_a_blocked_review_is_refused_and_has_no_action() -> None:
    blocked = FakeReviewDesk(blocked="O pedido 1234 já tem uma avaliação, e cada pedido só pode ter uma.")

    with client_for(FakeModel(REVIEW, REVIEW_DRAFT), blocked) as client:
        body = client.post("/questions", json=REQUEST).json()

    assert set(body) == ESSENTIAL
    assert (body["status"], body["answer"]) == ("refused", blocked.blocked)


def test_docs_show_the_action_routes_and_a_pending_answer() -> None:
    with client_for(None, None) as client:
        schema = client.get("/openapi.json").json()

    examples = schema["paths"]["/questions"]["post"]["responses"]["200"]["content"]["application/json"]

    assert {"/actions/{action_id}/approve", "/actions/{action_id}/reject"} <= set(schema["paths"])
    assert examples["examples"]["acao_pendente"]["value"]["status"] == "pending_approval"


@pytest.mark.db
def test_an_approved_review_reaches_postgres_once(
    settings: Settings,
    repository: DeliveryRepository,
    writer_conn: psycopg.Connection,
    admin_conn: psycopg.Connection,
    unreviewed_order: int,
    tmp_path: Path,
) -> None:
    reviews = ReviewActions(repository, ReviewRepository(settings), AuditLog(tmp_path / "audit.jsonl"))
    draft = ReviewDraft(order_id=unreviewed_order, rating=4, comment="Chegou frio.")
    tools = SQLTools(repository, AuditLog(tmp_path / "audit.jsonl"))

    with client_for(FakeModel(REVIEW, draft), reviews, tools=tools, repository=repository) as client:
        pending = client.post("/questions", json=REQUEST).json()
        before = admin_conn.execute("SELECT count(*) FROM reviews WHERE order_id = %s", (unreviewed_order,))
        written_before = before.fetchone()
        admin_conn.commit()
        action_id = pending["action"]["action_id"]
        first = client.post(f"/actions/{action_id}/approve")
        second = client.post(f"/actions/{action_id}/approve")

    after = admin_conn.execute(
        "SELECT rating, comment FROM reviews WHERE order_id = %s", (unreviewed_order,)
    ).fetchall()

    assert pending["status"] == "pending_approval"
    assert written_before == (0,)
    assert (first.status_code, first.json()["status"], second.status_code) == (200, "written", 409)
    assert after == [(4, "Chegou frio.")]
