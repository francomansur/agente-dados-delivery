"""The only write to the database: the reviews that a person approved, as ``review_writer``.

The role can only insert into ``reviews`` (security layer 1 for writes): no SELECT, UPDATE or DELETE,
and the database generates the id. The SQL is fixed and the values go as parameters, so the model
never writes SQL that changes data. The rules and the approval come first, in
``services.review_actions``.
"""

from __future__ import annotations

import psycopg

from delivery_agent.core.errors import DatabaseUnavailable, DuplicateReview, ExecutionError
from delivery_agent.core.settings import Settings

# The dates of the base are in local time, without time zone, like the rest of the data.
_INSERT_REVIEW = """
    INSERT INTO reviews (order_id, rating, comment, created_at)
    VALUES (%s, %s, %s, date_trunc('second', now() AT TIME ZONE 'America/Sao_Paulo'))
"""


class ReviewRepository:
    """Writes as ``review_writer``, with a short connection for each approved review."""

    def __init__(self, settings: Settings) -> None:
        self._conninfo = settings.review_conninfo()

    def insert(self, *, order_id: int, rating: int, comment: str | None) -> None:
        """Insert one approved review and commit it.

        Args:
            order_id: Order being reviewed.
            rating: Rating from 1 to 5.
            comment: Comment of the review, if any.

        Raises:
            DuplicateReview: The order already has a review.
            ExecutionError: Postgres refused the review, as when the order does not exist.
            DatabaseUnavailable: The database cannot be reached.
        """
        try:
            with psycopg.connect(self._conninfo, connect_timeout=5) as conn:
                conn.execute(_INSERT_REVIEW, (order_id, rating, comment))
        except psycopg.errors.UniqueViolation as error:
            raise DuplicateReview(f"O pedido {order_id} já tem uma avaliação.") from error
        except psycopg.OperationalError as error:
            raise DatabaseUnavailable(f"O banco de dados não respondeu: {error}") from error
        except psycopg.Error as error:
            message = error.diag.message_primary or str(error)

            raise ExecutionError(f"Erro do Postgres: {message}") from error
