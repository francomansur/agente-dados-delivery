import csv
import re
from pathlib import Path

import pytest

from delivery_agent.seed import TABLE_COLUMNS, Row, generate, main

EXPECTED_COUNTS = {
    "restaurants": 146,
    "customers": 4200,
    "couriers": 460,
    "menu_items": 1325,
    "orders": 28172,
    "order_items": 54956,
    "reviews": 10800,
}


@pytest.fixture(scope="module")
def dataset() -> dict[str, list[Row]]:
    return generate()


def test_same_seed_same_data(dataset: dict[str, list[Row]]) -> None:
    assert generate() == dataset
    assert generate(seed=7)["orders"] != dataset["orders"]


def test_counts_match_spec(dataset: dict[str, list[Row]]) -> None:
    assert {table: len(rows) for table, rows in dataset.items()} == EXPECTED_COUNTS


def test_rows_follow_table_columns(dataset: dict[str, list[Row]]) -> None:
    for table, columns in TABLE_COLUMNS.items():
        assert {len(row) for row in dataset[table]} == {len(columns)}


def test_personal_data_is_fictional(dataset: dict[str, list[Row]]) -> None:
    columns = TABLE_COLUMNS["customers"]
    emails = [row[columns.index("email")] for row in dataset["customers"]]
    phones = [row[columns.index("phone")] for row in dataset["customers"]]

    assert all(isinstance(email, str) and email.endswith("@example.com") for email in emails)
    assert len(set(emails)) == len(emails)
    assert all(isinstance(phone, str) and re.fullmatch(r"\(\d{2}\) 90000-\d{4}", phone) for phone in phones)


def test_main_writes_one_csv_per_table(tmp_path: Path) -> None:
    main(["--out", str(tmp_path)])

    for table, columns in TABLE_COLUMNS.items():
        with (tmp_path / f"{table}.csv").open(encoding="utf-8") as file:
            rows = list(csv.reader(file))

        assert tuple(rows[0]) == columns
        assert len(rows) - 1 == EXPECTED_COUNTS[table]
