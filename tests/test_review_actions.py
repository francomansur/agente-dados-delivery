"""The rules of the review action, its audit log and its write, with fakes and against Postgres."""

import json
from pathlib import Path

import psycopg
import pytest

from delivery_agent.core.audit import ActionContext, AuditLog
from delivery_agent.core.errors import DatabaseUnavailable, DuplicateReview, ReviewBlocked
from delivery_agent.core.settings import Settings
from delivery_agent.llm.prompts import ReviewDraft
from delivery_agent.repositories.delivery_repository import DeliveryRepository, OrderReviewState
from delivery_agent.repositories.review_repository import ReviewRepository
from delivery_agent.services.review_actions import ReviewActions, ReviewProposal
from fakes import FakeOrders, FakeReviewWriter

CONTEXT = ActionContext(
    request_id="req-1",
    source="api",
    question="Avalie o pedido 1234 com nota 4: chegou frio",
    action_id="act-1",
)
DELIVERED = OrderReviewState(status="delivered", reviewed=False)
ORDERS = FakeOrders(
    {
        1234: DELIVERED,
        2000: OrderReviewState(status="canceled", reviewed=False),
        3000: OrderReviewState(status="delivered", reviewed=True),
    }
)
FIELDS = {
    "timestamp",
    "request_id",
    "source",
    "question",
    "action_id",
    "action",
    "event",
    "order_id",
    "rating",
    "comment",
    "error",
}


def actions_with(tmp_path: Path, writer: FakeReviewWriter | None = None) -> tuple[ReviewActions, Path]:
    log_path = tmp_path / "audit.jsonl"

    return ReviewActions(ORDERS, writer or FakeReviewWriter(), AuditLog(log_path)), log_path


def read_log(log_path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]


def test_a_valid_request_becomes_a_proposal_and_is_logged(tmp_path: Path) -> None:
    actions, log_path = actions_with(tmp_path)

    proposal = actions.propose(ReviewDraft(order_id=1234, rating=4, comment="  Chegou frio.  "), CONTEXT)
    [record] = read_log(log_path)

    assert proposal == ReviewProposal(order_id=1234, rating=4, comment="Chegou frio.")
    assert set(record) == FIELDS
    assert (record["event"], record["action_id"], record["request_id"]) == ("proposed", "act-1", "req-1")
    assert (record["order_id"], record["rating"], record["comment"]) == (1234, 4, "Chegou frio.")


def test_an_empty_comment_is_no_comment(tmp_path: Path) -> None:
    actions, _ = actions_with(tmp_path)

    assert actions.propose(ReviewDraft(order_id=1234, rating=5, comment="  "), CONTEXT).comment is None


@pytest.mark.parametrize(
    ("draft", "message"),
    [
        (ReviewDraft(order_id=None, rating=4, comment=None), "diga o número do pedido"),
        (ReviewDraft(order_id=1234, rating=None, comment=None), "diga a nota, de 1 a 5"),
        (ReviewDraft(order_id=1234, rating=0, comment=None), "A nota precisa ser de 1 a 5"),
        (ReviewDraft(order_id=1234, rating=6, comment=None), "A nota precisa ser de 1 a 5"),
        (ReviewDraft(order_id=1234, rating=4, comment="a" * 301), "até 300 caracteres"),
        (ReviewDraft(order_id=9999, rating=4, comment=None), "Não encontrei o pedido 9999"),
        (ReviewDraft(order_id=2000, rating=4, comment=None), "O pedido 2000 não foi entregue"),
        (ReviewDraft(order_id=3000, rating=4, comment=None), "O pedido 3000 já tem uma avaliação"),
    ],
)
def test_the_rules_block_the_request_and_the_block_is_logged(
    draft: ReviewDraft, message: str, tmp_path: Path
) -> None:
    writer = FakeReviewWriter()
    actions, log_path = actions_with(tmp_path, writer)

    with pytest.raises(ReviewBlocked, match=message):
        actions.propose(draft, CONTEXT)

    [record] = read_log(log_path)

    assert record["event"] == "blocked"
    assert message in str(record["error"])
    assert writer.inserted == []


def test_each_decision_and_the_write_are_logged(tmp_path: Path) -> None:
    writer = FakeReviewWriter()
    actions, log_path = actions_with(tmp_path, writer)
    proposal = ReviewProposal(order_id=1234, rating=4, comment="Chegou frio.")

    actions.decide(proposal, CONTEXT, approved=False)
    actions.decide(proposal, CONTEXT, approved=True)
    actions.write(proposal, CONTEXT)

    assert [record["event"] for record in read_log(log_path)] == ["rejected", "approved", "written"]
    assert writer.inserted == [(1234, 4, "Chegou frio.")]


@pytest.mark.parametrize(
    "error",
    [DuplicateReview("O pedido 1234 já tem uma avaliação."), DatabaseUnavailable("o banco não respondeu")],
)
def test_a_failed_write_is_logged_and_raised(error: Exception, tmp_path: Path) -> None:
    actions, log_path = actions_with(tmp_path, FakeReviewWriter(error=error))
    proposal = ReviewProposal(order_id=1234, rating=4, comment=None)

    with pytest.raises(type(error)):
        actions.write(proposal, CONTEXT)

    [record] = read_log(log_path)

    assert (record["event"], record["error"]) == ("write_failed", str(error))


@pytest.mark.db
def test_the_rules_read_postgres_and_the_write_goes_through_review_writer(
    settings: Settings,
    repository: DeliveryRepository,
    writer_conn: psycopg.Connection,
    admin_conn: psycopg.Connection,
    unreviewed_order: int,
    tmp_path: Path,
) -> None:
    actions = ReviewActions(repository, ReviewRepository(settings), AuditLog(tmp_path / "audit.jsonl"))
    draft = ReviewDraft(order_id=unreviewed_order, rating=2, comment="Demorou.")

    actions.write(actions.propose(draft, CONTEXT), CONTEXT)
    review = admin_conn.execute(
        "SELECT rating, comment FROM reviews WHERE order_id = %s", (unreviewed_order,)
    ).fetchone()

    assert review == (2, "Demorou.")

    with pytest.raises(ReviewBlocked, match="já tem uma avaliação"):
        actions.propose(draft, CONTEXT)
