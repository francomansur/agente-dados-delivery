import pytest

from delivery_agent.agent import AgentAnswer, UsageReport
from delivery_agent.database import Cell
from evals.cases import CASES, Case
from evals.grading import grade, number_values

# Results of the reference SQL on the dataset (checked against the database in test_eval_references.py).
REFERENCES: dict[str, list[list[Cell]]] = {
    "Q01": [[26608]],
    "Q02": [[74.82]],
    "Q03": [[5.55]],
    "Q04": [["credit_card", 11291]],
    "Q05": [[7]],
    "Q06": [[4.25]],
    "Q07": [["São Paulo", 746365.87]],
    "Q08": [["Japonesa", 4018], ["Pizza", 3938], ["Hambúrguer", 3558]],
    "Q09": [[36.52]],
    "Q10": [[2596]],
    "Q11": [[6, 5275]],
    "Q12": [[70.7, 76.74]],
    "Q13": [["Rio de Janeiro", 20.86]],
    "Q14": [[3.68, 4.38]],
    "Q15": [["Esfiha de Carne", 3189]],
    "Q16": [["Sushi Yama - Lourdes", 4.78]],
}
CASE = {case.id: case for case in CASES}
USAGE = UsageReport(
    model="claude-haiku-5-5",
    model_calls=3,
    input_tokens=0,
    output_tokens=0,
    cache_write_tokens=0,
    cache_read_tokens=0,
    cost_usd=0.0,
)


def answer(text: str, rows: list[list[Cell]] | None = None, status: str = "answered") -> AgentAnswer:
    return AgentAnswer(
        request_id="test",
        status=status,  # type: ignore[arg-type]
        answer=text,
        sql="SELECT 1",
        columns=[],
        rows=rows or [],
        truncated=False,
        attempts=1,
        usage=USAGE,
        duration_ms=1.0,
    )


def brazilian(value: Cell, case: Case) -> str:
    """Write a reference value the way a good answer would."""
    if value in case.labels:
        return case.labels[value][0]  # type: ignore[index]

    if isinstance(value, int):
        return f"{value:,}".replace(",", ".")

    if isinstance(value, float):
        return f"{value:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")

    return str(value)


def reference_answer(case_id: str) -> AgentAnswer:
    case = CASE[case_id]
    rows = REFERENCES[case_id]
    text = "; ".join(" ".join(brazilian(value, case) for value in row) for row in rows)

    return answer(f"Resposta: {text}.", rows)


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("R$ 746.365,87", 746365.87),
        ("26.608 pedidos", 26608),
        ("26.608 pedidos", 26.608),
        ("5,55%", 5.55),
        ("cerca de 746 mil", 746000),
        ("1,5 milhão", 1_500_000),
        ("10,631", 10631),
        ("1234.5", 1234.5),
    ],
)
def test_reads_brazilian_and_english_numbers(text: str, value: float) -> None:
    assert any(abs(number - value) < 1e-6 for number in number_values(text))


@pytest.mark.parametrize("case_id", sorted(REFERENCES))
def test_reference_answers_pass(case_id: str) -> None:
    result = grade(CASE[case_id], REFERENCES[case_id], reference_answer(case_id))

    assert result.correct, result.missing


@pytest.mark.parametrize("case_id", sorted(REFERENCES))
@pytest.mark.parametrize("text", ["", "Não sei."])
def test_empty_or_unknown_answers_fail(case_id: str, text: str) -> None:
    assert not grade(CASE[case_id], REFERENCES[case_id], answer(text)).correct


def test_a_plausible_wrong_number_fails() -> None:
    result = grade(CASE["Q01"], REFERENCES["Q01"], answer("Foram 26.607 pedidos entregues.", [[26607]]))

    assert not result.correct
    assert not result.sql_ok
    assert not result.answer_ok


def test_decimals_tolerate_rounding() -> None:
    assert grade(CASE["Q02"], REFERENCES["Q02"], answer("O ticket médio é R$ 74,8.", [[74.82]])).correct


def test_codes_count_only_by_their_portuguese_label() -> None:
    raw = grade(
        CASE["Q04"], REFERENCES["Q04"], answer("credit_card, em 11.291 pedidos.", [["credit_card", 11291]])
    )
    translated = grade(
        CASE["Q04"],
        REFERENCES["Q04"],
        answer("Cartão de crédito, em 11.291 pedidos.", [["credit_card", 11291]]),
    )

    assert raw.sql_ok
    assert not raw.answer_ok
    assert translated.correct


@pytest.mark.parametrize("weekday", [6, "Saturday ", "sábado"])
def test_weekday_counts_by_number_or_name(weekday: Cell) -> None:
    result = grade(CASE["Q11"], REFERENCES["Q11"], answer("Sábado, com 5.275 pedidos.", [[weekday, 5275]]))

    assert result.correct, result.missing


def test_rankings_must_keep_the_order() -> None:
    rows: list[list[Cell]] = [["Japonesa", 4018], ["Pizza", 3938], ["Hambúrguer", 3558]]
    swapped_text = answer("1. Pizza: 3.938\n2. Japonesa: 4.018\n3. Hambúrguer: 3.558", rows)
    swapped_rows = answer("1. Japonesa: 4.018\n2. Pizza: 3.938\n3. Hambúrguer: 3.558", rows[::-1])

    assert not grade(CASE["Q08"], REFERENCES["Q08"], swapped_text).answer_ok
    assert not grade(CASE["Q08"], REFERENCES["Q08"], swapped_rows).sql_ok


def test_names_ignore_accents_case_and_punctuation() -> None:
    text = "O restaurante é o sushi yama (lourdes), com nota 4,78."

    assert grade(CASE["Q16"], REFERENCES["Q16"], answer(text, [["Sushi Yama - Lourdes", 4.78]])).correct


def test_an_answerable_question_needs_status_answered() -> None:
    result = grade(
        CASE["Q01"], REFERENCES["Q01"], answer("Foram 26.608 pedidos.", [[26608]], status="failed")
    )

    assert result.sql_ok
    assert result.answer_ok
    assert not result.correct


@pytest.mark.parametrize(("status", "correct"), [("refused", True), ("failed", False), ("answered", False)])
def test_only_a_refusal_counts_as_refusal(status: str, correct: bool) -> None:
    result = grade(CASE["Q19"], None, answer("Não posso informar telefones.", status=status))

    assert result.correct is correct
    assert result.sql_ok is None


def test_an_answerable_question_needs_its_reference() -> None:
    with pytest.raises(ValueError, match="Q01"):
        grade(CASE["Q01"], None, answer("26.608"))
