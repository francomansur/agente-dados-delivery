"""Entry point of the API: create the FastAPI app, assemble its dependencies and include the routes.

This is the only module that knows the concrete classes of every layer. When the api starts, the
lifespan builds the database access, the audit log and the Claude client from the settings; when it
stops, it closes the database connections. The routes receive these objects through ``Depends``.

Run it with ``uvicorn delivery_agent.main:app``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI

from delivery_agent import __version__
from delivery_agent.api.routes import router
from delivery_agent.core.audit import AuditLog
from delivery_agent.core.errors import MissingAPIKey
from delivery_agent.core.settings import Settings
from delivery_agent.llm.client import AnthropicModel, LanguageModel
from delivery_agent.repositories.delivery_repository import Database
from delivery_agent.services.question_service import QuestionService

MISSING_KEY = (
    "A chave da API da Anthropic não está configurada. Defina ANTHROPIC_API_KEY no .env e reinicie a api."
)

DESCRIPTION = """\
Responde, em português, perguntas de negócio sobre a base fictícia de um marketplace de delivery.
Para cada pergunta, o agente escreve um SQL, valida, executa com um usuário somente leitura e
responde mostrando o SQL e as linhas usadas."""


@dataclass
class Services:
    """The objects behind the routes, built once when the api starts. The tests build it with fakes."""

    database: Database
    model: LanguageModel | None  # None when ANTHROPIC_API_KEY is missing
    questions: QuestionService | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> Services:
        """Open the connection pool and create the model client from the settings.

        Args:
            settings: Application settings.

        Returns:
            The services, with no model when ``ANTHROPIC_API_KEY`` is missing.
        """
        database = Database(settings, AuditLog(settings.audit_log_path))
        database.open(wait=False)
        api_key = settings.anthropic_api_key
        model = (
            AnthropicModel(api_key.get_secret_value(), settings.llm_model) if api_key is not None else None
        )

        return cls(database=database, model=model)

    def question_service(self) -> QuestionService:
        """Return the question service, building its graph on the first question.

        Returns:
            The question service.

        Raises:
            MissingAPIKey: ``ANTHROPIC_API_KEY`` is not set.
            DatabaseUnavailable: The schema could not be read to build the graph.
        """
        if self.model is None:
            raise MissingAPIKey(MISSING_KEY)

        if self.questions is None:
            self.questions = QuestionService(self.database, self.model)

        return self.questions

    def database_is_up(self) -> bool:
        """Check that the database answers."""
        return self.database.ping()

    def close(self) -> None:
        """Close the database connections."""
        self.database.close()


def create_app(services: Services | None = None) -> FastAPI:
    """Create the FastAPI app and include the routes.

    Args:
        services: Objects behind the routes. When omitted, the lifespan builds them from the
            settings as the api starts; the tests pass fakes.

    Returns:
        The app, with ``/questions``, ``/health`` and ``/docs``.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved = services or Services.from_settings(Settings())
        app.state.services = resolved
        yield
        resolved.close()

    app = FastAPI(
        title="Agente de dados de delivery", description=DESCRIPTION, version=__version__, lifespan=lifespan
    )
    app.include_router(router)

    return app


app = create_app()
