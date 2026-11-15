"""Shared fixtures. Tests marked with ``db`` need the Postgres of the Compose (make db-up).

Without the database those tests are skipped, except when REQUIRE_DB is set, as in the CI,
where they fail instead.
"""

import os
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from delivery_agent.core.audit import AuditLog
from delivery_agent.core.settings import Settings
from delivery_agent.repositories.delivery_repository import DeliveryRepository
from delivery_agent.services.sql_tools import SQLTools


class AdminSettings(BaseSettings):
    """Administrator credentials, used only by tests that inspect what the agent cannot see."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    postgres_user: str = "admin"
    postgres_password: SecretStr


def _require_database(settings: Settings) -> None:
    try:
        with psycopg.connect(settings.agent_conninfo(), connect_timeout=3):
            return
    except psycopg.Error as error:
        message = f"Postgres fora do ar ({str(error).strip()}). Rode make db-up."

    if os.environ.get("REQUIRE_DB"):
        pytest.fail(message)

    pytest.skip(message)


def _require_review_writer(settings: Settings) -> None:
    _require_database(settings)

    if settings.review_db_password is not None:
        return

    message = "Falta REVIEW_DB_PASSWORD no .env, a senha do review_writer."

    if os.environ.get("REQUIRE_DB"):
        pytest.fail(message)

    pytest.skip(message)


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings()


@pytest.fixture(scope="session")
def audit_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("audit") / "audit.jsonl"


@pytest.fixture(scope="session")
def repository(settings: Settings) -> Iterator[DeliveryRepository]:
    _require_database(settings)

    with DeliveryRepository(settings) as repository:
        yield repository


@pytest.fixture(scope="session")
def tools(repository: DeliveryRepository, audit_path: Path) -> SQLTools:
    return SQLTools(repository, AuditLog(audit_path))


@pytest.fixture
def agent_conn(settings: Settings) -> Iterator[psycopg.Connection]:
    _require_database(settings)

    with psycopg.connect(settings.agent_conninfo()) as conn:
        yield conn


@pytest.fixture
def admin_conn(settings: Settings) -> Iterator[psycopg.Connection]:
    _require_database(settings)
    admin = AdminSettings()
    conninfo = make_conninfo(
        host=settings.database_host,
        port=settings.database_port,
        dbname=settings.database_name,
        user=admin.postgres_user,
        password=admin.postgres_password.get_secret_value(),
    )

    with psycopg.connect(conninfo) as conn:
        yield conn


@pytest.fixture
def writer_conn(settings: Settings) -> Iterator[psycopg.Connection]:
    _require_review_writer(settings)

    with psycopg.connect(settings.review_conninfo(), autocommit=True) as conn:
        yield conn


@pytest.fixture
def unreviewed_order(admin_conn: psycopg.Connection) -> Iterator[int]:
    """A delivered order without a review. A review that a test writes for it is deleted at the end."""
    row = admin_conn.execute(
        "SELECT o.id FROM orders o WHERE o.status = 'delivered' "
        "AND NOT EXISTS (SELECT 1 FROM reviews r WHERE r.order_id = o.id) ORDER BY o.id LIMIT 1"
    ).fetchone()
    assert row is not None
    admin_conn.commit()

    yield row[0]

    admin_conn.execute("DELETE FROM reviews WHERE order_id = %s", (row[0],))
    admin_conn.commit()
