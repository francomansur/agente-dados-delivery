"""HTTP routes: questions, the decision on pending actions and ``/health``, and the web page at ``/``.

The routes only deal with HTTP: they read the request, call a service and turn errors into status
codes. They receive the services through FastAPI's ``Depends``; ``delivery_agent.main`` builds them
when the api starts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal, Protocol

import anthropic
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from delivery_agent.core.errors import (
    ActionAlreadyDecided,
    ActionNotFound,
    DatabaseUnavailable,
    MissingAPIKey,
)
from delivery_agent.services.graph import Status
from delivery_agent.services.question_service import ActionResult, AgentAnswer, PendingAction, QuestionService

# Fields of every answer, plus the pending action when there is one. The technical details (sql,
# attempts, usage and duration_ms) only come with include_details=true.
SUMMARY_FIELDS = {"request_id", "status", "answer", "columns", "rows", "truncated", "action"}

# The example with every field is the one that AgentAnswer documents, and the default one is its
# summary, both with the fields in the order of the answer (the JSON schema sorts them).
_EXAMPLE: dict[str, Any] = AgentAnswer.model_json_schema()["examples"][0]
DETAILED_EXAMPLE = {name: _EXAMPLE[name] for name in AgentAnswer.model_fields if name in _EXAMPLE}
SUMMARY_EXAMPLE = {name: value for name, value in DETAILED_EXAMPLE.items() if name in SUMMARY_FIELDS}
PENDING_EXAMPLE: dict[str, Any] = {
    "request_id": "8d1e0a4f5b6c4d2e9f3a7b1c0d2e4f6a",
    "status": "pending_approval",
    "answer": (
        "Pronto para registrar a avaliação do pedido 1001, com nota 4 e um comentário. "
        "Ela só será gravada depois da sua aprovação."
    ),
    "columns": [],
    "rows": [],
    "truncated": False,
    "action": {
        "action_id": "5c2a9e1d7b3f4a6c8e0d2b4f6a8c0e1d",
        "type": "register_review",
        "review": {"order_id": 1001, "rating": 4, "comment": "Chegou frio."},
    },
}

# The web page to try the agent: index.html here, and its CSS and JavaScript under /static.
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

router = APIRouter()


class AppServices(Protocol):
    """What the routes use. ``delivery_agent.main.Services`` implements it; the tests use fakes."""

    def question_service(self) -> QuestionService:
        """Return the question service, built on the first question."""
        ...

    def decide_action(self, action_id: str, *, approve: bool) -> ActionResult:
        """Approve or reject a pending action."""
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


class AnswerSummary(BaseModel):
    """Default body of ``POST /questions``: the answer and its rows, without the technical details.

    The fields are the same as in ``AgentAnswer``; a test keeps them in step.
    """

    model_config = ConfigDict(json_schema_extra={"examples": [SUMMARY_EXAMPLE]})

    request_id: str
    status: Status = Field(
        description="answered (respondida), refused (recusada), failed (falhou) ou pending_approval "
        "(aguardando aprovação)."
    )
    answer: str = Field(description="Resposta em português.")
    columns: list[str]
    rows: list[list[str | int | float | bool | None]] = Field(description="No máximo 100 linhas.")
    truncated: bool = Field(description="true quando o resultado tinha mais de 100 linhas.")
    action: PendingAction | None = Field(
        default=None, description="Só numa ação que espera aprovação (pending_approval)."
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
    description=(
        "Escreve um SQL, valida, executa só leitura e responde com as linhas usadas. "
        "Com include_details=true, a resposta traz também o SQL, as tentativas, o tempo e o custo."
    ),
    responses={
        200: {
            "description": "A resposta. Os detalhes técnicos só vêm com include_details=true.",
            "content": {
                "application/json": {
                    "examples": {
                        "padrao": {"summary": "Padrão: só o essencial", "value": SUMMARY_EXAMPLE},
                        "com_detalhes": {"summary": "Com include_details=true", "value": DETAILED_EXAMPLE},
                        "acao_pendente": {
                            "summary": "Pedido de avaliação: espera aprovação",
                            "value": PENDING_EXAMPLE,
                        },
                    }
                }
            },
        },
        502: {"description": "A API do modelo falhou."},
        503: {"description": "Falta a chave da API, ou o banco de dados está fora do ar."},
    },
    # The pending action is left out of the answers that have none, instead of coming as null.
    response_model_exclude_unset=True,
)
def ask_question(
    request: QuestionRequest,
    services: Annotated[AppServices, Depends(get_services)],
    include_details: Annotated[
        bool,
        Query(description="true para receber também o SQL, as tentativas, o tempo e o custo (usage)."),
    ] = False,
) -> AgentAnswer | AnswerSummary:
    try:
        answer = services.question_service().ask(request.question, source="api")
    except MissingAPIKey as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except DatabaseUnavailable as error:
        raise HTTPException(status_code=503, detail=f"O banco de dados está fora do ar: {error}") from error
    except anthropic.APIError as error:
        raise HTTPException(status_code=502, detail=f"A API do modelo falhou: {error}") from error

    if include_details:
        return answer

    return AnswerSummary.model_validate(answer.model_dump(include=SUMMARY_FIELDS, exclude_unset=True))


ACTION_ERRORS: dict[int | str, dict[str, Any]] = {
    404: {"description": "Não existe ação com esse id, ou a api reiniciou e perdeu as pendentes."},
    409: {"description": "A ação já foi aprovada ou recusada."},
}


@router.post(
    "/actions/{action_id}/approve",
    summary="Aprova uma ação pendente e grava a avaliação",
    description=(
        "Grava a avaliação que veio em action, com o papel review_writer. Uma ação só é decidida uma vez."
    ),
    responses=ACTION_ERRORS,
)
def approve_action(action_id: str, services: Annotated[AppServices, Depends(get_services)]) -> ActionResult:
    return _decide(services, action_id, approve=True)


@router.post(
    "/actions/{action_id}/reject",
    summary="Recusa uma ação pendente: nada é gravado",
    description="Descarta a avaliação que veio em action. Uma ação só é decidida uma vez.",
    responses=ACTION_ERRORS,
)
def reject_action(action_id: str, services: Annotated[AppServices, Depends(get_services)]) -> ActionResult:
    return _decide(services, action_id, approve=False)


def _decide(services: AppServices, action_id: str, *, approve: bool) -> ActionResult:
    try:
        return services.decide_action(action_id, approve=approve)
    except ActionNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ActionAlreadyDecided as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/", include_in_schema=False)
def web_page() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


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
