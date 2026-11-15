"""Test doubles for the language model and the database."""

from dataclasses import dataclass

from pydantic import BaseModel

from delivery_agent.core.audit import QueryContext
from delivery_agent.core.errors import DatabaseUnavailable
from delivery_agent.llm.client import Effort, ModelCall, TokenUsage
from delivery_agent.llm.prompts import FinalAnswer, SQLDraft, Understanding
from delivery_agent.repositories.delivery_repository import QueryResult
from delivery_agent.services.sql_validator import validate_sql

USAGE = TokenUsage(input_tokens=100, output_tokens=50, cache_read_tokens=1000, calls=1)
QUERY = Understanding(intent="query", reason="Dá para responder com os pedidos.")
COUNT_SQL = SQLDraft(sql="SELECT count(*) AS n FROM orders")
DONE = FinalAnswer(answer="Há 1 pedido.")


@dataclass
class Request:
    system: str
    prompt: str
    effort: Effort
    output_type: type[BaseModel]


class FakeModel:
    """Replies with a script and records every request.

    A ``BaseModel`` in the script is a successful call; a string is a stop reason without output,
    such as ``"refusal"`` or ``"max_tokens"``; an exception is raised.
    """

    name = "claude-haiku-5-5"

    def __init__(self, *script: BaseModel | str | Exception) -> None:
        self.script = list(script)
        self.requests: list[Request] = []

    def ask(self, *, system, prompt, effort, output_type):  # type: ignore[no-untyped-def]
        self.requests.append(Request(system, prompt, effort, output_type))
        reply = self.script.pop(0)

        if isinstance(reply, Exception):
            raise reply

        if isinstance(reply, str):
            return ModelCall(output=None, stop_reason=reply, usage=USAGE)

        assert isinstance(reply, output_type)

        return ModelCall(output=reply, stop_reason="end_turn", usage=USAGE)


class FakeDatabase:
    """Runs the real validator but answers from a script instead of Postgres.

    ``outcomes`` maps a fragment of the SQL to the result or exception it produces; any other
    query returns one row with the number 1.
    """

    def __init__(
        self, outcomes: dict[str, QueryResult | Exception] | None = None, *, healthy: bool = True
    ) -> None:
        self.outcomes = outcomes or {}
        self.healthy = healthy
        self.executed: list[str] = []

    def describe_schema(self) -> str:
        if not self.healthy:
            raise DatabaseUnavailable("o banco não respondeu")

        return "## orders (tabela)\nPedidos.\n- id (integer): Identificador do pedido.\n"

    def validate(self, sql_text: str, context: QueryContext) -> str:
        return validate_sql(sql_text)

    def execute_sql(self, sql_text: str, context: QueryContext) -> QueryResult:
        sql = validate_sql(sql_text)
        self.executed.append(sql)

        for fragment, outcome in self.outcomes.items():
            if fragment in sql:
                if isinstance(outcome, Exception):
                    raise outcome

                return outcome.model_copy(update={"sql": sql})

        return QueryResult(sql=sql, columns=["n"], rows=[[1]], truncated=False, duration_ms=1.0)

    def ping(self) -> bool:
        return self.healthy

    def close(self) -> None:
        pass
