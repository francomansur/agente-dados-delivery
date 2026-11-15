import json
from pathlib import Path

import anthropic
import httpx2
import pytest

from delivery_agent.llm.prompts import Understanding
from delivery_agent.services.question_service import Agent, AgentAnswer
from evals.cases import Case
from evals.run import confirm_spending, estimate_cost, run_evaluation, spent_so_far, summarize
from fakes import COUNT_SQL, DONE, QUERY, FakeDatabase, FakeModel

COUNT = Case(id="C1", question="Quantos pedidos há?", kind="easy", reference_sql="SELECT 1")
REFUSE = Case(id="C2", question="Qual o telefone do cliente?", kind="refusal")
REFERENCES = {"C1": [[1]]}


def agent_with(*script: object) -> Agent:
    return Agent(FakeDatabase(), FakeModel(*script))  # type: ignore[arg-type]


def test_executions_are_graded_saved_and_summarized(tmp_path: Path) -> None:
    agent = agent_with(QUERY, COUNT_SQL, DONE, Understanding(intent="not_answerable", reason="Dado pessoal."))

    outcome = run_evaluation(
        [COUNT, REFUSE],
        REFERENCES,
        lambda q: agent.ask(q, source="eval"),
        reps=1,
        out_dir=tmp_path,
        budget_left_usd=1,
    )
    summary = summarize([COUNT, REFUSE], outcome, model="claude-haiku-5-5", reps=1, commit="abc123")
    lines = (tmp_path / "results.jsonl").read_text(encoding="utf-8").splitlines()

    assert [execution.correct for execution in outcome.executions] == [True, True]
    assert len(lines) == 2
    assert json.loads(lines[0])["case_id"] == "C1"
    assert summary["accuracy"] == 1.0
    assert summary["refusal_accuracy"] == 1.0
    assert summary["per_question"] == {"C1": {"correct": 1, "valid": 1}, "C2": {"correct": 1, "valid": 1}}
    assert summary["total_cost_usd"] > 0


def test_infra_error_is_not_scored(tmp_path: Path) -> None:
    calls: list[str] = []
    error = anthropic.APIConnectionError(
        request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    )

    def failing_ask(question: str) -> AgentAnswer:
        calls.append(question)

        raise error

    outcome = run_evaluation(
        [COUNT], REFERENCES, failing_ask, reps=1, out_dir=tmp_path, budget_left_usd=1, retry_delay_s=0
    )
    summary = summarize([COUNT], outcome, model="claude-haiku-5-5", reps=1, commit="abc123")

    assert len(calls) == 2
    assert outcome.executions == []
    assert summary["accuracy"] is None
    assert summary["infrastructure_errors"] == 1
    assert (
        json.loads((tmp_path / "errors.jsonl").read_text(encoding="utf-8"))["error"] == "APIConnectionError"
    )


def test_the_budget_stops_the_run(tmp_path: Path) -> None:
    outcome = run_evaluation(
        [COUNT], REFERENCES, agent_with().ask, reps=3, out_dir=tmp_path, budget_left_usd=0
    )

    assert outcome.stopped_by_budget
    assert outcome.executions == []


def test_asks_before_spending() -> None:
    questions: list[str] = []

    def no(prompt: str) -> str:
        questions.append(prompt)

        return "n"

    assert not confirm_spending(0.1, 0.0, 2.0, no)
    assert confirm_spending(0.1, 0.0, 2.0, lambda _: "s")
    assert not confirm_spending(1.5, 1.0, 2.0, no)
    assert len(questions) == 1


def test_estimate_and_spending_come_from_previous_runs(tmp_path: Path) -> None:
    assert estimate_cost(tmp_path, 60) == 60 * 0.002

    run = tmp_path / "2026-11-15_1500_claude-haiku-5-5"
    run.mkdir()
    (run / "summary.json").write_text(
        json.dumps({"total_cost_usd": 0.03, "executions": 60}), encoding="utf-8"
    )

    assert spent_so_far(tmp_path) == 0.03
    assert estimate_cost(tmp_path, 60) == pytest.approx(0.045)
