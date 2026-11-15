"""Automatic grading of the agent's answers (requirement EVA-02).

An answerable question is correct when the agent answered it and every value of the reference
result shows up both in the rows the agent returned (SQL check) and in its text (answer check).
Integers must be exact; decimals tolerate rounding (up to 1% or 0.05). The text may use the
Brazilian format (R$ 1.234,56 and 12,5%), and codes count only by their Portuguese label. A
question that must be refused is correct only when the agent refused it: a failure is not a refusal.
"""

from __future__ import annotations

import itertools
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

from delivery_agent.agent import AgentAnswer
from delivery_agent.database import Cell
from evals.cases import Case

_NUMBER = re.compile(
    r"(?<!\w)(?P<number>\d+(?:[.,]\d+)*)(?:\s*(?P<scale>mil|milh(?:ão|ao|ões|oes)|bilh(?:ão|ao|ões|oes))\b)?",
    re.IGNORECASE,
)
_SCALES = {"mil": 1e3, "milhao": 1e6, "milhoes": 1e6, "bilhao": 1e9, "bilhoes": 1e9}


@dataclass(frozen=True)
class Grade:
    """Result of grading one execution."""

    correct: bool
    sql_ok: bool | None  # None for the questions that must be refused
    answer_ok: bool | None
    missing: tuple[str, ...]  # what was not found, to make wrong answers easy to inspect


def grade(case: Case, reference_rows: Sequence[Sequence[Cell]] | None, answer: AgentAnswer) -> Grade:
    """Grade one answer of the agent.

    Args:
        case: The question.
        reference_rows: Result of the reference SQL; ``None`` for the questions that must be refused.
        answer: What the agent returned.

    Returns:
        Whether the answer is correct, the result of each check and what was missing.

    Raises:
        ValueError: An answerable question came without reference rows.
    """
    if case.expects_refusal:
        refused = answer.status == "refused"
        missing = () if refused else (f"status {answer.status}, esperado refused",)

        return Grade(correct=refused, sql_ok=None, answer_ok=None, missing=missing)

    if reference_rows is None:
        raise ValueError(f"A pergunta {case.id} precisa do resultado do SQL de referência.")

    rows_missing = check_rows(case, reference_rows, answer.rows)
    text_missing = check_text(case, reference_rows, answer.answer)
    status_missing = [] if answer.status == "answered" else [f"status {answer.status}, esperado answered"]
    sql_ok = not rows_missing
    answer_ok = not text_missing

    return Grade(
        correct=answer.status == "answered" and sql_ok and answer_ok,
        sql_ok=sql_ok,
        answer_ok=answer_ok,
        missing=tuple(status_missing + rows_missing + text_missing),
    )


def check_rows(
    case: Case, reference_rows: Sequence[Sequence[Cell]], rows: Sequence[Sequence[Cell]]
) -> list[str]:
    """Look for every reference value among the cells the agent returned.

    Returns:
        A description of each value that is missing, and of a ranking out of order.
    """
    missing: list[str] = []
    positions: list[int] = []

    for row in reference_rows:
        for index, value in enumerate(row):
            if value is None:
                continue

            position = next(
                (
                    number
                    for number, cells in enumerate(rows)
                    if any(_cell_matches(case, value, c) for c in cells)
                ),
                None,
            )

            if position is None:
                missing.append(f"linhas: {value}")
            elif index == 0:
                positions.append(position)

    if case.ordered and not _increasing(positions):
        missing.append("linhas: ordem do ranking")

    return missing


def check_text(case: Case, reference_rows: Sequence[Sequence[Cell]], text: str) -> list[str]:
    """Look for every reference value in the text of the answer.

    Returns:
        A description of each value that is missing, and of a ranking out of order.
    """
    missing: list[str] = []
    positions: list[int] = []
    numbers = number_values(text)
    words = f" {plain(text)} "

    for row in reference_rows:
        for index, value in enumerate(row):
            if value is None:
                continue

            if isinstance(value, str) or value in case.labels:
                found = [words.find(f" {plain(form)} ") for form in _text_forms(case, value)]
                found = [position for position in found if position >= 0]

                if not found:
                    missing.append(f"texto: {value}")
                elif index == 0:
                    positions.append(min(found))
            elif not any(_number_matches(value, number) for number in numbers):
                missing.append(f"texto: {value}")

    if case.ordered and not _increasing(positions):
        missing.append("texto: ordem do ranking")

    return missing


def number_values(text: str) -> list[float]:
    """Every number written in the text, read both in the Brazilian and in the English style.

    Args:
        text: Answer of the agent.

    Returns:
        The possible values of each number, scaled by "mil", "milhão" and similar words.
    """
    values: list[float] = []

    for match in _NUMBER.finditer(text):
        scale = _SCALES[plain(match["scale"])] if match["scale"] else 1.0
        values.extend(reading * scale for reading in _readings(match["number"]))

    return values


def plain(text: str) -> str:
    """Lowercase ASCII words only, so that "São Paulo" and "sao paulo" compare equal."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()

    return " ".join(re.findall(r"[a-z0-9]+", ascii_text))


def _readings(token: str) -> set[float]:
    readings: set[float] = set()

    if "," in token and "." in token:
        if token.rfind(",") > token.rfind("."):
            readings.add(float(token.replace(".", "").replace(",", ".")))  # 1.234,56
        else:
            readings.add(float(token.replace(",", "")))  # 1,234.56
    elif "," in token:
        parts = token.split(",")

        if len(parts) == 2:
            readings.add(float(token.replace(",", ".")))  # decimal comma: 12,5

        if all(len(part) == 3 for part in parts[1:]):
            readings.add(float(token.replace(",", "")))  # thousands comma: 1,234
    elif "." in token:
        parts = token.split(".")

        if all(len(part) == 3 for part in parts[1:]):
            readings.add(float(token.replace(".", "")))  # thousands dot: 26.608

        if len(parts) == 2:
            readings.add(float(token))  # decimal dot: 12.5
    else:
        readings.add(float(token))

    return readings


def _number_matches(expected: Cell, found: float) -> bool:
    if isinstance(expected, bool) or not isinstance(expected, int | float):
        return False

    if isinstance(expected, int):
        return found == expected

    return abs(found - expected) <= max(0.05, 0.01 * abs(expected))


def _cell_matches(case: Case, expected: Cell, cell: Cell) -> bool:
    if isinstance(cell, str):
        if isinstance(expected, str) or expected in case.labels:
            forms = {
                plain(str(expected)),
                *(plain(label) for label in case.labels.get(_label_key(expected), ())),
            }

            return plain(cell) in forms

        return False

    if isinstance(cell, bool) or not isinstance(cell, int | float):
        return False

    return _number_matches(expected, float(cell))


def _text_forms(case: Case, value: Cell) -> tuple[str, ...]:
    labels = case.labels.get(_label_key(value))

    return labels if labels else (str(value),)


def _label_key(value: Cell) -> str | int:
    return value if isinstance(value, str | int) and not isinstance(value, bool) else str(value)


def _increasing(positions: list[int]) -> bool:
    return all(earlier < later for earlier, later in itertools.pairwise(positions))
