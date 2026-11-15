"""Entry point shared by the API and the evaluation: one question in, one answer out."""

from __future__ import annotations

import time
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from delivery_agent.core.audit import QueryContext, Source
from delivery_agent.llm.client import LanguageModel, TokenUsage
from delivery_agent.repositories.delivery_repository import Cell
from delivery_agent.services.graph import AgentState, SQLRunner, Status, build_graph

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


class AgentAnswer(BaseModel):
    """Answer to one question: the text, the SQL that ran and its rows."""

    model_config = ConfigDict(json_schema_extra={"examples": [_EXAMPLE]})

    request_id: str
    status: Status = Field(description="answered (respondida), refused (recusada) ou failed (falhou).")
    answer: str = Field(description="Resposta em português.")
    sql: str | None = Field(description="SQL executado; numa falha, o último SQL tentado.")
    columns: list[str]
    rows: list[list[Cell]] = Field(description="No máximo 100 linhas.")
    truncated: bool = Field(description="true quando o resultado tinha mais de 100 linhas.")
    attempts: int = Field(description="Quantas consultas o agente escreveu, contando as correções.")
    usage: UsageReport
    duration_ms: float


class Agent:
    """The compiled graph plus the bookkeeping around one question."""

    def __init__(self, runner: SQLRunner, model: LanguageModel) -> None:
        self._model = model
        self._graph = build_graph(runner, model)

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
        initial: AgentState = {
            "question": question,
            "context": context,
            "usage": TokenUsage(),
            "attempts": 0,
            "fixes": 0,
        }
        state = self._graph.invoke(initial)
        result = state.get("result")
        status = state["status"]
        usage = state["usage"]

        sql = state.get("sql") if status == "failed" else None

        if result is not None:
            sql = result.sql

        return AgentAnswer(
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
