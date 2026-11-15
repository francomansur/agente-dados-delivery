"""Application settings, read from environment variables and the local ``.env`` file."""

from __future__ import annotations

from pathlib import Path

from psycopg.conninfo import make_conninfo
from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings of the agent: database access, limits, audit log and model.

    The API receives only the passwords of ``agent_reader``, the read-only role, and of
    ``review_writer``, which can only insert reviews, and never the administrator's.
    """

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True
    )

    database_host: str = "127.0.0.1"
    # Inside Docker the api uses DATABASE_PORT; on the host, the port the Compose publishes (POSTGRES_PORT).
    database_port: int = Field(default=5433, validation_alias=AliasChoices("DATABASE_PORT", "POSTGRES_PORT"))
    database_name: str = "delivery"
    agent_db_user: str = "agent_reader"
    agent_db_password: SecretStr
    review_db_user: str = "review_writer"
    review_db_password: SecretStr | None = None  # without it, reviews cannot be written
    statement_timeout_ms: int = Field(default=5000, gt=0)
    audit_log_path: Path = Path("logs/audit.jsonl")
    anthropic_api_key: SecretStr | None = None
    llm_model: str = "claude-haiku-5-5"

    @field_validator("anthropic_api_key", "review_db_password", mode="before")
    @classmethod
    def _empty_means_missing(cls, value: object) -> object:
        """Treat an empty variable, such as ``ANTHROPIC_API_KEY=`` in .env.example, as a missing one."""
        return None if value == "" else value

    def agent_conninfo(self) -> str:
        """Build the libpq connection string of the read-only role.

        Returns:
            Connection string for ``psycopg.connect`` or a connection pool.
        """
        return make_conninfo(
            host=self.database_host,
            port=self.database_port,
            dbname=self.database_name,
            user=self.agent_db_user,
            password=self.agent_db_password.get_secret_value(),
            application_name="delivery_agent",
        )

    def review_conninfo(self) -> str:
        """Build the libpq connection string of ``review_writer``, the role that only inserts reviews.

        Returns:
            Connection string for ``psycopg.connect``.

        Raises:
            ValueError: ``REVIEW_DB_PASSWORD`` is not set.
        """
        if self.review_db_password is None:
            raise ValueError("REVIEW_DB_PASSWORD is not set")

        return make_conninfo(
            host=self.database_host,
            port=self.database_port,
            dbname=self.database_name,
            user=self.review_db_user,
            password=self.review_db_password.get_secret_value(),
            application_name="delivery_agent_reviews",
        )
