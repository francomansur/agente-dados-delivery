from pathlib import Path

import psycopg
import pytest

from delivery_agent.core.audit import AuditLog
from delivery_agent.core.settings import Settings
from delivery_agent.repositories.delivery_repository import DeliveryRepository
from delivery_agent.seed import TABLE_COLUMNS
from delivery_agent.services.sql_tools import SQLTools

pytestmark = pytest.mark.db

EXPECTED_COLUMNS = {
    **{table: list(columns) for table, columns in TABLE_COLUMNS.items()},
    "customers_public": ["id", "neighborhood", "city", "signup_date", "is_subscriber"],
}
AGENT_RELATIONS = (
    "restaurants",
    "couriers",
    "menu_items",
    "orders",
    "order_items",
    "reviews",
    "customers_public",
)


def test_schema_matches_spec(admin_conn: psycopg.Connection) -> None:
    rows = admin_conn.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' ORDER BY table_name, ordinal_position"
    ).fetchall()
    actual: dict[str, list[str]] = {}

    for table, column in rows:
        actual.setdefault(table, []).append(column)

    assert actual == EXPECTED_COLUMNS


def test_every_table_and_column_has_comment(admin_conn: psycopg.Connection) -> None:
    missing = admin_conn.execute(
        """
        SELECT c.relname, a.attname
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'v')
          AND COALESCE(col_description(c.oid, a.attnum), '') = ''
        UNION ALL
        SELECT c.relname, NULL
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'v')
          AND COALESCE(obj_description(c.oid, 'pg_class'), '') = ''
        """
    ).fetchall()

    assert missing == []


def test_integrity(admin_conn: psycopg.Connection) -> None:
    constraints = {
        name
        for (name,) in admin_conn.execute(
            "SELECT conname FROM pg_constraint WHERE conrelid = 'orders'::regclass AND contype = 'c'"
        )
    }
    different_city = admin_conn.execute(
        "SELECT count(*) FROM orders o JOIN customers c ON c.id = o.customer_id "
        "JOIN restaurants r ON r.id = o.restaurant_id WHERE c.city <> r.city"
    ).fetchone()

    assert {"orders_total_check", "orders_canceled_check", "orders_delivered_check"} <= constraints
    assert different_city == (0,)


def test_shows_only_allowed_objects(tools: SQLTools) -> None:
    description = tools.describe_schema()

    for relation in AGENT_RELATIONS:
        assert f"## {relation} (" in description

    assert "## customers (" not in description

    for column in ("full_name", "email", "phone", "street_address"):
        assert column not in description


def test_is_deterministic(settings: Settings, tmp_path: Path) -> None:
    audit_log = AuditLog(tmp_path / "audit.jsonl")

    with DeliveryRepository(settings) as first, DeliveryRepository(settings) as second:
        assert SQLTools(first, audit_log).describe_schema() == SQLTools(second, audit_log).describe_schema()


def test_includes_examples_and_glossary(tools: SQLTools) -> None:
    description = tools.describe_schema()

    assert "'credit_card'" in description
    assert "'Japonesa'" in description
    assert "De 2026-01-01" in description
    assert "## Glossário de métricas" in description
    assert "28172" not in description
    assert "28.172" not in description
