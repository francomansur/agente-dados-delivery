import pytest

from delivery_agent.repositories.delivery_repository import DeliveryRepository
from evals.cases import CASES
from evals.run import compute_references
from test_eval_grading import REFERENCES

pytestmark = pytest.mark.db


def test_reference_sql_gives_the_expected_answers(repository: DeliveryRepository) -> None:
    references = compute_references(CASES, repository)

    assert references == REFERENCES


def test_every_answerable_question_has_reference_sql() -> None:
    answerable = [case for case in CASES if not case.expects_refusal]

    assert len(CASES) == 20
    assert len(answerable) == 16
    assert [case.kind for case in CASES].count("refusal") == 4
