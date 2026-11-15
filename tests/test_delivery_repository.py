"""The repository on its own, against Postgres: no validator and no audit log in front of it."""

import pytest

from delivery_agent.core.errors import ExecutionError, QueryTimeout
from delivery_agent.core.settings import Settings
from delivery_agent.repositories.delivery_repository import DeliveryRepository

pytestmark = pytest.mark.db

CITIES = "SELECT city, COUNT(*) AS restaurants FROM restaurants GROUP BY city ORDER BY city"
AGENT_RELATIONS = [
    "couriers",
    "customers_public",
    "menu_items",
    "order_items",
    "orders",
    "restaurants",
    "reviews",
]


def test_select_returns_columns_rows_and_duration(repository: DeliveryRepository) -> None:
    result = repository.select(CITIES, max_rows=100)

    assert result.sql == CITIES
    assert result.columns == ["city", "restaurants"]
    assert result.rows[0] == ["Belo Horizonte", 18]
    assert len(result.rows) == 7
    assert not result.truncated
    assert result.duration_ms > 0


def test_select_cuts_the_result_at_max_rows(repository: DeliveryRepository) -> None:
    result = repository.select("SELECT id FROM orders ORDER BY id", max_rows=100)

    assert len(result.rows) == 100
    assert result.truncated


def test_converts_numbers_and_dates(repository: DeliveryRepository) -> None:
    result = repository.select(
        "SELECT total, created_at, created_at::date AS day, status = 'delivered' AS delivered "
        "FROM orders WHERE id = 1",
        max_rows=100,
    )
    total, created_at, day, delivered = result.rows[0]

    assert isinstance(total, float)
    assert created_at == "2026-01-01 11:16:59"
    assert day == "2026-01-01"
    assert isinstance(delivered, bool)


def test_postgres_errors_become_execution_errors(repository: DeliveryRepository) -> None:
    with pytest.raises(ExecutionError, match='Erro do Postgres: column "nope" does not exist'):
        repository.select("SELECT nope FROM orders", max_rows=100)


def test_writes_fail_even_without_the_validator(repository: DeliveryRepository) -> None:
    with pytest.raises(ExecutionError, match="read-only transaction"):
        repository.select("DELETE FROM orders WHERE id = 1", max_rows=100)


def test_one_call_runs_a_single_statement(repository: DeliveryRepository) -> None:
    with pytest.raises(ExecutionError, match="multiple commands"):
        repository.select("SELECT 1; SELECT 2", max_rows=100)


def test_slow_query_hits_the_timeout(settings: Settings) -> None:
    fast = settings.model_copy(update={"statement_timeout_ms": 200})

    with DeliveryRepository(fast) as repository, pytest.raises(QueryTimeout, match="0,2 s"):
        repository.select("SELECT count(*) FROM orders a, orders b, orders c", max_rows=100)


def test_ping(repository: DeliveryRepository) -> None:
    assert repository.ping()


def test_lists_only_what_the_role_can_read(repository: DeliveryRepository) -> None:
    relations = {relation.name: relation for relation in repository.list_relations()}

    assert list(relations) == AGENT_RELATIONS
    assert relations["customers_public"].is_view
    assert [column.name for column in relations["customers_public"].columns] == [
        "id",
        "neighborhood",
        "city",
        "signup_date",
        "is_subscriber",
    ]
    assert all(column.comment for relation in relations.values() for column in relation.columns)


def test_reads_sample_values_and_date_ranges(repository: DeliveryRepository) -> None:
    payment_methods = repository.distinct_values("orders", "payment_method", limit=16)
    first, last = repository.value_range("orders", "created_at")

    assert payment_methods == ["cash", "credit_card", "debit_card", "meal_voucher", "pix"]
    assert str(first).startswith("2026-01-01")
    assert str(last).startswith("2026-10-31")
