"""The agent's flow in LangGraph: understand, write SQL, validate, execute, fix and answer.

The one write, registering a review, takes its own path: the model extracts the review, the rules
check it, and the graph pauses with ``interrupt`` until a person approves or rejects it. Only then
does the review go to the database.
"""

from __future__ import annotations

import operator
from typing import Annotated, Literal, Protocol, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from delivery_agent.core.audit import ActionContext, QueryContext
from delivery_agent.core.errors import (
    DatabaseUnavailable,
    DuplicateReview,
    ExecutionError,
    ReviewBlocked,
    SQLError,
    SQLRejected,
)
from delivery_agent.llm import prompts
from delivery_agent.llm.client import LanguageModel, ModelCall, TokenUsage
from delivery_agent.repositories.delivery_repository import QueryResult
from delivery_agent.services.review_actions import ReviewProposal

MAX_FIXES = 2

MODEL_REFUSED = "O modelo se recusou a responder esta pergunta."
OUT_OF_SCOPE = "Não posso responder esta pergunta com os dados desta base."
MODEL_CUT = "A resposta do modelo passou do limite de tokens e foi cortada. Tente uma pergunta mais simples."
MODEL_INVALID = "O modelo devolveu uma resposta fora do formato esperado. Tente reformular a pergunta."
ANSWER_FAILED = "A consulta funcionou, mas não consegui escrever a resposta. Veja o SQL e as linhas."
REVIEWS_OFF = "Esta instalação não grava avaliações: falta a senha do review_writer (REVIEW_DB_PASSWORD)."
REVIEW_DISCARDED = "Avaliação descartada: nada foi gravado."

Status = Literal["answered", "refused", "failed", "pending_approval"]
ActionStatus = Literal["written", "rejected", "failed"]


class SQLRunner(Protocol):
    """What the graph needs to read the database. ``SQLTools`` implements it; the tests use a fake."""

    def describe_schema(self) -> str:
        """Describe the tables, views and metrics the agent can use."""
        ...

    def validate(self, sql_text: str, context: QueryContext) -> str:
        """Validate model-written SQL and return the SQL to run."""
        ...

    def execute_sql(self, sql_text: str, context: QueryContext) -> QueryResult:
        """Validate and run model-written SQL as the read-only role."""
        ...


class ReviewDesk(Protocol):
    """What the graph needs for the review action. ``ReviewActions`` implements it; the tests use a fake."""

    def propose(self, draft: prompts.ReviewDraft, context: ActionContext) -> ReviewProposal:
        """Apply the rules to the review that the model extracted; raise ``ReviewBlocked`` if one fails."""
        ...

    def decide(self, proposal: ReviewProposal, context: ActionContext, *, approved: bool) -> None:
        """Log the person's decision."""
        ...

    def write(self, proposal: ReviewProposal, context: ActionContext) -> None:
        """Write an approved review."""
        ...


class _ReviewsOff:
    """Stands in for the review actions when the api has no password to write: every request is blocked."""

    def propose(self, draft: prompts.ReviewDraft, context: ActionContext) -> ReviewProposal:
        raise ReviewBlocked(REVIEWS_OFF)

    def decide(self, proposal: ReviewProposal, context: ActionContext, *, approved: bool) -> None:
        raise ReviewBlocked(REVIEWS_OFF)

    def write(self, proposal: ReviewProposal, context: ActionContext) -> None:
        raise ReviewBlocked(REVIEWS_OFF)


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
    action_id: str  # also the id of the graph's thread, which the approval resumes
    review_draft: prompts.ReviewDraft
    proposal: ReviewProposal
    approved: bool
    action_status: ActionStatus


def build_graph(
    runner: SQLRunner,
    model: LanguageModel,
    reviews: ReviewDesk | None = None,
    checkpointer: BaseCheckpointSaver[str] | None = None,
) -> CompiledStateGraph[AgentState, None, AgentState, AgentState]:
    """Build and compile the agent's graph.

    Args:
        runner: Database access: schema description, validation and execution.
        model: Language model used by the nodes that write a classification, SQL, a review or an answer.
        reviews: Rules, log and write of the review action. Without it, every review request is refused.
        checkpointer: Where the graph keeps the state of a pending action while it waits for approval.

    Returns:
        The compiled graph. Invoke it with ``question``, ``context``, ``action_id`` and an empty
        ``usage``, and with the thread id in the config.
    """
    schema = runner.describe_schema()
    understand_system = prompts.system_prompt(prompts.UNDERSTAND_TASK, schema)
    write_sql_system = prompts.system_prompt(prompts.WRITE_SQL_TASK, schema)
    fix_sql_system = prompts.system_prompt(prompts.FIX_SQL_TASK, schema)
    draft_review_system = prompts.system_prompt(prompts.DRAFT_REVIEW_TASK, schema)
    answer_system = prompts.system_prompt(prompts.ANSWER_TASK, schema)
    desk: ReviewDesk = reviews if reviews is not None else _ReviewsOff()

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

        prompt = prompts.answer_prompt(
            state["question"],
            sql=result.sql,
            columns=result.columns,
            rows=result.rows,
            truncated=result.truncated,
        )
        call = model.ask(system=answer_system, prompt=prompt, effort="low", output_type=prompts.FinalAnswer)

        if call.output is not None:
            return {"usage": call.usage, "answer": call.output.answer, "status": "answered"}

        if call.stop_reason == "refusal":
            return {"usage": call.usage, "answer": MODEL_REFUSED, "status": "refused"}

        return {"usage": call.usage, "answer": ANSWER_FAILED, "status": "failed"}

    def draft_review(state: AgentState) -> AgentState:
        call = model.ask(
            system=draft_review_system,
            prompt=prompts.question_prompt(state["question"]),
            effort="low",
            output_type=prompts.ReviewDraft,
        )

        if call.output is None:
            return {"usage": call.usage, "model_stop": call.stop_reason}

        return {"usage": call.usage, "review_draft": call.output}

    def check_review(state: AgentState) -> AgentState:
        try:
            proposal = desk.propose(state["review_draft"], _action_context(state))
        except ReviewBlocked as error:
            return {"answer": str(error), "status": "refused"}

        return {"proposal": proposal, "answer": _pending_message(proposal), "status": "pending_approval"}

    def wait_for_approval(state: AgentState) -> AgentState:
        # The graph stops here until POST /actions/{id}/approve or /reject resumes it with True or False.
        # On resume the node runs again from the top, and interrupt() returns that decision.
        approved = bool(interrupt({"action": "register_review", "review": state["proposal"].model_dump()}))
        desk.decide(state["proposal"], _action_context(state), approved=approved)

        return {"approved": approved}

    def write_review(state: AgentState) -> AgentState:
        proposal = state["proposal"]

        try:
            desk.write(proposal, _action_context(state))
        except DuplicateReview as error:
            return {"action_status": "failed", "answer": f"A avaliação não foi gravada: {error}"}
        except (ExecutionError, DatabaseUnavailable) as error:
            return {"action_status": "failed", "answer": f"Não consegui gravar a avaliação: {error}"}

        message = f"Avaliação registrada: pedido {proposal.order_id}, nota {proposal.rating}."

        return {"action_status": "written", "answer": message}

    def discard_review(state: AgentState) -> AgentState:
        return {"action_status": "rejected", "answer": REVIEW_DISCARDED}

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
    builder.add_node("draft_review", draft_review)
    builder.add_node("check_review", check_review)
    builder.add_node("wait_for_approval", wait_for_approval)
    builder.add_node("write_review", write_review)
    builder.add_node("discard_review", discard_review)
    builder.add_node("refuse", refuse)
    builder.add_node("give_up", give_up)

    builder.add_edge(START, "understand")
    builder.add_conditional_edges(
        "understand", _after_understand, ["generate_sql", "draft_review", "refuse", "give_up"]
    )
    builder.add_conditional_edges("generate_sql", _after_sql_draft, ["validate", "refuse", "give_up"])
    builder.add_conditional_edges("fix_sql", _after_sql_draft, ["validate", "refuse", "give_up"])
    builder.add_conditional_edges("validate", _after_validate, ["execute", "fix_sql", "give_up"])
    builder.add_conditional_edges("execute", _after_execute, ["answer", "fix_sql", "give_up"])
    builder.add_conditional_edges("draft_review", _after_review_draft, ["check_review", "refuse", "give_up"])
    builder.add_conditional_edges("check_review", _after_check_review, ["wait_for_approval", END])
    builder.add_conditional_edges("wait_for_approval", _after_approval, ["write_review", "discard_review"])
    builder.add_edge("answer", END)
    builder.add_edge("write_review", END)
    builder.add_edge("discard_review", END)
    builder.add_edge("refuse", END)
    builder.add_edge("give_up", END)

    return builder.compile(checkpointer=checkpointer)


def _action_context(state: AgentState) -> ActionContext:
    return ActionContext(**state["context"].model_dump(), action_id=state["action_id"])


def _pending_message(proposal: ReviewProposal) -> str:
    comment = f'comentário "{proposal.comment}"' if proposal.comment else "sem comentário"

    return (
        f"Pronto para registrar a avaliação do pedido {proposal.order_id}: nota {proposal.rating}, "
        f"{comment}. Ela só será gravada depois da sua aprovação."
    )


def _sql_draft_update(call: ModelCall[prompts.SQLDraft], *, attempts: int) -> AgentState:
    if call.output is None:
        return {"usage": call.usage, "model_stop": call.stop_reason}

    return {"usage": call.usage, "sql": call.output.sql, "attempts": attempts, "error": None}


def _stopped_route(state: AgentState) -> Literal["refuse", "give_up"] | None:
    stop = state.get("model_stop")

    if stop is None:
        return None

    return "refuse" if stop == "refusal" else "give_up"


def _after_understand(state: AgentState) -> Literal["generate_sql", "draft_review", "refuse", "give_up"]:
    stopped = _stopped_route(state)

    if stopped is not None:
        return stopped

    intent = state.get("intent")

    if intent == "query":
        return "generate_sql"

    return "draft_review" if intent == "register_review" else "refuse"


def _after_review_draft(state: AgentState) -> Literal["check_review", "refuse", "give_up"]:
    stopped = _stopped_route(state)

    if stopped is not None:
        return stopped

    return "check_review"


def _after_check_review(state: AgentState) -> Literal["wait_for_approval", "__end__"]:
    # "__end__" is END: a refused review already has its answer.
    return "wait_for_approval" if state.get("status") == "pending_approval" else "__end__"


def _after_approval(state: AgentState) -> Literal["write_review", "discard_review"]:
    return "write_review" if state.get("approved") else "discard_review"


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
