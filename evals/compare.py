"""Compare the last evaluation run with the previous one (requirement EVA-07).

The model varies a little from one execution to the next, so a question is only flagged when its
result changes in at least 2 of the 3 repetitions; a single flip is treated as noise.

Usage:
    python -m evals.compare                      # the two most recent runs
    python -m evals.compare evals/runs/A evals/runs/B
"""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evals.run import RUNS_DIR


@dataclass(frozen=True)
class Change:
    """A question whose result moved beyond the noise."""

    case_id: str
    before: str  # correct executions, such as "1/3"
    after: str


@dataclass(frozen=True)
class Comparison:
    """Difference between two runs."""

    accuracy_before: float | None
    accuracy_after: float | None
    improved: list[Change]
    worsened: list[Change]
    threshold: int  # how many repetitions must change for a question to be flagged


def compare(before: Mapping[str, Any], after: Mapping[str, Any]) -> Comparison:
    """Compare two summaries.

    Args:
        before: ``summary.json`` of the older run.
        after: ``summary.json`` of the newer run.

    Returns:
        The change in accuracy and the questions that improved or worsened beyond the noise.
    """
    reps = min(before["reps"], after["reps"])
    threshold = math.ceil(2 * reps / 3)
    improved: list[Change] = []
    worsened: list[Change] = []

    for case_id, counts_after in after["per_question"].items():
        counts_before = before["per_question"].get(case_id)

        if not counts_before or not counts_before["valid"] or not counts_after["valid"]:
            continue

        delta = reps * (
            counts_after["correct"] / counts_after["valid"]
            - counts_before["correct"] / counts_before["valid"]
        )
        change = Change(case_id, _fraction(counts_before), _fraction(counts_after))

        if delta >= threshold - 1e-9:
            improved.append(change)
        elif delta <= -threshold + 1e-9:
            worsened.append(change)

    return Comparison(before["accuracy"], after["accuracy"], improved, worsened, threshold)


def main(argv: list[str] | None = None) -> int:
    """Print the comparison of two runs.

    Returns:
        The exit code: 0 on success.
    """
    parser = argparse.ArgumentParser(
        description="Compara duas rodadas da avaliação (por padrão, as duas últimas)."
    )
    parser.add_argument("runs", nargs="*", type=Path, help="pastas das rodadas: a mais antiga e a mais nova")
    args = parser.parse_args(argv)
    runs = args.runs or sorted(path.parent for path in RUNS_DIR.glob("*/summary.json"))[-2:]

    if len(runs) < 2:
        print("Ainda não há duas rodadas para comparar.")

        return 0

    before, after = (json.loads((run / "summary.json").read_text(encoding="utf-8")) for run in runs[-2:])
    result = compare(before, after)
    print(f"Antes: {runs[-2].name}\nDepois: {runs[-1].name}\n")
    print(f"Taxa de acerto: {_percent(result.accuracy_before)} -> {_percent(result.accuracy_after)}")
    print(f"Uma pergunta só é marcada quando muda em pelo menos {result.threshold} das repetições.")

    for title, changes in (("Melhoraram", result.improved), ("Pioraram", result.worsened)):
        listed = ", ".join(f"{c.case_id} ({c.before} -> {c.after})" for c in changes) or "nenhuma"
        print(f"{title}: {listed}")

    return 0


def _fraction(counts: Mapping[str, int]) -> str:
    return f"{counts['correct']}/{counts['valid']}"


def _percent(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.1f}%".replace(".", ",")


if __name__ == "__main__":
    raise SystemExit(main())
