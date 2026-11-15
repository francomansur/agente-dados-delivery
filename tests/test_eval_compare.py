import json
from pathlib import Path

import pytest

from evals.compare import compare, main


def summary(accuracy: float, **counts: int) -> dict[str, object]:
    return {
        "reps": 3,
        "accuracy": accuracy,
        "per_question": {case_id: {"correct": correct, "valid": 3} for case_id, correct in counts.items()},
    }


def test_flags_only_changes_beyond_the_noise() -> None:
    result = compare(summary(0.6, Q01=1, Q02=3, Q03=3, Q04=2), summary(0.7, Q01=3, Q02=2, Q03=0, Q04=3))

    assert [change.case_id for change in result.improved] == ["Q01"]
    assert [change.case_id for change in result.worsened] == ["Q03"]
    assert result.improved[0].before == "1/3"
    assert result.improved[0].after == "3/3"
    assert result.threshold == 2
    assert (result.accuracy_before, result.accuracy_after) == (0.6, 0.7)


def test_main_compares_the_two_given_runs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runs = []

    for name, content in (("a", summary(0.5, Q01=0)), ("b", summary(1.0, Q01=3))):
        run = tmp_path / name
        run.mkdir()
        (run / "summary.json").write_text(json.dumps(content), encoding="utf-8")
        runs.append(str(run))

    assert main(runs) == 0

    output = capsys.readouterr().out

    assert "50,0% -> 100,0%" in output
    assert "Melhoraram: Q01 (0/3 -> 3/3)" in output


def test_main_needs_two_runs(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["only-one"]) == 0
    assert "duas rodadas" in capsys.readouterr().out
