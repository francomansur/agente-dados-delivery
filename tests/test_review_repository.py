"""The write of the review action, as review_writer, straight against Postgres."""

import psycopg
import pytest
from pydantic import SecretStr

from delivery_agent.core.errors import DatabaseUnavailable, DuplicateReview, ExecutionError
from delivery_agent.core.settings import Settings
from delivery_agent.repositories.review_repository import ReviewRepository


@pytest.fixture
def writer(settings: Settings, writer_conn: psycopg.Connection) -> ReviewRepository:
    # writer_conn checks that Postgres is up and that REVIEW_DB_PASSWORD is set.
    return ReviewRepository(settings)


@pytest.mark.db
def test_inserts_a_review_with_the_local_date(
    writer: ReviewRepository, admin_conn: psycopg.Connection, unreviewed_order: int
) -> None:
    writer.insert(order_id=unreviewed_order, rating=5, comment="Chegou quente.")
    review = admin_conn.execute(
        "SELECT rating, comment, created_at::date = (now() AT TIME ZONE 'America/Sao_Paulo')::date "
        "FROM reviews WHERE order_id = %s",
        (unreviewed_order,),
    ).fetchone()

    assert review == (5, "Chegou quente.", True)


@pytest.mark.db
def test_a_second_review_of_the_same_order_is_refused(
    writer: ReviewRepository, unreviewed_order: int
) -> None:
    writer.insert(order_id=unreviewed_order, rating=5, comment=None)

    with pytest.raises(DuplicateReview, match=f"O pedido {unreviewed_order} já tem uma avaliação"):
        writer.insert(order_id=unreviewed_order, rating=1, comment="De novo.")


@pytest.mark.db
@pytest.mark.parametrize(("order_id", "rating"), [(999_999_999, 4), (None, 9)])
def test_the_database_checks_the_order_and_the_rating(
    writer: ReviewRepository, unreviewed_order: int, order_id: int | None, rating: int
) -> None:
    with pytest.raises(ExecutionError, match="Erro do Postgres"):
        writer.insert(order_id=order_id or unreviewed_order, rating=rating, comment=None)


def test_without_the_database_the_write_fails_clearly(settings: Settings) -> None:
    unreachable = settings.model_copy(update={"database_port": 1, "review_db_password": SecretStr("x")})

    with pytest.raises(DatabaseUnavailable, match="O banco de dados não respondeu"):
        ReviewRepository(unreachable).insert(order_id=1, rating=5, comment=None)
