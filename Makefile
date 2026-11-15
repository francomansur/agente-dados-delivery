# Daily shortcuts. In the CI, run them with BIN= to use the tools on the PATH.
BIN ?= .venv/bin/

.PHONY: install db-up db-down db-reset lint format typecheck test check

install:
	python3.12 -m venv .venv
	$(BIN)pip install -e ".[dev]"

db-up:
	docker compose up -d --build --wait postgres

db-down:
	docker compose down

db-reset:
	docker compose down -v
	docker compose up -d --build --wait postgres

lint:
	$(BIN)ruff check .
	$(BIN)ruff format --check .

format:
	$(BIN)ruff format .
	$(BIN)ruff check --fix .

typecheck:
	$(BIN)mypy

test:
	$(BIN)pytest --cov

check: lint typecheck test
