"""The review action through the graph: the pause for approval, the decision and the write."""

import json
from pathlib import Path

import pytest

from delivery_agent.core.audit import AuditLog
from delivery_agent.core.errors import ActionAlreadyDecided, ActionNotFound, DuplicateReview
from delivery_agent.repositories.delivery_repository import OrderReviewState
from delivery_agent.services.graph import MODEL_CUT, MODEL_REFUSED, REVIEWS_OFF
from delivery_agent.services.question_service import AgentAnswer, QuestionService
from delivery_agent.services.review_actions import ReviewActions, ReviewProposal
from fakes import (
    COUNT_SQL,
    DONE,
    QUERY,
    REVIEW,
    REVIEW_DRAFT,
    FakeModel,
    FakeOrders,
    FakeReviewDesk,
    FakeReviewWriter,
    FakeSQLTools,
)

REQUEST = "Avalie o pedido 1234 com nota 4: chegou frio"
PROPOSAL = ReviewProposal(order_id=1234, rating=4, comment="Chegou frio.")


def ask(
    desk: FakeReviewDesk | ReviewActions | None = None, model: FakeModel | None = None
) -> tuple[QuestionService, AgentAnswer]:
    service = QuestionService(FakeSQLTools(), model or FakeModel(REVIEW, REVIEW_DRAFT), desk)

    return service, service.ask(REQUEST, source="api")


def action_id_of(answer: AgentAnswer) -> str:
    assert answer.action is not None

    return answer.action.action_id


def test_a_review_request_waits_for_approval_without_writing() -> None:
    desk = FakeReviewDesk()
    model = FakeModel(REVIEW, REVIEW_DRAFT)

    _, answer = ask(desk, model)

    assert answer.status == "pending_approval"
    assert answer.action is not None
    assert answer.action.review == PROPOSAL
    assert answer.answer == (
        "Pronto para registrar a avaliação do pedido 1234, com nota 4 e um comentário. "
        "Ela só será gravada depois da sua aprovação."
    )
    assert (answer.sql, answer.rows) == (None, [])
    assert desk.written == []
    assert REQUEST in model.requests[1].prompt
    assert [request.effort for request in model.requests] == ["low", "low"]


def test_approval_writes_the_review_once() -> None:
    desk = FakeReviewDesk()
    service, answer = ask(desk)

    result = service.decide(action_id_of(answer), approve=True)

    assert (result.status, result.answer) == ("written", "Avaliação registrada: pedido 1234, nota 4.")
    assert result.review == PROPOSAL

    with pytest.raises(ActionAlreadyDecided, match="já foi aprovada, e a avaliação foi gravada"):
        service.decide(action_id_of(answer), approve=True)

    assert desk.written == [PROPOSAL]
    assert desk.steps == ["proposed", "approved"]


def test_rejection_writes_nothing() -> None:
    desk = FakeReviewDesk()
    service, answer = ask(desk)

    result = service.decide(action_id_of(answer), approve=False)

    assert (result.status, result.answer) == ("rejected", "Avaliação descartada: nada foi gravado.")

    with pytest.raises(ActionAlreadyDecided, match="já foi recusada"):
        service.decide(action_id_of(answer), approve=True)

    assert desk.written == []
    assert desk.steps == ["proposed", "rejected"]


def test_an_unknown_action_is_not_found() -> None:
    service, _ = ask()

    with pytest.raises(ActionNotFound, match="Não encontrei a ação nope"):
        service.decide("nope", approve=True)


def test_a_failed_write_is_reported_and_cannot_be_retried() -> None:
    service, answer = ask(FakeReviewDesk(write_error=DuplicateReview("O pedido 1234 já tem uma avaliação.")))

    result = service.decide(action_id_of(answer), approve=True)

    assert result.status == "failed"
    assert result.answer == "A avaliação não foi gravada: O pedido 1234 já tem uma avaliação."

    with pytest.raises(ActionAlreadyDecided, match="a gravação falhou"):
        service.decide(action_id_of(answer), approve=True)


def test_a_blocked_review_is_refused_without_a_pause() -> None:
    message = "O pedido 1234 não foi entregue, então não pode ser avaliado."

    _, answer = ask(FakeReviewDesk(blocked=message))

    assert (answer.status, answer.answer) == ("refused", message)
    assert answer.action is None
    assert "action" not in answer.model_fields_set


def test_without_the_review_writer_every_review_is_refused() -> None:
    _, answer = ask(None)

    assert (answer.status, answer.answer) == ("refused", REVIEWS_OFF)


@pytest.mark.parametrize(
    ("stop", "status", "message"),
    [("refusal", "refused", MODEL_REFUSED), ("max_tokens", "failed", MODEL_CUT)],
)
def test_a_draft_without_output_ends_the_request(stop: str, status: str, message: str) -> None:
    desk = FakeReviewDesk()

    _, answer = ask(desk, FakeModel(REVIEW, stop))

    assert (answer.status, answer.answer) == (status, message)
    assert desk.steps == []


def test_only_pending_actions_stay_in_memory() -> None:
    service = QuestionService(FakeSQLTools(), FakeModel(QUERY, COUNT_SQL, DONE, REVIEW, REVIEW_DRAFT))

    service.ask("Quantos pedidos há?", source="api")

    assert list(service._checkpointer.list(None)) == []

    service.ask(REQUEST, source="api")

    # No desk: the review is refused, so nothing waits for approval either.
    assert list(service._checkpointer.list(None)) == []


def test_the_audit_log_follows_the_action_from_the_proposal_to_the_write(tmp_path: Path) -> None:
    writer = FakeReviewWriter()
    orders = FakeOrders({1234: OrderReviewState(status="delivered", reviewed=False)})
    log_path = tmp_path / "audit.jsonl"
    service, answer = ask(ReviewActions(orders, writer, AuditLog(log_path)))

    service.decide(action_id_of(answer), approve=True)
    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]

    assert [record["event"] for record in records] == ["proposed", "approved", "written"]
    assert {record["action_id"] for record in records} == {action_id_of(answer)}
    assert {record["request_id"] for record in records} == {answer.request_id}
    assert all(record["question"] == REQUEST for record in records)
    assert writer.inserted == [(1234, 4, "Chegou frio.")]
