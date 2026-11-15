"""HTTP routes: ``POST /questions`` and ``GET /health``, documented at ``/docs``.

The routes only deal with HTTP: they read the request, call a service and turn errors into status
codes. They receive the services through FastAPI's ``Depends``; ``delivery_agent.main`` builds them
when the api starts.
"""

from __future__ import annotations

from typing import Annotated, Literal, Protocol

import anthropic
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from delivery_agent.core.errors import DatabaseUnavailable, MissingAPIKey
from delivery_agent.services.question_service import AgentAnswer, QuestionService

router = APIRouter()


class AppServices(Protocol):
    """What the routes use. ``delivery_agent.main.Services`` implements it; the tests use fakes."""

    def question_service(self) -> QuestionService:
        """Return the question service, built on the first question."""
        ...

    def database_is_up(self) -> bool:
        """Check that the database answers."""
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


def get_services(request: Request) -> AppServices:
    """Give the routes the services that ``delivery_agent.main`` built when the api started."""
    services: AppServices = request.app.state.services

    return services


@router.post(
    "/questions",
    summary="Responde uma pergunta de negócio",
    description="Escreve um SQL, valida, executa só leitura e responde com o SQL e as linhas.",
    responses={
        502: {"description": "A API do modelo falhou."},
        503: {"description": "Falta a chave da API, ou o banco de dados está fora do ar."},
    },
)
def ask_question(
    request: QuestionRequest, services: Annotated[AppServices, Depends(get_services)]
) -> AgentAnswer:
    try:
        return services.question_service().ask(request.question, source="api")
    except MissingAPIKey as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except DatabaseUnavailable as error:
        raise HTTPException(status_code=503, detail=f"O banco de dados está fora do ar: {error}") from error
    except anthropic.APIError as error:
        raise HTTPException(status_code=502, detail=f"A API do modelo falhou: {error}") from error


@router.get(
    "/health",
    response_model=Health,
    summary="Diz se a api e o banco estão no ar",
    responses={503: {"model": Health, "description": "O banco de dados não respondeu."}},
)
def health(services: Annotated[AppServices, Depends(get_services)]) -> Health | JSONResponse:
    if services.database_is_up():
        return Health(status="ok")

    body = Health(status="unavailable", detail="O banco de dados não respondeu.")

    return JSONResponse(status_code=503, content=body.model_dump())
