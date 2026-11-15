import pytest

from delivery_agent.core.errors import ExecutionError
from delivery_agent.llm.prompts import SQLDraft, Understanding
from delivery_agent.services.question_service import AgentAnswer, QuestionService
from fakes import COUNT_SQL, DONE, QUERY, FakeModel, FakeSQLTools

QUESTION = "Quantos pedidos foram entregues?"
BROKEN_SQL = SQLDraft(sql="SELECT nope FROM orders")
MISSING_COLUMN = ExecutionError('Erro do Postgres: column "nope" does not exist')


def ask(model: FakeModel, tools: FakeSQLTools | None = None) -> AgentAnswer:
    return QuestionService(tools or FakeSQLTools(), model).ask(QUESTION, source="eval")


def test_question_is_answered_with_sql_and_rows() -> None:
    model = FakeModel(QUERY, COUNT_SQL, DONE)

    answer = ask(model)

    assert answer.status == "answered"
    assert answer.answer == "Há 1 pedido."
    assert answer.sql == "SELECT count(*) AS n FROM orders\nLIMIT 101"
    assert answer.columns == ["n"]
    assert answer.rows == [[1]]
    assert answer.attempts == 1
    assert [request.effort for request in model.requests] == ["low", "medium", "low"]


def test_out_of_scope_is_refused_without_sql() -> None:
    model = FakeModel(Understanding(intent="out_of_scope", reason="A pergunta pede para apagar dados."))
    tools = FakeSQLTools()

    answer = ask(model, tools)

    assert answer.status == "refused"
    assert answer.answer == "A pergunta pede para apagar dados."
    assert answer.sql is None
    assert answer.rows == []
    assert len(model.requests) == 1
    assert tools.executed == []


def test_not_answerable_explains_what_is_missing() -> None:
    reason = "A base não tem os custos dos restaurantes, então não dá para calcular o lucro."

    answer = ask(FakeModel(Understanding(intent="not_answerable", reason=reason)))

    assert answer.status == "refused"
    assert answer.answer == reason


def test_answer_prompt_receives_the_sql_and_the_rows() -> None:
    model = FakeModel(QUERY, COUNT_SQL, DONE)

    ask(model)

    assert "SELECT count(*) AS n FROM orders" in model.requests[2].prompt
    assert '"rows": [[1]]' in model.requests[2].prompt


def test_sql_error_is_fixed() -> None:
    model = FakeModel(QUERY, BROKEN_SQL, COUNT_SQL, DONE)

    answer = ask(model, FakeSQLTools({"nope": MISSING_COLUMN}))

    assert answer.status == "answered"
    assert answer.attempts == 2
    assert "SELECT nope FROM orders" in model.requests[2].prompt
    assert 'column "nope" does not exist' in model.requests[2].prompt


def test_rejected_sql_goes_back_to_the_model_and_never_runs() -> None:
    model = FakeModel(QUERY, SQLDraft(sql="DELETE FROM orders"), COUNT_SQL, DONE)
    tools = FakeSQLTools()

    answer = ask(model, tools)

    assert answer.status == "answered"
    assert "recebi um comando DELETE" in model.requests[2].prompt
    assert all("DELETE" not in sql for sql in tools.executed)


def test_gives_up_after_two_fixes() -> None:
    model = FakeModel(QUERY, BROKEN_SQL, BROKEN_SQL, BROKEN_SQL)

    answer = ask(model, FakeSQLTools({"nope": MISSING_COLUMN}))

    assert answer.status == "failed"
    assert answer.attempts == 3
    assert "falhou 3 vezes" in answer.answer
    assert 'column "nope" does not exist' in answer.answer
    assert answer.sql == "SELECT nope FROM orders\nLIMIT 101"
    assert len(model.requests) == 4


@pytest.mark.parametrize("script", [("refusal",), (QUERY, "refusal")])
def test_model_refusal(script: tuple[object, ...]) -> None:
    answer = ask(FakeModel(*script))  # type: ignore[arg-type]

    assert answer.status == "refused"
    assert answer.answer == "O modelo se recusou a responder esta pergunta."


def test_max_tokens() -> None:
    answer = ask(FakeModel(QUERY, "max_tokens"))

    assert answer.status == "failed"
    assert "limite de tokens" in answer.answer


def test_output_out_of_format_fails() -> None:
    answer = ask(FakeModel("invalid_output"))

    assert answer.status == "failed"
    assert "fora do formato" in answer.answer


@pytest.mark.parametrize(("stop_reason", "status"), [("max_tokens", "failed"), ("refusal", "refused")])
def test_answer_that_stops_keeps_the_sql_and_rows(stop_reason: str, status: str) -> None:
    answer = ask(FakeModel(QUERY, COUNT_SQL, stop_reason))

    assert answer.status == status
    assert answer.rows == [[1]]
    assert answer.sql is not None


def test_usage_and_cost_are_summed_over_calls() -> None:
    answer = ask(FakeModel(QUERY, COUNT_SQL, DONE))

    assert answer.usage.model == "claude-haiku-5-5"
    assert answer.usage.model_calls == 3
    assert answer.usage.input_tokens == 300
    assert answer.usage.output_tokens == 150
    assert answer.usage.cache_read_tokens == 3000
    assert answer.usage.cost_usd == pytest.approx(3 * (100 * 0.10 + 50 * 0.50 + 1000 * 0.01) / 1e6)
