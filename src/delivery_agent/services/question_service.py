"""Use case shared by the API and the evaluation: answer one business question.

A request to register a review comes back as a pending action. Its state stays in memory, in the
graph's checkpointer, until a person approves or rejects it with ``decide``.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Literal

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field

from delivery_agent.core.audit import QueryContext, Source
from delivery_agent.core.errors import ActionAlreadyDecided, ActionNotFound
from delivery_agent.llm.client import LanguageModel, TokenUsage
from delivery_agent.llm.prompts import ReviewDraft
from delivery_agent.repositories.delivery_repository import Cell, QueryResult
from delivery_agent.services.graph import ActionStatus, AgentState, ReviewDesk, SQLRunner, Status, build_graph
from delivery_agent.services.review_actions import ReviewProposal

# The types that the checkpointer may rebuild from a saved state, and no others.
STATE_TYPES = (QueryContext, QueryResult, TokenUsage, ReviewDraft, ReviewProposal)

ACTION_NOT_FOUND = (
    "Não encontrei a ação {action_id}. As ações pendentes ficam na memória da api e se perdem quando ela "
    "reinicia."
)
DECIDED = {
    "written": "Esta ação já foi aprovada, e a avaliação foi gravada.",
    "rejected": "Esta ação já foi recusada, e nada foi gravado.",
    "failed": "Esta ação já foi aprovada, mas a gravação falhou.",
}

_EXAMPLE: dict[str, Any] = {
    "request_id": "3f2b9c0e8d1a4e6f9b7c5a2d1e0f4b6c",
    "status": "answered",
    "answer": "São Paulo teve o maior faturamento: R$ 746.365,87.",
    "sql": (
        "SELECT r.city, ROUND(SUM(o.total)::numeric, 2) AS revenue\n"
        "FROM orders o JOIN restaurants r ON r.id = o.restaurant_id\n"
        "WHERE o.status = 'delivered'\nGROUP BY r.city\nORDER BY revenue DESC\nLIMIT 1"
    ),
    "columns": ["city", "revenue"],
    "rows": [["São Paulo", 746365.87]],
    "truncated": False,
    "attempts": 1,
    "usage": {
        "model": "claude-haiku-5-5",
        "model_calls": 3,
        "input_tokens": 1200,
        "output_tokens": 900,
        "cache_write_tokens": 0,
        "cache_read_tokens": 9000,
        "cost_usd": 0.0006,
    },
    "duration_ms": 4200.0,
}


class UsageReport(BaseModel):
    """Tokens and cost of one question, summed over every model call."""

    model: str
    model_calls: int
    input_tokens: int
    output_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    cost_usd: float


class PendingAction(BaseModel):
    """A write that waits for a person: what will be written and the id to approve or reject it."""

    action_id: str = Field(description="Use em POST /actions/{action_id}/approve ou /reject.")
    type: Literal["register_review"]
    review: ReviewProposal = Field(description="O que será gravado.")


class ActionResult(BaseModel):
    """What happened after a person approved or rejected a write."""

    action_id: str
    status: ActionStatus = Field(description="written (gravada), rejected (descartada) ou failed (falhou).")
    answer: str = Field(description="O que aconteceu, em português.")
    review: ReviewProposal


class AgentAnswer(BaseModel):
    """Answer to one question: the text, the SQL that ran and its rows."""

    model_config = ConfigDict(json_schema_extra={"examples": [_EXAMPLE]})

    request_id: str
    status: Status = Field(
        description="answered (respondida), refused (recusada), failed (falhou) ou pending_approval "
        "(aguardando aprovação)."
    )
    answer: str = Field(description="Resposta em português.")
    sql: str | None = Field(description="SQL executado; numa falha, o último SQL tentado.")
    columns: list[str]
    rows: list[list[Cell]] = Field(description="No máximo 100 linhas.")
    truncated: bool = Field(description="true quando o resultado tinha mais de 100 linhas.")
    attempts: int = Field(description="Quantas consultas o agente escreveu, contando as correções.")
    usage: UsageReport
    duration_ms: float
    action: PendingAction | None = Field(
        default=None, description="Só numa ação que espera aprovação (pending_approval)."
    )


class QuestionService:
    """Answer business questions: the compiled graph plus the bookkeeping around each question."""

    def __init__(self, runner: SQLRunner, model: LanguageModel, reviews: ReviewDesk | None = None) -> None:
        self._model = model
        self._checkpointer = InMemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=STATE_TYPES))
        self._graph = build_graph(runner, model, reviews, self._checkpointer)
        self._decisions = threading.Lock()

    def ask(self, question: str, *, source: Source, request_id: str | None = None) -> AgentAnswer:
        """Answer one question in Portuguese.

        Args:
            question: The question, in Portuguese.
            source: Who is asking (``api``, ``eval`` or ``mcp``), for the audit log.
            request_id: Id for the audit log; a new one is created when omitted.

        Returns:
            Status, answer, SQL, rows, tokens, cost and duration.

        Raises:
            DatabaseUnavailable: The database cannot be reached.
            anthropic.APIError: The model API failed after the SDK retries.
        """
        started = time.monotonic()
        context = QueryContext(request_id=request_id or uuid.uuid4().hex, source=source, question=question)
        action_id = uuid.uuid4().hex
        initial: AgentState = {
            "question": question,
            "context": context,
            "action_id": action_id,
            "usage": TokenUsage(),
            "attempts": 0,
            "fixes": 0,
        }
        pending = False

        try:
            state = self._graph.invoke(initial, _thread(action_id))
            pending = state["status"] == "pending_approval"
        finally:
            if not pending:  # only a pending action keeps its state, for the approval
                self._checkpointer.delete_thread(action_id)

        result = state.get("result")
        status = state["status"]
        usage = state["usage"]

        sql = state.get("sql") if status == "failed" else None

        if result is not None:
            sql = result.sql

        answer = AgentAnswer(
            request_id=context.request_id,
            status=status,
            answer=state["answer"],
            sql=sql,
            columns=result.columns if result is not None else [],
            rows=result.rows if result is not None else [],
            truncated=result.truncated if result is not None else False,
            attempts=state.get("attempts", 0),
            usage=UsageReport(
                model=self._model.name,
                model_calls=usage.calls,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cost_usd=round(usage.cost_usd(self._model.name), 6),
            ),
            duration_ms=round((time.monotonic() - started) * 1000, 1),
        )

        # Set only here, and left unset otherwise: the API omits the field when nothing waits for approval.
        if pending:
            proposal = state["proposal"]
            answer.action = PendingAction(action_id=action_id, type="register_review", review=proposal)

        return answer

    def decide(self, action_id: str, *, approve: bool) -> ActionResult:
        """Approve or reject a pending action, writing the review only when it is approved.

        Args:
            action_id: Id that came in the pending answer.
            approve: Whether the person approved the action.

        Returns:
            What happened: the review was written, discarded, or the write failed.

        Raises:
            ActionNotFound: No action has this id, or the api restarted since it was proposed.
            ActionAlreadyDecided: The action was approved or rejected before.
        """
        config = _thread(action_id)

        # One decision at a time, so that two approvals of the same action cannot both write.
        with self._decisions:
            snapshot = self._graph.get_state(config)

            if not snapshot.values:
                raise ActionNotFound(ACTION_NOT_FOUND.format(action_id=action_id))

            if not snapshot.interrupts:
                raise ActionAlreadyDecided(DECIDED[snapshot.values["action_status"]])

            state = self._graph.invoke(Command(resume=approve), config)

        return ActionResult(
            action_id=action_id,
            status=state["action_status"],
            answer=state["answer"],
            review=state["proposal"],
        )


def _thread(action_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": action_id}}
