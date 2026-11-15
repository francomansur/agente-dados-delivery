"""HTTP API: ``POST /questions`` and ``GET /health``, documented at ``/docs``.

Run it with ``uvicorn delivery_agent.api:create_app --factory``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal, Protocol

import anthropic
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from delivery_agent import __version__
from delivery_agent.agent import Agent, AgentAnswer
from delivery_agent.audit import AuditLog
from delivery_agent.database import Database
from delivery_agent.errors import DatabaseUnavailable
from delivery_agent.graph import SQLRunner
from delivery_agent.llm import AnthropicModel, LanguageModel
from delivery_agent.settings import Settings

MISSING_KEY = (
    "A chave da API da Anthropic não está configurada. Defina ANTHROPIC_API_KEY no .env e reinicie a api."
)

DESCRIPTION = """\
Responde, em português, perguntas de negócio sobre a base fictícia de um marketplace de delivery.
Para cada pergunta, o agente escreve um SQL, valida, executa com um usuário somente leitura e
responde mostrando o SQL e as linhas usadas."""


class Backend(SQLRunner, Protocol):
    """Database access the API needs: the graph's runner plus a health check."""

    def ping(self) -> bool:
        """Check that the database answers."""
        ...

    def close(self) -> None:
        """Release the connections."""
        ...


class QuestionRequest(BaseModel):
    """Body of ``POST /questions``."""

    question: str = Field(
        min_length=3,
        max_length=500,
        description="Pergunta em português sobre a base de delivery.",
        examples=["Qual cidade teve o maior faturamento, e de quanto foi?"],
    )


class Health(BaseModel):
    """Body of ``GET /health``."""

    status: Literal["ok", "unavailable"]
    detail: str | None = None


@dataclass
class Services:
    """What the API runs on. The tests build it with fakes."""

    database: Backend
    model: LanguageModel | None
    agent: Agent | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> Services:
        """Connect to the database and create the model client from the settings.

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

    def get_agent(self) -> Agent:
        """Return the agent, building its graph on first use.

        Returns:
            The agent.

        Raises:
            HTTPException: 503 when the API key is missing.
            DatabaseUnavailable: The schema could not be read to build the graph.
        """
        if self.model is None:
            raise HTTPException(status_code=503, detail=MISSING_KEY)

        if self.agent is None:
            self.agent = Agent(self.database, self.model)

        return self.agent


def create_app(services: Services | None = None) -> FastAPI:
    """Build the FastAPI app.

    Args:
        services: Database and model to use; when omitted, they come from the settings.

    Returns:
        The app, with ``/questions``, ``/health`` and ``/docs``.
    """
    resolved = services or Services.from_settings(Settings())

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        resolved.database.close()

    app = FastAPI(
        title="Agente de dados de delivery", description=DESCRIPTION, version=__version__, lifespan=lifespan
    )

    @app.post(
        "/questions",
        summary="Responde uma pergunta de negócio",
        description="Escreve um SQL, valida, executa só leitura e responde com o SQL e as linhas.",
        responses={
            502: {"description": "A API do modelo falhou."},
            503: {"description": "Falta a chave da API, ou o banco de dados está fora do ar."},
        },
    )
    def ask_question(request: QuestionRequest) -> AgentAnswer:
        try:
            return resolved.get_agent().ask(request.question, source="api")
        except DatabaseUnavailable as error:
            raise HTTPException(
                status_code=503, detail=f"O banco de dados está fora do ar: {error}"
            ) from error
        except anthropic.APIError as error:
            raise HTTPException(status_code=502, detail=f"A API do modelo falhou: {error}") from error

    @app.get(
        "/health",
        response_model=Health,
        summary="Diz se a api e o banco estão no ar",
        responses={503: {"model": Health, "description": "O banco de dados não respondeu."}},
    )
    def health() -> Health | JSONResponse:
        if resolved.database.ping():
            return Health(status="ok")

        body = Health(status="unavailable", detail="O banco de dados não respondeu.")

        return JSONResponse(status_code=503, content=body.model_dump())

    return app
