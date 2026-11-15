"""Run the evaluation: every question, a few repetitions each, graded automatically.

Each run is saved in ``evals/runs/<date-time>_<model>/`` with ``summary.json``, ``results.jsonl``
(one line per execution) and ``errors.jsonl`` (infrastructure failures, kept out of the score).
The run calls ``Agent.ask``, the same entry point as the API, and asks before spending money.

Usage:
    python -m evals.run            # 20 questions x 3 repetitions
    python -m evals.run --reps 1 --only Q01 Q07
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import anthropic
import psycopg
from pydantic import BaseModel

from delivery_agent.core.audit import AuditLog
from delivery_agent.core.errors import DatabaseUnavailable
from delivery_agent.core.settings import Settings
from delivery_agent.llm.client import AnthropicModel
from delivery_agent.repositories.delivery_repository import Cell, Database, fetch_rows
from delivery_agent.services.question_service import Agent, AgentAnswer, UsageReport
from evals.cases import CASES, Case, Kind
from evals.grading import grade

RUNS_DIR = Path(__file__).parent / "runs"
DEFAULT_REPS = 3
DEFAULT_BUDGET_USD = 2.0
# Conservative cost per execution before the first run; the smoke test measured about US$ 0.0005.
FALLBACK_COST_USD = 0.002
INFRA_ERRORS = (anthropic.APIError, DatabaseUnavailable)


class Execution(BaseModel):
    """One question asked once: what the agent returned and how it was graded."""

    case_id: str
    kind: Kind
    rep: int
    status: str
    correct: bool
    sql_ok: bool | None
    answer_ok: bool | None
    missing: list[str]
    answer: str
    sql: str | None
    rows: list[list[Cell]]
    attempts: int
    usage: UsageReport
    duration_ms: float
    request_id: str


@dataclass
class RunOutcome:
    """Executions and infrastructure errors of one run."""

    executions: list[Execution] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    stopped_by_budget: bool = False


def compute_references(cases: Sequence[Case], conninfo: str) -> dict[str, list[list[Cell]]]:
    """Run the reference SQL of every answerable question as the read-only role.

    Args:
        cases: Evaluation questions.
        conninfo: Connection string of ``agent_reader``.

    Returns:
        The rows of each reference result, keyed by question id.
    """
    references: dict[str, list[list[Cell]]] = {}

    with psycopg.connect(conninfo) as conn:
        conn.read_only = True

        for case in cases:
            if case.reference_sql is None:
                continue

            with conn.transaction(force_rollback=True):
                _, rows, _ = fetch_rows(conn, case.reference_sql, 100)

            references[case.id] = rows

    return references


def run_evaluation(
    cases: Sequence[Case],
    references: Mapping[str, list[list[Cell]]],
    ask: Callable[[str], AgentAnswer],
    *,
    reps: int,
    out_dir: Path,
    budget_left_usd: float,
    retry_delay_s: float = 2.0,
) -> RunOutcome:
    """Ask every question ``reps`` times, grade each answer and append it to the run files.

    Repetitions go round by round, so a run stopped by the budget still covers every question
    evenly. An infrastructure failure is retried once; if it fails again it goes to
    ``errors.jsonl`` and is left out of the score.

    Args:
        cases: Evaluation questions.
        references: Rows of each reference result.
        ask: Asks the agent one question.
        reps: Repetitions per question.
        out_dir: Folder of this run.
        budget_left_usd: The run stops before spending more than this.
        retry_delay_s: Wait before retrying an infrastructure failure.

    Returns:
        The graded executions and the infrastructure errors.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    outcome = RunOutcome()
    spent = 0.0

    for rep in range(1, reps + 1):
        for case in cases:
            if spent >= budget_left_usd:
                outcome.stopped_by_budget = True

                return outcome

            answer = _ask_with_retry(ask, case, rep, outcome, out_dir, retry_delay_s)

            if answer is None:
                continue

            result = grade(case, references.get(case.id), answer)
            execution = Execution(
                case_id=case.id,
                kind=case.kind,
                rep=rep,
                status=answer.status,
                correct=result.correct,
                sql_ok=result.sql_ok,
                answer_ok=result.answer_ok,
                missing=list(result.missing),
                answer=answer.answer,
                sql=answer.sql,
                rows=answer.rows[:20],
                attempts=answer.attempts,
                usage=answer.usage,
                duration_ms=answer.duration_ms,
                request_id=answer.request_id,
            )
            outcome.executions.append(execution)
            spent += answer.usage.cost_usd
            _append(out_dir / "results.jsonl", execution.model_dump_json())
            print(f"{case.id} rodada {rep}: {'certo' if result.correct else 'errado'} ({answer.status})")

    return outcome


def summarize(
    cases: Sequence[Case], outcome: RunOutcome, *, model: str, reps: int, commit: str
) -> dict[str, Any]:
    """Compute the figures of a run.

    The accuracy is the mean, over the questions, of the share of correct executions of each one.

    Returns:
        The content of ``summary.json``.
    """
    executions = outcome.executions
    per_case: dict[str, dict[str, int]] = {}

    for case in cases:
        own = [execution for execution in executions if execution.case_id == case.id]
        per_case[case.id] = {"correct": sum(execution.correct for execution in own), "valid": len(own)}

    rates = [counts["correct"] / counts["valid"] for counts in per_case.values() if counts["valid"]]
    answerable = [execution for execution in executions if execution.kind != "refusal"]
    refusals = [execution for execution in executions if execution.kind == "refusal"]
    by_kind: dict[str, float | None] = {}

    for kind in ("easy", "medium", "hard", "refusal"):
        own_rates = [
            per_case[case.id]["correct"] / per_case[case.id]["valid"]
            for case in cases
            if case.kind == kind and per_case[case.id]["valid"]
        ]
        by_kind[kind] = _mean(own_rates)

    input_tokens = sum(
        e.usage.input_tokens + e.usage.cache_read_tokens + e.usage.cache_write_tokens for e in executions
    )

    return {
        "date": datetime.now().isoformat(timespec="seconds"),
        "model": model,
        "commit": commit,
        "questions": len(cases),
        "reps": reps,
        "executions": len(executions),
        "planned_executions": len(cases) * reps,
        "infrastructure_errors": len(outcome.errors),
        "stopped_by_budget": outcome.stopped_by_budget,
        "accuracy": _mean(rates),
        "accuracy_by_kind": by_kind,
        "sql_ok_rate": _mean([float(bool(e.sql_ok)) for e in answerable]),
        "answer_ok_rate": _mean([float(bool(e.answer_ok)) for e in answerable]),
        "refusal_accuracy": _mean([float(e.correct) for e in refusals]),
        "mean_seconds": _mean([e.duration_ms / 1000 for e in executions]),
        "mean_cost_usd": _mean([e.usage.cost_usd for e in executions]),
        "total_cost_usd": sum(e.usage.cost_usd for e in executions),
        "cache_read_share": (sum(e.usage.cache_read_tokens for e in executions) / input_tokens)
        if input_tokens
        else None,
        "per_question": per_case,
    }


def spent_so_far(runs_dir: Path) -> float:
    """Total cost of every run already saved."""
    return float(sum(summary["total_cost_usd"] for summary in _summaries(runs_dir)))


def estimate_cost(runs_dir: Path, executions: int) -> float:
    """Estimate the cost of a run from the previous ones, or from a conservative guess."""
    summaries = [summary for summary in _summaries(runs_dir) if summary["executions"]]

    if not summaries:
        return executions * FALLBACK_COST_USD

    cost = sum(summary["total_cost_usd"] for summary in summaries)
    count = sum(summary["executions"] for summary in summaries)

    return float(executions * cost / count * 1.5)  # with a margin, because answers vary in length


def confirm_spending(estimate: float, spent: float, budget: float, ask_user: Callable[[str], str]) -> bool:
    """Show the estimate and ask before spending (requirement EVA-08).

    Returns:
        Whether the run may go on: it fits the budget and the user said yes.
    """
    print(
        f"Estimativa desta rodada: US$ {estimate:.3f}. Já gasto nas rodadas anteriores: US$ {spent:.4f}. "
        f"Teto: US$ {budget:.2f}."
    )

    if spent + estimate > budget:
        print("A estimativa passa do teto. Rodada cancelada.")

        return False

    return ask_user("Continuar? [s/N] ").strip().lower() in ("s", "sim", "y", "yes")


def main(argv: list[str] | None = None) -> int:
    """Run the evaluation from the command line.

    Returns:
        The exit code: 0 on success.
    """
    parser = argparse.ArgumentParser(
        description="Roda a avaliação do agente e salva o resultado em evals/runs/."
    )
    parser.add_argument("--reps", type=int, default=DEFAULT_REPS, help="repetições por pergunta")
    parser.add_argument(
        "--only", nargs="+", metavar="ID", help="roda só estas perguntas (por exemplo, Q01 Q07)"
    )
    parser.add_argument(
        "--budget", type=float, default=DEFAULT_BUDGET_USD, help="teto de gasto em US$, no total"
    )
    parser.add_argument("--yes", action="store_true", help="não pergunta antes de gastar")
    args = parser.parse_args(argv)

    settings = Settings()
    cases = [case for case in CASES if not args.only or case.id in args.only]
    spent = spent_so_far(RUNS_DIR)
    estimate = estimate_cost(RUNS_DIR, len(cases) * args.reps)

    if settings.anthropic_api_key is None:
        print("Defina ANTHROPIC_API_KEY no .env antes de rodar a avaliação.")

        return 2

    if not confirm_spending(estimate, spent, args.budget, (lambda _: "s") if args.yes else input):
        return 1

    references = compute_references(cases, settings.agent_conninfo())
    model = AnthropicModel(settings.anthropic_api_key.get_secret_value(), settings.llm_model)
    out_dir = RUNS_DIR / f"{datetime.now():%Y-%m-%d_%H%M}_{settings.llm_model}"

    with Database(settings, AuditLog(settings.audit_log_path)) as database:
        agent = Agent(database, model)
        outcome = run_evaluation(
            cases,
            references,
            lambda question: agent.ask(question, source="eval"),
            reps=args.reps,
            out_dir=out_dir,
            budget_left_usd=args.budget - spent,
        )

    summary = summarize(cases, outcome, model=settings.llm_model, reps=args.reps, commit=_commit())
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    _print_summary(summary, out_dir)

    return 0


def _ask_with_retry(
    ask: Callable[[str], AgentAnswer],
    case: Case,
    rep: int,
    outcome: RunOutcome,
    out_dir: Path,
    retry_delay_s: float,
) -> AgentAnswer | None:
    for attempt in (1, 2):
        try:
            return ask(case.question)
        except INFRA_ERRORS as error:
            if attempt == 2:
                record = {
                    "case_id": case.id,
                    "rep": rep,
                    "error": type(error).__name__,
                    "message": str(error),
                }
                outcome.errors.append(record)
                _append(out_dir / "errors.jsonl", json.dumps(record, ensure_ascii=False))
                print(
                    f"{case.id} rodada {rep}: erro de infraestrutura ({type(error).__name__}), fora da taxa"
                )

                return None

            time.sleep(retry_delay_s)

    return None


def _summaries(runs_dir: Path) -> list[dict[str, Any]]:
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(runs_dir.glob("*/summary.json"))]


def _append(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8") as file:
        file.write(line + "\n")


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _commit() -> str:
    git = shutil.which("git")

    if git is None:
        return "desconhecido"

    # Fixed arguments, nothing from the user: safe to run.
    result = subprocess.run(  # noqa: S603
        [git, "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=False
    )

    return result.stdout.strip() or "desconhecido"


def _percent(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.1f}%".replace(".", ",")


def _print_summary(summary: Mapping[str, Any], out_dir: Path) -> None:
    print()

    if not summary["executions"]:
        print(f"Nenhuma execução válida. Detalhes em {out_dir}")

        return

    by_kind = summary["accuracy_by_kind"]
    print(f"Taxa de acerto: {_percent(summary['accuracy'])} ({summary['executions']} execuções válidas)")
    print(
        f"Por tipo: fáceis {_percent(by_kind['easy'])}, médias {_percent(by_kind['medium'])}, "
        f"difíceis {_percent(by_kind['hard'])}, recusas {_percent(by_kind['refusal'])}"
    )
    sql_ok, answer_ok = _percent(summary["sql_ok_rate"]), _percent(summary["answer_ok_rate"])
    print(f"SQL certo: {sql_ok}; resposta certa: {answer_ok}")
    print(
        f"Tempo médio: {summary['mean_seconds']:.1f} s; custo médio: US$ {summary['mean_cost_usd']:.4f} "
        f"por pergunta; custo total: US$ {summary['total_cost_usd']:.4f}"
    )
    print(f"Resultado salvo em {out_dir}")


if __name__ == "__main__":
    raise SystemExit(main())
