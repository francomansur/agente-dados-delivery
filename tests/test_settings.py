import pytest
from pydantic import ValidationError

from delivery_agent.core.settings import Settings


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    names = ("DATABASE_PORT", "POSTGRES_PORT", "LLM_MODEL", "AGENT_DB_PASSWORD", "REVIEW_DB_PASSWORD")

    for name in (*names, "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def test_defaults() -> None:
    settings = Settings(_env_file=None, agent_db_password="x")

    assert settings.llm_model == "claude-haiku-5-5"
    assert settings.database_port == 5433
    assert settings.agent_db_user == "agent_reader"
    assert settings.statement_timeout_ms == 5000
    assert settings.anthropic_api_key is None
    assert settings.review_db_user == "review_writer"
    assert settings.review_db_password is None


def test_database_port_prefers_the_docker_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_PORT", "6000")
    monkeypatch.setenv("DATABASE_PORT", "5432")

    assert Settings(_env_file=None, agent_db_password="x").database_port == 5432

    monkeypatch.delenv("DATABASE_PORT")

    assert Settings(_env_file=None, agent_db_password="x").database_port == 6000


def test_model_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MODEL", "claude-sonnet-5-5")

    assert Settings(_env_file=None, agent_db_password="x").llm_model == "claude-sonnet-5-5"


def test_password_is_kept_secret() -> None:
    settings = Settings(_env_file=None, agent_db_password="segredo")

    assert "segredo" not in repr(settings)
    assert "password=segredo" in settings.agent_conninfo()


def test_password_is_required() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_empty_api_key_means_missing() -> None:
    assert Settings(_env_file=None, agent_db_password="x", anthropic_api_key="").anthropic_api_key is None


def test_review_writer_connects_with_its_own_password() -> None:
    settings = Settings(_env_file=None, agent_db_password="x", review_db_password="escrita")

    assert "escrita" not in repr(settings)
    assert "user=review_writer" in settings.review_conninfo()
    assert "password=escrita" in settings.review_conninfo()


def test_without_its_password_the_review_writer_has_no_connection() -> None:
    settings = Settings(_env_file=None, agent_db_password="x", review_db_password="")

    assert settings.review_db_password is None

    with pytest.raises(ValueError, match="REVIEW_DB_PASSWORD"):
        settings.review_conninfo()
