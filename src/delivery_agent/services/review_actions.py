"""The only write action: register the review of a delivered order, after a person approves it.

The model never writes SQL that changes data: it only extracts the order, the rating and the comment.
This service applies the rules before anything goes for approval (the order exists and was
delivered, it has no review yet, the rating goes from 1 to 5 and the comment is short), reading the
database through the read-only repository. The write runs only after the approval, through
``ReviewRepository``, as ``review_writer``. Every step goes to the audit log.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field

from delivery_agent.core.audit import ActionContext, ActionEvent, ActionRecord, AuditLog
from delivery_agent.core.errors import DatabaseUnavailable, DuplicateReview, ExecutionError, ReviewBlocked
from delivery_agent.llm.prompts import ReviewDraft
from delivery_agent.repositories.delivery_repository import OrderReviewState

MAX_COMMENT_LENGTH = 300


class ReviewProposal(BaseModel):
    """What will be written, after the rules: the order, the rating and the comment."""

    order_id: int = Field(description="Pedido avaliado.")
    rating: int = Field(ge=1, le=5, description="Nota de 1 a 5.")
    comment: str | None = Field(max_length=MAX_COMMENT_LENGTH, description="Comentário, ou null.")


class OrderReader(Protocol):
    """The read the rules need. ``DeliveryRepository`` implements it, as the read-only role."""

    def order_review_state(self, order_id: int) -> OrderReviewState | None:
        """Read the status of an order and whether it has a review."""
        ...


class ReviewWriter(Protocol):
    """The write after the approval. ``ReviewRepository`` implements it, as ``review_writer``."""

    def insert(self, *, order_id: int, rating: int, comment: str | None) -> None:
        """Insert one approved review."""
        ...


class ReviewActions:
    """Check, log and write the reviews that people ask for and approve."""

    def __init__(self, reader: OrderReader, writer: ReviewWriter, audit_log: AuditLog) -> None:
        self._reader = reader
        self._writer = writer
        self._audit_log = audit_log

    def propose(self, draft: ReviewDraft, context: ActionContext) -> ReviewProposal:
        """Apply the rules to what the model extracted and log the outcome.

        Args:
            draft: Order, rating and comment, as the model read them in the request.
            context: Request and action, for the audit log.

        Returns:
            The review that will be written if a person approves it.

        Raises:
            ReviewBlocked: A rule refused the request; the message says which, in Portuguese.
            DatabaseUnavailable: The database cannot be reached.
        """
        request = draft.model_copy(update={"comment": (draft.comment or "").strip() or None})

        try:
            proposal = self._check(request)
        except ReviewBlocked as error:
            self._log(context, "blocked", request, error=str(error))

            raise

        self._log(context, "proposed", proposal)

        return proposal

    def decide(self, proposal: ReviewProposal, context: ActionContext, *, approved: bool) -> None:
        """Log the decision of the person who approved or rejected the review.

        Args:
            proposal: The review that went for approval.
            context: Request and action, for the audit log.
            approved: Whether the person approved it.
        """
        self._log(context, "approved" if approved else "rejected", proposal)

    def write(self, proposal: ReviewProposal, context: ActionContext) -> None:
        """Write an approved review as ``review_writer`` and log the outcome.

        Args:
            proposal: The review that the person approved.
            context: Request and action, for the audit log.

        Raises:
            DuplicateReview: The order got a review in the meantime.
            ExecutionError: Postgres refused the review.
            DatabaseUnavailable: The database cannot be reached.
        """
        try:
            self._writer.insert(order_id=proposal.order_id, rating=proposal.rating, comment=proposal.comment)
        except (DuplicateReview, ExecutionError, DatabaseUnavailable) as error:
            self._log(context, "write_failed", proposal, error=str(error))

            raise

        self._log(context, "written", proposal)

    def _check(self, request: ReviewDraft) -> ReviewProposal:
        order_id, rating, comment = request.order_id, request.rating, request.comment

        if order_id is None:
            raise ReviewBlocked("Para registrar uma avaliação, diga o número do pedido.")

        if rating is None:
            raise ReviewBlocked("Para registrar uma avaliação, diga a nota, de 1 a 5.")

        if not 1 <= rating <= 5:
            raise ReviewBlocked(f"A nota precisa ser de 1 a 5, e o pedido foi de nota {rating}.")

        if comment is not None and len(comment) > MAX_COMMENT_LENGTH:
            raise ReviewBlocked(
                f"O comentário pode ter até {MAX_COMMENT_LENGTH} caracteres, e este tem {len(comment)}."
            )

        state = self._reader.order_review_state(order_id)

        if state is None:
            raise ReviewBlocked(f"Não encontrei o pedido {order_id}.")

        if state.status != "delivered":
            raise ReviewBlocked(f"O pedido {order_id} não foi entregue, então não pode ser avaliado.")

        if state.reviewed:
            raise ReviewBlocked(f"O pedido {order_id} já tem uma avaliação, e cada pedido só pode ter uma.")

        return ReviewProposal(order_id=order_id, rating=rating, comment=comment)

    def _log(
        self,
        context: ActionContext,
        event: ActionEvent,
        review: ReviewDraft | ReviewProposal,
        *,
        error: str | None = None,
    ) -> None:
        self._audit_log.record(
            ActionRecord(
                request_id=context.request_id,
                source=context.source,
                question=context.question,
                action_id=context.action_id,
                action="register_review",
                event=event,
                order_id=review.order_id,
                rating=review.rating,
                comment=review.comment,
                error=error,
            )
        )
