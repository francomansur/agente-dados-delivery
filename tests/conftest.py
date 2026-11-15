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

from delivery_agent.audit import AuditLog
from delivery_agent.database import Database
from delivery_agent.settings import Settings


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


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings()


@pytest.fixture(scope="session")
def audit_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("audit") / "audit.jsonl"


@pytest.fixture(scope="session")
def database(settings: Settings, audit_path: Path) -> Iterator[Database]:
    _require_database(settings)

    with Database(settings, AuditLog(audit_path)) as db:
        yield db


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
