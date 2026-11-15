"""The agent's flow in LangGraph: understand, write SQL, validate, execute, fix and answer."""

from __future__ import annotations

import operator
from typing import Annotated, Literal, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from delivery_agent.core.audit import QueryContext
from delivery_agent.core.errors import SQLError, SQLRejected
from delivery_agent.llm import prompts
from delivery_agent.llm.client import LanguageModel, ModelCall, TokenUsage
from delivery_agent.repositories.delivery_repository import QueryResult

MAX_FIXES = 2

MODEL_REFUSED = "O modelo se recusou a responder esta pergunta."
OUT_OF_SCOPE = "Não posso responder esta pergunta com os dados desta base."
MODEL_CUT = "A resposta do modelo passou do limite de tokens e foi cortada. Tente uma pergunta mais simples."
MODEL_INVALID = "O modelo devolveu uma resposta fora do formato esperado. Tente reformular a pergunta."
ANSWER_FAILED = "A consulta funcionou, mas não consegui escrever a resposta. Veja o SQL e as linhas."

Status = Literal["answered", "refused", "failed"]


class SQLRunner(Protocol):
    """What the graph needs from the database. ``Database`` implements it; the tests use a fake."""

    def describe_schema(self) -> str:
        """Describe the tables, views and metrics the agent can use."""
        ...

    def validate(self, sql_text: str, context: QueryContext) -> str:
        """Validate model-written SQL and return the SQL to run."""
        ...

    def execute_sql(self, sql_text: str, context: QueryContext) -> QueryResult:
        """Validate and run model-written SQL as the read-only role."""
        ...


class AgentState(TypedDict, total=False):
    """State shared by the nodes. Each node returns only the keys it changes."""

    question: str
    context: QueryContext
    intent: str
    reason: str
    model_stop: str | None  # refusal, max_tokens or invalid_output, when a model call gave no output
    sql: str | None
    error: str | None
    attempts: int  # SQL drafts written, counting the fixes
    fixes: int
    result: QueryResult | None
    answer: str
    status: Status
    usage: Annotated[TokenUsage, operator.add]


def build_graph(
    runner: SQLRunner, model: LanguageModel
) -> CompiledStateGraph[AgentState, None, AgentState, AgentState]:
    """Build and compile the agent's graph.

    Args:
        runner: Database access: schema description, validation and execution.
        model: Language model used by ``understand``, ``generate_sql``, ``fix_sql`` and ``answer``.

    Returns:
        The compiled graph. Invoke it with ``question``, ``context`` and an empty ``usage``.
    """
    schema = runner.describe_schema()
    understand_system = prompts.system_prompt(prompts.UNDERSTAND_TASK, schema)
    write_sql_system = prompts.system_prompt(prompts.WRITE_SQL_TASK, schema)
    fix_sql_system = prompts.system_prompt(prompts.FIX_SQL_TASK, schema)
    answer_system = prompts.system_prompt(prompts.ANSWER_TASK, schema)

    def understand(state: AgentState) -> AgentState:
        call = model.ask(
            system=understand_system,
            prompt=prompts.question_prompt(state["question"]),
            effort="low",
            output_type=prompts.Understanding,
        )

        if call.output is None:
            return {"usage": call.usage, "model_stop": call.stop_reason}

        return {"usage": call.usage, "intent": call.output.intent, "reason": call.output.reason}

    def generate_sql(state: AgentState) -> AgentState:
        call = model.ask(
            system=write_sql_system,
            prompt=prompts.question_prompt(state["question"]),
            effort="medium",
            output_type=prompts.SQLDraft,
        )

        return _sql_draft_update(call, attempts=state.get("attempts", 0) + 1)

    def validate(state: AgentState) -> AgentState:
        try:
            sql = runner.validate(state.get("sql") or "", state["context"])
        except SQLRejected as error:
            return {"error": str(error)}

        return {"sql": sql, "error": None}

    def execute(state: AgentState) -> AgentState:
        try:
            result = runner.execute_sql(state.get("sql") or "", state["context"])
        except SQLError as error:
            return {"error": str(error)}

        return {"result": result, "error": None}

    def fix_sql(state: AgentState) -> AgentState:
        call = model.ask(
            system=fix_sql_system,
            prompt=prompts.fix_prompt(state["question"], state.get("sql") or "", state.get("error") or ""),
            effort="medium",
            output_type=prompts.SQLDraft,
        )
        update = _sql_draft_update(call, attempts=state.get("attempts", 0) + 1)
        update["fixes"] = state.get("fixes", 0) + 1

        return update

    def answer(state: AgentState) -> AgentState:
        result = state.get("result")

        if result is None:  # the routes only reach this node after a successful execution
            return {"answer": ANSWER_FAILED, "status": "failed"}

        call = model.ask(
            system=answer_system,
            prompt=prompts.answer_prompt(state["question"], result),
            effort="low",
            output_type=prompts.FinalAnswer,
        )

        if call.output is not None:
            return {"usage": call.usage, "answer": call.output.answer, "status": "answered"}

        if call.stop_reason == "refusal":
            return {"usage": call.usage, "answer": MODEL_REFUSED, "status": "refused"}

        return {"usage": call.usage, "answer": ANSWER_FAILED, "status": "failed"}

    def refuse(state: AgentState) -> AgentState:
        if state.get("model_stop") == "refusal":
            return {"answer": MODEL_REFUSED, "status": "refused"}

        return {"answer": state.get("reason") or OUT_OF_SCOPE, "status": "refused"}

    def give_up(state: AgentState) -> AgentState:
        stop = state.get("model_stop")

        if stop == "max_tokens":
            return {"answer": MODEL_CUT, "status": "failed"}

        if stop is not None:
            return {"answer": MODEL_INVALID, "status": "failed"}

        attempts = state.get("attempts", 0)
        message = (
            f"Não consegui responder: a consulta falhou {attempts} vezes. Último erro: {state.get('error')}"
        )

        return {"answer": message, "status": "failed"}

    builder = StateGraph(AgentState)
    builder.add_node("understand", understand)
    builder.add_node("generate_sql", generate_sql)
    builder.add_node("validate", validate)
    builder.add_node("execute", execute)
    builder.add_node("fix_sql", fix_sql)
    builder.add_node("answer", answer)
    builder.add_node("refuse", refuse)
    builder.add_node("give_up", give_up)

    builder.add_edge(START, "understand")
    builder.add_conditional_edges("understand", _after_understand, ["generate_sql", "refuse", "give_up"])
    builder.add_conditional_edges("generate_sql", _after_sql_draft, ["validate", "refuse", "give_up"])
    builder.add_conditional_edges("fix_sql", _after_sql_draft, ["validate", "refuse", "give_up"])
    builder.add_conditional_edges("validate", _after_validate, ["execute", "fix_sql", "give_up"])
    builder.add_conditional_edges("execute", _after_execute, ["answer", "fix_sql", "give_up"])
    builder.add_edge("answer", END)
    builder.add_edge("refuse", END)
    builder.add_edge("give_up", END)

    return builder.compile()


def _sql_draft_update(call: ModelCall[prompts.SQLDraft], *, attempts: int) -> AgentState:
    if call.output is None:
        return {"usage": call.usage, "model_stop": call.stop_reason}

    return {"usage": call.usage, "sql": call.output.sql, "attempts": attempts, "error": None}


def _stopped_route(state: AgentState) -> Literal["refuse", "give_up"] | None:
    stop = state.get("model_stop")

    if stop is None:
        return None

    return "refuse" if stop == "refusal" else "give_up"


def _after_understand(state: AgentState) -> Literal["generate_sql", "refuse", "give_up"]:
    stopped = _stopped_route(state)

    if stopped is not None:
        return stopped

    return "generate_sql" if state.get("intent") == "query" else "refuse"


def _after_sql_draft(state: AgentState) -> Literal["validate", "refuse", "give_up"]:
    stopped = _stopped_route(state)

    if stopped is not None:
        return stopped

    return "validate"


def _retry_or_give_up(state: AgentState) -> Literal["fix_sql", "give_up"]:
    return "fix_sql" if state.get("fixes", 0) < MAX_FIXES else "give_up"


def _after_validate(state: AgentState) -> Literal["execute", "fix_sql", "give_up"]:
    if state.get("error") is None:
        return "execute"

    return _retry_or_give_up(state)


def _after_execute(state: AgentState) -> Literal["answer", "fix_sql", "give_up"]:
    if state.get("error") is None:
        return "answer"

    return _retry_or_give_up(state)
